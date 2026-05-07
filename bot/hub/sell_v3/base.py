import datetime
import pytz
import asyncio
from utils.logger import logger

class SellV3Base:
    """Base class providing shared configuration and timing helpers for Sell V3 components."""
    def __init__(self, manager):
        self.manager = manager
        self.orchestrator = manager.orchestrator
        self.instrument_name = manager.instrument_name

    def _cfg(self, path, default=None, type_func=None):
        return self.orchestrator.get_strat_cfg(f"sell.{path}", default, type_func)

    def _v3_cfg(self, path, default=None, type_func=None, timestamp=None):
        # Delegate to manager to keep workflow_phase context consistent
        return self.manager._v3_cfg(path, default, type_func, timestamp=timestamp)

    def _get_market_open_time(self, timestamp):
        """Returns the market open time for the instrument."""
        is_mcx = self.orchestrator.is_mcx

        # 1. Priority: Strategy JSON sell.v3.start_time (Phase-aware)
        strat_start = self.manager._v3_cfg("start_time", timestamp=timestamp)

        # 2. Priority: Strategy JSON sell.start_time (Global)
        if not strat_start:
            strat_start = self.orchestrator.get_strat_cfg("sell.start_time")

        if strat_start:
            try:
                t_str = str(strat_start).replace('.', ':')
                if t_str:
                    parts = t_str.split(':')
                    if len(parts) == 2:
                        t = datetime.datetime.strptime(t_str, '%H:%M').time()
                    else:
                        t = datetime.datetime.strptime(t_str, '%H:%M:%S').time()
                    return datetime.datetime.combine(timestamp.date(), t).replace(tzinfo=timestamp.tzinfo)
            except: pass

        # 3. Priority: instrument-specific INI config start_time
        inst_start = self.orchestrator.config_manager.get(self.instrument_name, 'start_time')
        if inst_start:
            try:
                if inst_start:
                    if len(str(inst_start).split(':')) == 2:
                        t = datetime.datetime.strptime(str(inst_start), '%H:%M').time()
                    else:
                        t = datetime.datetime.strptime(str(inst_start), '%H:%M:%S').time()
                    return datetime.datetime.combine(timestamp.date(), t).replace(tzinfo=timestamp.tzinfo)
            except: pass

        # 4. Fallback: Default session open
        hour = 9
        minute = 0 if is_mcx else 15
        res = datetime.datetime.combine(timestamp.date(), datetime.time(hour, minute)).replace(tzinfo=timestamp.tzinfo)
        return res

    def _is_in_priming_wait(self, timestamp):
        """Determines if the bot should wait for indicators to prime before evaluating entry."""
        market_open_time = self._get_market_open_time(timestamp)
        if timestamp < market_open_time:
            self.manager._wait_reason = f"Pre-Market: Waiting for market open at {market_open_time.strftime('%H:%M:%S')}"
            return True

        if self.manager.active_trades:
            return False

        is_beg = "beginning" in self.manager.workflow_phase.lower()
        rules = self._v3_cfg('entry_rules_beginning' if is_beg else 'entry_rules_reentry', [])

        if is_beg and not rules:
            return False

        wait_tfs = [1]
        wait_indicators = []
        for r in rules:
            ind = (r.get('indicator') or '').lower()
            uses_tick_ind = False
            if ind in ('vwap', 'vwap_slope', 'slope'):
                uses_tick_ind = True
            elif ind == 'advanced':
                op1 = (r.get('operand1') or '').lower()
                op2 = (r.get('operand2') or '').lower()
                if any(x in (op1, op2) for x in ('vwap', 'slope')):
                    uses_tick_ind = True

            if uses_tick_ind:
                tf_val = r.get('tf')
                if tf_val:
                    wait_tfs.append(int(tf_val))
                    wait_indicators.append(f"{ind.upper()}({tf_val}m)")

        max_wait_tf = max(wait_tfs)
        has_slope = any('SLOPE' in ind for ind in wait_indicators)
        wait_minutes = (2 * max_wait_tf) if has_slope else max_wait_tf

        market_open_wait_end = market_open_time + datetime.timedelta(minutes=wait_minutes)
        if timestamp < market_open_wait_end:
            self.manager._wait_reason = f"Priming: Waiting {wait_minutes}m until {market_open_wait_end.strftime('%H:%M:%S')} for {', '.join(wait_indicators) or '1m boundary'}"
            return True

        startup_wait_end = self.manager._startup_timestamp + datetime.timedelta(minutes=wait_minutes)
        if not self.orchestrator.is_backtest and timestamp < startup_wait_end:
            self.manager._wait_reason = f"Bot Startup Wait until {startup_wait_end.strftime('%H:%M:%S')}"
            return True

        return False

    def get_finalized_anchor(self, timestamp, tf):
        """Returns the last second of the most recently closed candle for the given timeframe."""
        boundary_minute = (timestamp.minute // tf) * tf
        return timestamp.replace(minute=boundary_minute, second=0, microsecond=0) - datetime.timedelta(seconds=1)

    async def _get_atm_keys(self, timestamp):
        interval = self.orchestrator.config_manager.get_int(self.instrument_name, 'strike_interval', 50)
        anchor_price = self.orchestrator.get_anchor_price()

        if not anchor_price:
            return None, None, None
        atm = int(round(anchor_price / interval) * interval)
        expiry = self.orchestrator.atm_manager.get_expiry_by_mode('sell', 'signal')
        if not expiry: return None, None, None
        ce_key = self.orchestrator.atm_manager.find_instrument_key_by_strike(atm, 'CE', expiry)
        pe_key = self.orchestrator.atm_manager.find_instrument_key_by_strike(atm, 'PE', expiry)
        return ce_key, pe_key, atm

    async def evaluate_rules(self, rules, ce_key, pe_key, timestamp, is_entry=True, anchor_ts=None, do_log=False, return_reason=False):
        """
        ULTRA-FAST RULE ENGINE:
        1. Pre-calculates all technical indicators for the pulse.
        2. Dispatches boolean tokens to Rust for high-speed logic evaluation (Shunting-Yard).
        """
        if not rules:
            return (is_entry, "No Rules") if return_reason else is_entry

        from .rust_bridge import RustBridge

        # 1. Gather required indicators for this pulse
        needed = set()
        for r in rules:
            ind = r.get('indicator', '').lower()
            if ind == 'advanced':
                needed.add(r.get('operand1', '').lower())
                needed.add(r.get('operand2', '').lower())
            else: needed.add(ind)

        # Pass 2: build per-indicator config (tf, period, length) from rules.
        # For direct rules the TF/params come from that rule's own fields.
        # For 'advanced' rules the operand indicators inherit the advanced rule's tf.
        # First occurrence wins so that a single calc_tasks entry covers all rules
        # referencing the same indicator.
        _KNOWN = ('vwap', 'rsi', 'roc', 'slope', 'vwap_slope', 'slope_curr', 'slope_prev', 'ltp', 'close')
        # Resolve named config defaults once so call sites never use raw literals
        _rsi_period = self._v3_cfg('rsi.period', 14, int)
        _roc_length = self._v3_cfg('roc.length', 9, int)

        ind_config = {}
        for r in rules:
            ind = r.get('indicator', '').lower()
            r_tf = int(r.get('tf', 1))
            if ind == 'advanced':
                # Advanced operands inherit the enclosing rule's tf.
                # Also resolve period/length for rsi/roc operands via named config.
                for op_key in ('operand1', 'operand2'):
                    op = r.get(op_key, '').lower()
                    if op in _KNOWN and op not in ind_config:
                        op_cfg = {'tf': r_tf}
                        if op == 'rsi':
                            op_cfg['period'] = int(r.get('period', _rsi_period))
                        elif op == 'roc':
                            op_cfg['length'] = int(r.get('length', _roc_length))
                        ind_config[op] = op_cfg
            elif ind in _KNOWN and ind not in ind_config:
                cfg = {'tf': r_tf}
                if ind == 'rsi':
                    cfg['period'] = int(r.get('period', _rsi_period))
                elif ind == 'roc':
                    cfg['length'] = int(r.get('length', _roc_length))
                ind_config[ind] = cfg

        calc_tasks = {}
        for ind, cfg in ind_config.items():
            ind_tf = cfg.get('tf', 1)
            anchor = anchor_ts or self.get_finalized_anchor(timestamp, ind_tf)
            if ind == 'vwap':
                calc_tasks['vwap'] = self._get_combined_vwap(ce_key, pe_key, anchor)
            elif ind == 'rsi':
                calc_tasks['rsi'] = self.orchestrator.indicator_manager.calculate_combined_rsi(ce_key, pe_key, anchor, tf=ind_tf, period=cfg.get('period', _rsi_period))
            elif ind == 'roc':
                calc_tasks['roc'] = self.orchestrator.indicator_manager.calculate_combined_roc(ce_key, pe_key, anchor, tf=ind_tf, length=cfg.get('length', _roc_length))
            elif ind in ('slope', 'vwap_slope'):
                calc_tasks['slope'] = self._get_combined_slope(ce_key, pe_key, timestamp, ind_tf)
            elif ind == 'slope_curr':
                calc_tasks['slope_curr'] = self._get_combined_slope_pair(ce_key, pe_key, timestamp, ind_tf, idx=0)
            elif ind == 'slope_prev':
                calc_tasks['slope_prev'] = self._get_combined_slope_pair(ce_key, pe_key, timestamp, ind_tf, idx=1)
            elif ind == 'ltp':
                calc_tasks['ltp'] = self._get_combined_ltp(ce_key, pe_key)
            elif ind == 'close':
                calc_tasks['close'] = self._get_combined_close(ce_key, pe_key, anchor, ind_tf)

        data_map = {}
        if calc_tasks:
            res_vals = await asyncio.gather(*calc_tasks.values())
            data_map = dict(zip(calc_tasks.keys(), res_vals))

        # 2. Convert rules to evaluation tokens
        tokens = []
        val_strs = []
        for i, r in enumerate(rules):
            indicator = r.get('indicator', '').lower()

            passed = False
            val = None
            adv_label = None  # Friendly label for advanced rules in logs
            if indicator == 'advanced':
                op1_type = r.get('operand1', '')
                op2_type = r.get('operand2', '')
                v1 = float(r.get('operand1_val', 0)) if op1_type == 'VALUE' else data_map.get(op1_type.lower())
                v2 = float(r.get('operand2_val', 0)) if op2_type == 'VALUE' else data_map.get(op2_type.lower())
                _op_sym = r.get('operator_sym', '>')
                if v1 is not None and v2 is not None:
                    passed = self._compare(v1, v2, _op_sym)
                    val = v1 # For logging
                _v1s = f"{v1:.4f}" if isinstance(v1, (int, float)) else "N/A"
                _v2s = f"{v2:.4f}" if isinstance(v2, (int, float)) else "N/A"
                adv_label = f"{op1_type}({_v1s}){_op_sym}{op2_type}({_v2s})"
            else:
                val = data_map.get(indicator)
                if val is not None:
                    passed = self._compare(val, float(r.get('threshold', 0)), r.get('operator_sym', '<' if is_entry else '>'))

            # Use non-empty strings for tokens, ensuring multiple brackets are individual tokens
            if r.get('openBrackets'):
                for b in str(r.get('openBrackets')): tokens.append(b)
            tokens.append('True' if passed else 'False')
            if r.get('closeBrackets'):
                for b in str(r.get('closeBrackets')): tokens.append(b)

            # Join with operator only if there is a subsequent rule
            if i < len(rules) - 1:
                tokens.append(r.get('operator', 'and').lower())

            if adv_label is not None:
                val_strs.append(f"{adv_label}={'PASS' if passed else 'FAIL'}")
            elif val is not None:
                val_strs.append(f"{indicator.upper()}:{val:.4f}")
            else:
                val_strs.append(f"{indicator.upper()}:N/A")

        # 3. Execute Boolean Logic in Rust
        final_res = RustBridge.evaluate_boolean_logic([t for t in tokens if t])

        detailed_reason = " | ".join(val_strs) if (do_log or return_reason) else ""
        return (final_res, detailed_reason) if return_reason else final_res

    def _compare(self, v1, v2, sym):
        if sym == '>': return v1 > v2
        if sym == '<': return v1 < v2
        if sym == '>=': return v1 >= v2
        if sym == '<=': return v1 <= v2
        if sym == '==': return abs(v1 - v2) < 1e-9
        return False

    async def _get_combined_vwap(self, ce_key, pe_key, ts):
        v1 = await self.orchestrator.indicator_manager.calculate_vwap(ce_key, ts)
        v2 = await self.orchestrator.indicator_manager.calculate_vwap(pe_key, ts)
        return (v1 + v2) if (v1 is not None and v2 is not None) else None

    async def _get_combined_slope(self, ce_key, pe_key, timestamp, tf):
        res = await self.orchestrator.indicator_manager.get_vwap_slope_pair(ce_key, pe_key, timestamp, tf)
        return res[0]

    async def _get_combined_slope_pair(self, ce_key, pe_key, timestamp, tf, idx=0):
        """idx=0 returns current slope, idx=1 returns previous slope.
        Used by Advanced rules where the user compares SLOPE_CURR vs SLOPE_PREV."""
        res = await self.orchestrator.indicator_manager.get_vwap_slope_pair(ce_key, pe_key, timestamp, tf)
        if not res or len(res) <= idx:
            return None
        return res[idx]

    async def _get_combined_ltp(self, ce_key, pe_key):
        p1 = self.orchestrator.state_manager.get_ltp(ce_key) or 0
        p2 = self.orchestrator.state_manager.get_ltp(pe_key) or 0
        return p1 + p2

    async def _get_combined_close(self, ce_key, pe_key, anchor, tf):
        o1 = await self.orchestrator.indicator_manager.get_robust_ohlc(ce_key, tf, anchor)
        o2 = await self.orchestrator.indicator_manager.get_robust_ohlc(pe_key, tf, anchor)
        if o1 is not None and not o1.empty and o2 is not None and not o2.empty:
            return float(o1.iloc[-1]['close']) + float(o2.iloc[-1]['close'])
        return None
