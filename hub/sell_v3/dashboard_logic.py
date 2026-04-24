import datetime
import asyncio
from utils.logger import logger
from .base import SellV3Base

class DashboardLogic(SellV3Base):
    """Generates real-time and snapshot data for the V3 Dashboard and Admin UI (Dynamic Rule Support)."""

    async def get_v3_dashboard_data(self, timestamp):
        ce = self.manager.active_trades.get('CE')
        pe = self.manager.active_trades.get('PE')
        ce_strike = pe_strike = key_ce = key_pe = None
        pool_data = []

        ticks = {k: {'ltp': float(v)} for k, v in self.orchestrator.state_manager.option_prices.items() if v}

        if ce and pe:
            key_ce, key_pe = ce['key'], pe['key']
            ce_strike, pe_strike = ce['strike'], pe['strike']
        else:
            # MODULAR: Delegate strike selection to EntryLogic for consistency
            res = await self.manager.entry_logic._resolve_current_candidate_pair(ticks, timestamp)
            if res:
                key_ce, key_pe, ce_strike, pe_strike = res
            else:
                key_ce, key_pe, atm = await self._get_atm_keys(timestamp)
                if atm: ce_strike = pe_strike = atm

            if "beginning" not in self.manager.workflow_phase.lower():
                pool_data = await self._get_admin_pool_snapshot(ticks, timestamp)

        if not key_ce or not key_pe: return {}

        # Technical Metrics (Entry TF for Dashboard)
        entry_tf = self.orchestrator.config_manager.get_int(self.instrument_name, 'v_slope_entry.tf', 1)
        res = await self.orchestrator.indicator_manager.get_vwap_slope_pair(key_ce, key_pe, timestamp, entry_tf)
        curr_slope, prev_slope, v_curr, v_prev, v_prev2 = res

        combined_vwap = (await self.orchestrator.indicator_manager.calculate_vwap(key_ce, timestamp) or 0) + \
                        (await self.orchestrator.indicator_manager.calculate_vwap(key_pe, timestamp) or 0)

        combined_rsi = await self.orchestrator.indicator_manager.calculate_combined_rsi(key_ce, key_pe, timestamp, tf=entry_tf, period=14, skip_api=False)
        combined_roc = await self.orchestrator.indicator_manager.calculate_combined_roc(key_ce, key_pe, timestamp, tf=entry_tf, length=9, include_current=True)

        anchor_ts = timestamp.replace(minute=(timestamp.minute // entry_tf) * entry_tf, second=0, microsecond=0) - datetime.timedelta(seconds=1)
        ohlc_ce = await self.orchestrator.indicator_manager.get_robust_ohlc(key_ce, entry_tf, anchor_ts)
        ohlc_pe = await self.orchestrator.indicator_manager.get_robust_ohlc(key_pe, entry_tf, anchor_ts)
        combined_close = (ohlc_ce.iloc[-1]['close'] + ohlc_pe.iloc[-1]['close']) if (ohlc_ce is not None and not ohlc_ce.empty and ohlc_pe is not None and not ohlc_pe.empty) else 0.0

        ltp_ce = self.orchestrator.state_manager.option_prices.get(key_ce, 0) or 0
        ltp_pe = self.orchestrator.state_manager.option_prices.get(key_pe, 0) or 0

        rise_pct = 0.0
        if combined_vwap > 0 and self.manager.session_min_vwap != float('inf'):
            rise_pct = (combined_vwap - self.manager.session_min_vwap) / self.manager.session_min_vwap * 100

        status = "N/A"
        if self._is_in_priming_wait(timestamp):
            status = "WAITING"
        elif curr_slope is not None:
            # User requirement: Current Slope based status
            status = "DECREASING" if curr_slope <= 0 else "INCREASING"
        else:
            status = "WAITING" # Fallback if anchors not ready but past priming

        # Deriving OK status from dynamic rules
        is_beg = "beginning" in self.manager.workflow_phase.lower()
        rules = self._v3_cfg('entry_rules_beginning' if is_beg else 'entry_rules_reentry', [])

        rsi_ok = vwap_ok = roc_ok = True
        entry_details = []
        if rules:
            for r in rules:
                ind = r.get('indicator')
                passed, val_str = await self.evaluate_rules([r], key_ce, key_pe, timestamp, is_entry=True, anchor_ts=anchor_ts, do_log=False, return_reason=True)

                if ind == 'rsi': rsi_ok = passed
                elif ind == 'vwap': vwap_ok = passed
                elif ind == 'roc': roc_ok = passed

                entry_details.append({"label": ind.upper(), "val": val_str, "ok": passed})

        # --- Evaluate Technical Exit Rules ---
        exit_rules = self._v3_cfg('exit_rules', [])
        exit_rule_status = "OK"
        dyn_exit_details = []

        rsi_tf_macro = self._v3_cfg('rsi_exit.tf', 15, int)
        vwap_tf_macro = self._v3_cfg('vwap_exit.tf', 15, int)
        vslope_tf_macro = self._v3_cfg('v_slope_exit.tf', 15, int)
        macro_tf = max(rsi_tf_macro, vwap_tf_macro, vslope_tf_macro)

        if ce and pe and exit_rules:
            # We use macro anchor for exit rule evaluation consistency
            macro_anchor = timestamp.replace(second=0, microsecond=0) - datetime.timedelta(seconds=1)

            # Evaluate individual rules for detailed UI reporting
            for r in exit_rules:
                passed, val_str = await self.evaluate_rules([r], ce['key'], pe['key'], timestamp, is_entry=False, anchor_ts=macro_anchor, do_log=False, return_reason=True)
                r_tf = r.get('tf', macro_tf)
                dyn_exit_details.append({"label": f"D_{r.get('indicator', 'N/A').upper()}({r_tf}m)", "val": val_str, "ok": not passed})
                if passed:
                    exit_rule_status = "FAIL"

        # --- Exit Metrics for UI Visibility ---
        exit_details = []
        # 1. Guardrails
        g_pnl_target = self._v3_cfg('guardrail_pnl.target_pts', 0.0, float)
        g_pnl_sl = self._v3_cfg('guardrail_pnl.stoploss_pts', 0.0, float)

        pts_pnl = 0.0
        profit_rs = 0.0
        trade_pnl_rs = 0.0
        tsl_lock = getattr(self.manager, 'tsl_high_lock', 0.0)

        if ce and pe:
            pts_pnl = round((ce['entry_price'] + pe['entry_price']) - (float(ltp_ce) + float(ltp_pe)), 2)

            # Resolve total quantity across all brokers
            ref_broker = next(iter(self.orchestrator.broker_manager.brokers.values()), None)
            qty_multiplier = ref_broker.config_manager.get_int(ref_broker.instance_name, 'quantity', 1) if ref_broker else 1
            total_qty = ce.get('lot_size', 50) * qty_multiplier
            trade_pnl_rs = pts_pnl * total_qty

            # Trailing SL Metric
            if self._v3_cfg('tsl_scalable.enabled', False, bool):
                profit_rs = trade_pnl_rs

                if tsl_lock > 0:
                    exit_details.append({"label": "TSL_LOCK", "val": f"Profit:{profit_rs:.0f} vs Lock:{tsl_lock:.0f}", "ok": profit_rs > tsl_lock})

            if g_pnl_target > 0:
                exit_details.append({"label": "PNL_TGT", "val": f"{pts_pnl} >= {g_pnl_target}", "ok": pts_pnl < g_pnl_target})
            if g_pnl_sl < 0:
                exit_details.append({"label": "PNL_SL", "val": f"{pts_pnl} <= {g_pnl_sl}", "ok": pts_pnl > g_pnl_sl})

        macro_anchor = timestamp.replace(second=0, microsecond=0) - datetime.timedelta(seconds=1)
        macro_rsi = await self.orchestrator.indicator_manager.calculate_combined_rsi(key_ce, key_pe, macro_anchor, tf=rsi_tf_macro, period=self._v3_cfg('rsi_exit.period', 14, int))

        g_roc_tf = self._v3_cfg('guardrail_roc.tf', 15, int)
        g_roc_val = await self.orchestrator.indicator_manager.calculate_combined_roc(key_ce, key_pe, macro_anchor, tf=g_roc_tf, length=self._v3_cfg('guardrail_roc.length', 9, int), include_current=False)

        vwap_fail = False
        vwap_ce_m = await self.orchestrator.indicator_manager.calculate_vwap(key_ce, macro_anchor)
        vwap_pe_m = await self.orchestrator.indicator_manager.calculate_vwap(key_pe, macro_anchor)
        combined_vwap_m = (vwap_ce_m or 0) + (vwap_pe_m or 0)
        if combined_vwap_m > 0:
            ohlc_ce_m = await self.orchestrator.indicator_manager.get_robust_ohlc(key_ce, rsi_tf_macro, macro_anchor)
            ohlc_pe_m = await self.orchestrator.indicator_manager.get_robust_ohlc(key_pe, rsi_tf_macro, macro_anchor)
            if ohlc_ce_m is not None and not ohlc_ce_m.empty and ohlc_pe_m is not None and not ohlc_pe_m.empty:
                m_close = round(ohlc_ce_m.iloc[-1]['close'] + ohlc_pe_m.iloc[-1]['close'], 2)
                vwap_fail = m_close > combined_vwap_m

        mode = self._v3_cfg('exit_mode', 'OFF')
        if mode in ['RSI_ONLY', 'RSI_AND_VWAP', 'RSI_OR_VWAP']:
            r_thresh = self._v3_cfg('rsi_exit.threshold', 50.0, float)
            exit_details.append({"label": "M_RSI", "val": f"RSI({rsi_tf_macro}m):{macro_rsi if macro_rsi is not None else '--'} > {r_thresh}", "ok": not (macro_rsi is not None and macro_rsi > r_thresh)})
        if mode in ['VWAP_ONLY', 'RSI_AND_VWAP', 'RSI_OR_VWAP']:
            exit_details.append({"label": "M_VWAP", "val": f"P > V ({vwap_tf_macro}m)", "ok": not vwap_fail})

        exit_details.extend(dyn_exit_details)

        # Guardrail ROC for detail string
        # g_roc_val is already calculated above as a macro metric
        exit_details.append({"label": "G_ROC", "val": f"ROC({g_roc_tf}m):{g_roc_val if g_roc_val is not None else '--'}", "ok": True})

        return {
            "slope_status": status, "slope_rise_pct": round(rise_pct, 2),
            "session_min_vwap": round(self.manager.session_min_vwap, 2) if self.manager.session_min_vwap != float('inf') else 0,
            "combined_vwap": round(combined_vwap, 2),
            "combined_rsi": round(combined_rsi, 1) if combined_rsi is not None else None,
            "combined_roc": round(combined_roc, 2) if combined_roc is not None else None,
            "combined_price": round(float(ltp_ce) + float(ltp_pe), 2), "combined_close": round(combined_close, 2),
            "ce_strike": ce_strike, "pe_strike": pe_strike, "ce_ltp": round(float(ltp_ce), 2), "pe_ltp": round(float(ltp_pe), 2),
            "curr_slope": round(curr_slope, 4) if curr_slope is not None else 0,
            "prev_slope": round(prev_slope, 4) if prev_slope is not None else 0,
            "v_curr": round(v_curr, 2) if v_curr is not None else 0,
            "v_prev": round(v_prev, 2) if v_prev is not None else 0,
            "v_prev2": round(v_prev2, 2) if v_prev2 is not None else 0,
            "rsi_ok": rsi_ok, "vwap_ok": vwap_ok, "roc_ok": roc_ok,
            "slope_ok": (curr_slope <= 0) if curr_slope is not None else True, "pool": pool_data,
            "entry_reason": self.manager.active_trades.get('CE', {}).get('reason', 'SCANNING') if ce else 'SCANNING',
            "entry_tf": entry_tf, "macro_tf": macro_tf,
            "is_priming": self._is_in_priming_wait(timestamp),
            # Exit Metrics
            "macro_rsi": round(macro_rsi, 1) if macro_rsi is not None else None,
            "macro_vwap_fail": vwap_fail,
            "guardrail_roc": round(g_roc_val, 2) if g_roc_val is not None else None,
            "guardrail_pnl": pts_pnl,
            "profit_rs": round(profit_rs, 2),
            "tsl_lock": round(tsl_lock if tsl_lock > 0 else (self.manager.last_trade_locked_pnl if hasattr(self.manager, 'last_trade_locked_pnl') else 0.0), 2),
            "trade_pnl_rs": round(trade_pnl_rs, 2),
            "trades_today": self.manager.trades_completed_today,
            "max_trades": self._v3_cfg('max_trades_per_day', 0, int),
            "exit_rule_status": exit_rule_status,
            "entry_details": entry_details,
            "exit_details": exit_details
        }

    async def _get_admin_pool_snapshot(self, ticks, timestamp):
        interval = self.orchestrator.config_manager.get_int(self.instrument_name, 'strike_interval', 50)
        anchor_price = self.orchestrator.get_anchor_price()
        if not anchor_price: return []
        atm = int(round(anchor_price / interval) * interval)
        expiry = self.orchestrator.atm_manager.signal_expiry_date
        offset = self._v3_cfg('v_slope_pool_offset', None, int)
        if offset is None: offset = self._v3_cfg('reentry_offset', 2, int)
        strikes = [atm + i * interval for i in range(-offset, offset + 1)]

        va = timestamp.replace(minute=(timestamp.minute // 1) * 1, second=0, microsecond=0) - datetime.timedelta(seconds=1)

        tasks = []
        for s in strikes:
            ck = self.orchestrator.atm_manager.find_instrument_key_by_strike(s, 'CE', expiry)
            pk = self.orchestrator.atm_manager.find_instrument_key_by_strike(s, 'PE', expiry)
            cl, pl = ticks.get(ck, {}).get('ltp', 0) if ck else 0, ticks.get(pk, {}).get('ltp', 0) if pk else 0
            if cl >= 50 and pl >= 50:
                tasks.append(self._get_single_strike_snapshot(s, ck, pk, cl, pl, va, timestamp))
        res = await asyncio.gather(*tasks)
        return sorted([r for r in res if r], key=lambda x: x['ce'])

    async def _get_single_strike_snapshot(self, s, ck, pk, cl, pl, va, ts):
        try:
            rules = self._v3_cfg('entry_rules_reentry', [])
            v_passed = await self.evaluate_rules(rules, ck, pk, ts, is_entry=True, anchor_ts=va, do_log=False)

            vw_c, vw_p = await self.orchestrator.indicator_manager.calculate_vwap(ck, va), await self.orchestrator.indicator_manager.calculate_vwap(pk, va)
            c_vw = (vw_c or 0) + (vw_p or 0)
            c_rsi = await self.orchestrator.indicator_manager.calculate_combined_rsi(ck, pk, va, tf=1, period=14, skip_api=False)
            c_roc = await self.orchestrator.indicator_manager.calculate_combined_roc(ck, pk, ts, tf=1, length=9, include_current=True)

            # 2. Calculate VWAP % Metric for Pool (abs((Combined_VWAP - Combined_Close) / Combined_Close))
            vwap_dist = 999.0
            comb_close = cl + pl
            if c_vw > 0 and comb_close > 0:
                vwap_dist = (c_vw - comb_close) / comb_close

            return {
                "ce": s, "pe": s, "ce_ltp": round(cl, 2), "pe_ltp": round(pl, 2), "total": round(comb_close, 2),
                "slope_ok": v_passed,
                "rsi": round(c_rsi, 1) if c_rsi is not None else None,
                "roc": round(c_roc, 2) if c_roc is not None else None,
                "vwap": round(c_vw, 2),
                "vwap_pct": round(vwap_dist * 100, 2)
            }
        except: return None
