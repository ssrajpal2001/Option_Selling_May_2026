import json
import os
import datetime
import asyncio
import pytz
from utils.logger import logger
from .live_trade_log import LiveTradeLog
from .sell_v3.entry_logic import EntryLogic
from .sell_v3.exit_logic import ExitLogic
from .sell_v3.dashboard_logic import DashboardLogic

class SellManagerV3:
    """
    Coordinator for Sell Side V3 Strategy logic.
    Delegates specialized tasks to EntryLogic, ExitLogic, and DashboardLogic components.
    """

    def __init__(self, parent):
        # Support isolated UserSession as parent
        if hasattr(parent, 'user_id'):
            self.user_session = parent
            self.orchestrator = parent.orchestrator
            self.user_id = parent.user_id
        else:
            self.user_session = None
            self.orchestrator = parent
            self.user_id = None

        self.instrument_name = self.orchestrator.instrument_name

        target_uid = self.user_id or os.environ.get('CLIENT_ID')
        if target_uid:
            self.state_file = f'config/sell_v3_state_{target_uid}_{self.instrument_name}.json'
        else:
            self.state_file = f'config/sell_v3_state_{self.instrument_name}.json'

        # Performance optimization: Local reference to state manager
        self.state_manager = self.user_session.state_manager if self.user_session else self.orchestrator.state_manager

        # Persistent State Variables
        self.active_trades = {} # side -> trade_info
        self.last_entry_bucket = None # Start of candle for 1 entry per candle rule
        self.session_min_vwap = float('inf')
        self.session_points_pnl = 0.0
        self.trades_completed_today = 0
        self.tsl_high_lock = 0.0
        self.last_trade_locked_pnl = 0.0
        self._last_exit_timestamp = None
        self.strangle_closed = False
        self.workflow_phase = 'BEGINNING' # or 'CONTINUE'
        self.last_log_time = 0
        self.last_save_time = 0
        self.v3_dashboard_data = {}
        self._startup_timestamp = datetime.datetime.now(pytz.timezone('Asia/Kolkata'))

        # Sub-logic handlers
        self.entry_logic = EntryLogic(self)
        self.exit_logic = ExitLogic(self)
        self.dashboard_logic = DashboardLogic(self)

        # Log strategy settings on startup for visibility (Live and Backtest)
        self.log_active_strategy_settings()

        # State Recovery
        self.load_state()
        if self.orchestrator.is_backtest:
            self.workflow_phase = 'BEGINNING'
            self.strangle_closed = False
            self.active_trades = {}
            self.last_entry_bucket = None
            self.session_min_vwap = float('inf')
            self.session_points_pnl = 0.0
            self.trades_completed_today = 0
            self.tsl_high_lock = 0.0
            self.last_trade_locked_pnl = 0.0
            self.save_state()

    # --- Backward Compatibility Properties ---
    @property
    def ce_placed(self): return 'CE' in self.active_trades
    @property
    def pe_placed(self): return 'PE' in self.active_trades
    @property
    def sell_ce_strike(self): return self.active_trades.get('CE', {}).get('strike')
    @property
    def sell_pe_strike(self): return self.active_trades.get('PE', {}).get('strike')
    @property
    def sell_ce_key(self): return self.active_trades.get('CE', {}).get('key')
    @property
    def sell_pe_key(self): return self.active_trades.get('PE', {}).get('key')
    @property
    def strangle_placed(self): return 'CE' in self.active_trades or 'PE' in self.active_trades
    @property
    def sell_ce_entry_ltp(self): return self.active_trades.get('CE', {}).get('entry_price')
    @property
    def sell_pe_entry_ltp(self): return self.active_trades.get('PE', {}).get('entry_price')

    # TickProcessor compatibility
    @property
    def ce_key(self): return self.sell_ce_key
    @property
    def pe_key(self): return self.sell_pe_key
    @property
    def ce_strike(self): return self.sell_ce_strike
    @property
    def pe_strike(self): return self.sell_pe_strike

    def _cfg(self, path, default=None, type_func=None):
        # Multi-tenant: If user session is present, prefer user-specific strategy config
        if self.user_session and hasattr(self.user_session, 'strategy_config'):
            # Path in JSON is 'sell.v3.etc'
            # strategy_config is likely the resolved dictionary
            pass # Strategy resolution logic in orchestrator handles this via get_strat_cfg

        return self.orchestrator.get_strat_cfg(f"sell.{path}", default, type_func)

    def _v3_cfg(self, path, default=None, type_func=None, timestamp=None):
        # PERFORMANCE: Use tick-cached configuration if available
        if hasattr(self, '_tick_config_cache') and self._tick_config_cache is not None:
            val = self._tick_config_cache.get(path)
            if val is not None:
                if type_func:
                    try: return type_func(val)
                    except: pass
                return val

        # 0. Day-Specific Override (e.g., sell.v3.monday.target_pts)
        if timestamp is None:
            timestamp = getattr(self.orchestrator, '_last_tick_time', datetime.datetime.now(pytz.timezone('Asia/Kolkata')))

        day_name = timestamp.strftime('%A').lower()
        day_val = self._cfg(f"v3.{day_name}.{path}", type_func=type_func)
        if day_val is not None: return day_val

        phase_prefix = "beginning" if "beginning" in self.workflow_phase.lower() else "reentry"
        val = self._cfg(f"v3.{phase_prefix}.{path}", type_func=type_func)
        if val is not None: return val
        val = self._cfg(f"v3.{path}", type_func=type_func)
        if val is not None: return val

        # TF Fallbacks
        if path in ['rsi_entry.tf', 'rsi_exit.tf', 'vwap_entry.tf', 'vwap_exit.tf', 'v_slope_entry.tf']:
            if "rsi" in path: base = "rsi.tf"
            elif "vwap" in path: base = "vwap.tf"
            else: base = "v_slope.tf"
            val = self._cfg(f"v3.{phase_prefix}.{base}", type_func=int)
            if val is not None: return val
            val = self._cfg(f"v3.{base}", type_func=int)
            if val is not None: return val
            return 15 if "exit" in path else 1
        return default

    def log_active_strategy_settings(self):
        """Logs a clear summary of all enabled strategy parameters for the session log."""
        v3_mode = self.orchestrator.get_strat_cfg("v3_mode", False, bool)

        logger.info(f"╔══════════════════════════════════════════════════════════════════════╗")
        logger.info(f"║ ACTIVE V3 STRATEGY SETTINGS (Instrument: {self.instrument_name:<10})          ║")
        logger.info(f"╠══════════════════════════════════════════════════════════════════════╣")
        logger.info(f"║ MASTER STATUS: {'[ENABLED]' if v3_mode else '[DISABLED]'}                                      ║")
        logger.info(f"╠══════════════════════════════════════════════════════════════════════╣")

        # 1. Timing & Sizing
        st = self._v3_cfg('start_time', '09:20:00')
        et = self._v3_cfg('entry_end_time', '14:00:00')
        ct = self._v3_cfg('close_time', '15:20:00')
        ltp_tgt = self._v3_cfg('ltp_target')
        if ltp_tgt is None: ltp_tgt = self._cfg('ltp_min', 50.0)

        lot_size = self.orchestrator.state_manager.lot_size if hasattr(self.orchestrator.state_manager, 'lot_size') else "N/A"
        logger.info(f"║ TIMING: Start:{st} | EntryEnd:{et} | SquareOff:{ct} | Lot:{lot_size:<4} ║")
        logger.info(f"║ PREMIUMS: Target LTP: {ltp_tgt:<51} ║")

        # 2. Entry Rule Engine
        rules_beg = self._v3_cfg('entry_rules_beginning', [])
        rules_re = self._v3_cfg('entry_rules_reentry', [])

        def format_rules(rules, is_entry=True):
            if not rules: return "OFF"
            parts = []
            for i, r in enumerate(rules):
                ind = r.get('indicator', 'N/A').upper()
                tf = r.get('tf', 1)
                br_o, br_c = r.get('openBrackets', ''), r.get('closeBrackets', '')
                rule_desc = None
                if ind == 'VWAP_SLOPE':
                    rule_desc = f"{br_o}Slope({tf}m){br_c}"
                elif ind == 'RSI':
                    op = r.get('operator_sym')
                    if not op:
                        op = "<" if is_entry else ">"
                    rule_desc = f"{br_o}RSI({tf}m, {op}{r.get('threshold')}){br_c}"
                elif ind == 'ROC':
                    if is_entry:
                        target = r.get('target', 0.0)
                        lower = r.get('lower', -5.0)
                        upper = r.get('upper', 5.0)
                        rule_desc = f"{br_o}ROC({tf}m, {lower} to {upper}, Near:{target}){br_c}"
                    else:
                        thresh = r.get('threshold', r.get('upper', 10.0))
                        rule_desc = f"{br_o}ROC({tf}m, >{thresh}){br_c}"
                elif ind == 'VWAP':
                    rule_desc = f"{br_o}VWAP({tf}m){br_c}"
                elif ind == 'ADVANCED':
                    op1 = r.get('operand1', 'N/A')
                    op2 = r.get('operand2', 'N/A')
                    sym = r.get('operator_sym', '?')
                    rule_desc = f"{br_o}{op1}{sym}{op2}({tf}m){br_c}"
                else:
                    rule_desc = f"{br_o}{ind}({tf}m){br_c}"

                if rule_desc:
                    parts.append(rule_desc)
                    if i < len(rules) - 1:
                        parts.append(r.get('operator', 'AND'))
            return " ".join(parts) if parts else "OFF"

        logger.info(f"║ BEGINNING ENTRY: {format_rules(rules_beg, is_entry=True):<51} ║")
        logger.info(f"║ RE-ENTRY GATES:  {format_rules(rules_re, is_entry=True):<51} ║")

        # 3. Profit/Exit Logic
        target_en = self._v3_cfg('profit_target_enabled', False, bool)
        target_val = self._v3_cfg('profit_target_pct', 0.0, float)
        decay_en = self._v3_cfg('ltp_decay_enabled', False, bool)
        decay_val = self._v3_cfg('ltp_exit_min', 0, int)
        ratio_en = self._v3_cfg('ratio_exit.enabled', False, bool)
        ratio_val = self._v3_cfg('ratio_exit.threshold', 0.0, float)
        smart_en = self._v3_cfg('smart_rolling_enabled', True, bool)
        logger.info(f"║ ROLLOVERS: Target:{'ON' if target_en else 'OFF'}({target_val}%) | Decay:{'ON' if decay_en else 'OFF'}({decay_val}) | Ratio:{'ON' if ratio_en else 'OFF'}({ratio_val}) ║")
        logger.info(f"║ SMART ROLLING: {'[ENABLED]' if smart_en else '[DISABLED]'}                                     ║")

        # 4. Trailing SL
        tsl_en = self._v3_cfg('tsl_scalable.enabled', False, bool)
        if tsl_en:
            bp = self._v3_cfg('tsl_scalable.base_profit', 0)
            bl = self._v3_cfg('tsl_scalable.base_lock', 0)
            sp = self._v3_cfg('tsl_scalable.step_profit', 0)
            sl = self._v3_cfg('tsl_scalable.step_lock', 0)
            logger.info(f"║ SCALABLE TSL: Base:{bp}/{bl} | Step:{sp}/{sl} (Rupees)              ║")
        else:
            logger.info(f"║ SCALABLE TSL: DISABLED                                               ║")

        # 5. Global Guardrails
        g_roc_en = self._v3_cfg('guardrail_roc.enabled', False, bool)
        g_roc_tf = self._v3_cfg('guardrail_roc.tf', 15, int)
        g_roc_target = self._v3_cfg('guardrail_roc.target', 0.0, float)
        g_roc_sl = self._v3_cfg('guardrail_roc.stoploss', 0.0, float)
        logger.info(f"║ GUARDRAILS: ROC Exit:{'ON' if g_roc_en else 'OFF'}({g_roc_tf}m, T:{g_roc_target}/SL:{g_roc_sl})            ║")

        g_pnl_en = self._v3_cfg('guardrail_pnl.enabled', False, bool)
        g_pnl_target = self._v3_cfg('guardrail_pnl.target_pts', 0.0, float)
        g_pnl_sl = self._v3_cfg('guardrail_pnl.stoploss_pts', 0.0, float)
        logger.info(f"║ PNL GUARDRAIL: {'[ENABLED]' if g_pnl_en else '[DISABLED]'} Target:{g_pnl_target:>4.1f} Pts | SL:{g_pnl_sl:>4.1f} Pts                 ║")

        st_target = self._v3_cfg('single_trade_target_pts', 0.0, float)
        st_sl = self._v3_cfg('single_trade_stoploss_pts', 0.0, float)
        logger.info(f"║ SINGLE TRADE: Target: {st_target:>4.1f} Pts | SL: {st_sl:>4.1f} Pts                                ║")

        # 6. Technical Exit Rules
        exit_rules = self._v3_cfg('exit_rules', [])
        logger.info(f"║ DYNAMIC EXITS: {format_rules(exit_rules, is_entry=False):<53} ║")

        # 7. Hardware Acceleration
        from .sell_v3.rust_bridge import RUST_AVAILABLE
        logger.info(f"║ ACCELERATION: Rust Core {'[ACTIVE]' if RUST_AVAILABLE else '[INACTIVE]'} | Zero-Stall I/O [ACTIVE]       ║")

        # 8. Limits & Safety
        max_trades = self._v3_cfg('max_trades_per_day', 0, int)
        logger.info(f"║ LIMITS: Max Daily Trades: {max_trades if max_trades > 0 else 'Unlimited':<35} ║")

        logger.info(f"╚══════════════════════════════════════════════════════════════════════╝")

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.active_trades = state.get('active_trades', {})

                    # Convert entry_time strings back to datetime objects
                    for side in self.active_trades:
                        et = self.active_trades[side].get('entry_time')
                        if isinstance(et, str):
                            try:
                                self.active_trades[side]['entry_time'] = datetime.datetime.fromisoformat(et)
                            except: pass

                    self.last_entry_bucket = state.get('last_entry_bucket')
                    self.session_min_vwap = state.get('session_min_vwap', float('inf'))
                    self.session_points_pnl = state.get('session_points_pnl', 0.0)
                    self.trades_completed_today = state.get('trades_completed_today', 0)
                    self.tsl_high_lock = state.get('tsl_high_lock', 0.0)
                    self.last_trade_locked_pnl = state.get('last_trade_locked_pnl', 0.0)
                    self.strangle_closed = state.get('strangle_closed', False)
                    self.workflow_phase = state.get('workflow_phase', 'BEGINNING')
                    logger.info(f"[SellManagerV3] State loaded for {self.instrument_name}. TSL High Lock: {self.tsl_high_lock}")

                    # Ensure active positions are fully reconstructed into objects
                    self.reconnect_positions()
            except Exception as e:
                logger.error(f"[SellManagerV3] Error loading state: {e}")

    def save_state(self):
        state = {
            'active_trades': self.active_trades,
            'last_entry_bucket': self.last_entry_bucket,
            'session_min_vwap': self.session_min_vwap,
            'session_points_pnl': self.session_points_pnl,
            'trades_completed_today': self.trades_completed_today,
            'tsl_high_lock': self.tsl_high_lock,
            'last_trade_locked_pnl': self.last_trade_locked_pnl,
            'strangle_closed': self.strangle_closed,
            'workflow_phase': self.workflow_phase
        }
        try:
            os.makedirs('config', exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(state, f, default=str)
        except Exception as e:
            logger.error(f"[SellManagerV3] Error saving state: {e}")

    async def on_tick(self, ticks, timestamp):
        """
        Processes a market tick by pre-caching configuration and delegating to
        entry/exit logic components.
        """
        if self.strangle_closed:
            return

        # PERFORMANCE: Pre-load and cache configuration for this tick evaluation
        # This eliminates repetitive nested dictionary lookups during rule evaluation
        self._tick_config_cache = {}
        # Pre-cache common keys
        for key in ['multi_strike_scan', 'strict_ltp_balancing', 'v_slope_pool_offset', 'reentry_offset', 'reentry_best_metric', 'entry_workflow_mode', 'smart_rolling_enabled', 'profit_target_enabled', 'ltp_decay_enabled']:
            self._tick_config_cache[key] = self._v3_cfg(key, timestamp=timestamp)

        # 0. EOD Square-off check
        close_time_str = self._v3_cfg('close_time', '15:20:00')
        try:
            if len(close_time_str.split(':')) == 2:
                close_time = datetime.datetime.strptime(close_time_str, '%H:%M').time()
            else:
                close_time = datetime.datetime.strptime(close_time_str, '%H:%M:%S').time()

            if timestamp.time() >= close_time:
                if self.active_trades:
                    await self._execute_full_exit(timestamp, "EOD Square-off", stop_for_day=True)
                self.strangle_closed = True
                self.save_state()
                return
        except: pass

        # 1. Log Throttling & Tick Cleanup
        now_ts = asyncio.get_event_loop().time()
        self.do_log = False

        # Performance: Periodic cache cleanup for high-frequency ticks
        if int(now_ts) % 3600 == 0: # Hourly cleanup
            if hasattr(self, '_tick_config_cache'): self._tick_config_cache = None
        if self.orchestrator.is_backtest:
            sim_now = int(timestamp.timestamp())
            if (sim_now - getattr(self, '_last_sim_log_time', 0) >= 60):
                self._last_sim_log_time = sim_now
                self.do_log = True
        else:
            if (now_ts - getattr(self, '_last_log_time', 0) >= 60):
                self._last_log_time = now_ts
                self.do_log = True

        # 2. Update Indicators & Dashboard
        # PERFORMANCE: Throttle dashboard updates in backtest to 1-second simulation resolution
        if self.orchestrator.is_backtest:
            last_bt_update = getattr(self, '_last_bt_indicator_update', None)
            should_update = (last_bt_update is None or (timestamp - last_bt_update).total_seconds() >= 1.0)
            if should_update: self._last_bt_indicator_update = timestamp
        else:
            should_update = (now_ts - getattr(self, '_last_indicator_update_time', 0) >= 2.0)

        if should_update:
            self._last_indicator_update_time = now_ts
            await self._update_session_indicators(timestamp)
            self.v3_dashboard_data = await self.dashboard_logic.get_v3_dashboard_data(timestamp)

        # 3. Check Exits
        exit_occurred = False
        if self.active_trades:
            before_count = len(self.active_trades)
            await self.exit_logic.check_exits(ticks, timestamp)
            if before_count > 0 and len(self.active_trades) == 0:
                exit_occurred = True

        # 4. Check Entries
        if not self.active_trades and not exit_occurred:
            await self.entry_logic.check_entry(ticks, timestamp)

        # 5. Live Mode Heartbeat (every minute)
        if not self.orchestrator.is_backtest:
            if (now_ts - getattr(self, '_last_v3_heartbeat_time', 0) >= 60.0):
                self._last_v3_heartbeat_time = now_ts
                status_msg = self.workflow_phase
                if self.entry_logic._is_in_priming_wait(timestamp):
                    status_msg = "PRIMING_WAIT"
                elif not self.active_trades:
                    status_msg = "SCANNING_POOL" if self.workflow_phase == 'CONTINUE' else "WAITING_FOR_BOUNDARY"

                # Check if we have data for indicators
                has_indicators = "OK" if self.v3_dashboard_data.get('combined_rsi') is not None else "NO_DATA"

                logger.info(f"[SellManagerV3] Heartbeat: Phase={self.workflow_phase} | Status={status_msg} | Indicators={has_indicators} | Spot={self.orchestrator.state_manager.index_price}")

    async def _update_session_indicators(self, timestamp):
        ce = self.active_trades.get('CE')
        pe = self.active_trades.get('PE')
        if ce and pe:
            key_ce, key_pe = ce['key'], pe['key']
        else:
            key_ce, key_pe, _ = await self.entry_logic._get_atm_keys(timestamp)

        if key_ce and key_pe:
            vwap_ce = await self.orchestrator.indicator_manager.calculate_vwap(key_ce, timestamp)
            vwap_pe = await self.orchestrator.indicator_manager.calculate_vwap(key_pe, timestamp)
            if vwap_ce and vwap_pe:
                combined_vwap = vwap_ce + vwap_pe
                if combined_vwap < self.session_min_vwap:
                    self.session_min_vwap = combined_vwap
                    if asyncio.get_event_loop().time() - getattr(self, 'last_save_time', 0) >= 60:
                        self.last_save_time = asyncio.get_event_loop().time()
                        self.save_state()

    async def _execute_straddle_entry(self, ce, pe, timestamp, reason, slope_data=None):
        # Calculate slopes for logging if not provided
        slope_str = ""
        if slope_data and slope_data[0] is not None:
            c_slope = slope_data[0]
            dir_str = "DECREASING" if c_slope <= 0 else "INCREASING"
            slope_str = f" [V-Slope: {c_slope:+.4f} ({dir_str})]"

        # Resolve symbols for clearer logging as requested by user
        # Explicitly include Strike and Key for verification
        ce_desc = f"{ce['strike']} CE ({ce['key']})"
        pe_desc = f"{pe['strike']} PE ({pe['key']})"

        logger.info(f"[SellManagerV3] Executing {reason}: {ce_desc} @ {ce['ltp']}, {pe_desc} @ {pe['ltp']}{slope_str}")

        # Initialize session min VWAP
        combined_vwap = (await self.orchestrator.indicator_manager.calculate_vwap(ce['key'], timestamp) or 0) + \
                        (await self.orchestrator.indicator_manager.calculate_vwap(pe['key'], timestamp) or 0)
        self.session_min_vwap = combined_vwap if combined_vwap > 0 else float('inf')

        expiry = self.orchestrator.atm_manager.get_expiry_by_mode('sell', 'signal')
        product_type = self._cfg('product_type', 'NRML')

        ce_contract = self.orchestrator.atm_manager.contract_lookup.get(expiry, {}).get(float(ce['strike']), {}).get('CE')
        pe_contract = self.orchestrator.atm_manager.contract_lookup.get(expiry, {}).get(float(pe['strike']), {}).get('PE')

        if not ce_contract or not pe_contract:
            logger.error(f"[SellManagerV3] Contract lookup failed for {ce['strike']} or {pe['strike']}")
            return

        tasks = []
        for broker in self.orchestrator.broker_manager.brokers.values():
            if not broker.is_configured_for_instrument(self.instrument_name): continue
            qty_mult = broker.config_manager.get_int(broker.instance_name, 'quantity', 1)
            tasks.append(self._place_order_on_broker(broker, ce_contract, 'SELL', qty_mult * ce_contract.lot_size, expiry, product_type))
            tasks.append(self._place_order_on_broker(broker, pe_contract, 'SELL', qty_mult * pe_contract.lot_size, expiry, product_type))

        results = []
        if tasks:
            results = await asyncio.gather(*tasks)

        # In Live Mode, if all orders failed, DO NOT mark the trade as active.
        if not self.orchestrator.is_backtest and tasks:
            success_count = len([r for r in results if r is not None])
            if success_count == 0:
                logger.error(f"[SellManagerV3] ABORTING ENTRY: All broker orders failed. Check API configuration/logs.")
                return

        # Record anchor price
        idx_p = self.orchestrator.state_manager.index_price
        fut_p = self.orchestrator.state_manager.spot_price
        anchor_price = fut_p if (self.orchestrator.is_mcx or not idx_p) else idx_p

        # Enrichment: Capture technical snapshot at entry
        v3_data = self.v3_dashboard_data
        entry_details = v3_data.get('entry_details', [])
        if entry_details:
            entry_snap = " | ".join([f"{d['label']}:{d['val']}" for d in entry_details])
        else:
            rsi = v3_data.get('combined_rsi')
            roc = v3_data.get('combined_roc')
            slope = v3_data.get('slope_status')
            entry_tf = self._v3_cfg('rsi_entry.tf', 1, int)
            entry_snap = f"RSI({entry_tf}m):{rsi:.1f}, ROC:{roc:.2f}, Slope:{slope}" if rsi is not None else "--"

        self.active_trades['CE'] = {
            'strike': ce['strike'], 'key': ce['key'], 'entry_price': ce['ltp'], 'entry_time': timestamp,
            'entry_index_price': anchor_price, 'lot_size': ce_contract.lot_size, 'contract': ce_contract,
            'reason': reason, 'entry_indicators': entry_snap
        }
        self.active_trades['PE'] = {
            'strike': pe['strike'], 'key': pe['key'], 'entry_price': pe['ltp'], 'entry_time': timestamp,
            'entry_index_price': anchor_price, 'lot_size': pe_contract.lot_size, 'contract': pe_contract,
            'reason': reason, 'entry_indicators': entry_snap
        }

        for session in self.orchestrator.user_sessions.values():
            session.state_manager.trade_count += 1

        self.workflow_phase = 'CONTINUE'
        tf = self._v3_cfg('rsi_entry.tf', self._v3_cfg('rsi.tf', 1, int), int)
        self.last_entry_bucket = str(timestamp.replace(minute=(timestamp.minute // tf) * tf, second=0, microsecond=0))
        self.save_state()

        # Telegram trade-entry notification — fire in background thread (non-blocking, live only)
        try:
            client_id_env = os.environ.get('CLIENT_ID')
            trading_mode_env = os.environ.get('CLIENT_TRADING_MODE', 'PAPER').upper()
            if client_id_env and trading_mode_env == 'LIVE':
                from web.db import db_fetchone as _db_fetchone
                client_row = _db_fetchone(
                    "SELECT telegram_chat_id FROM users WHERE id=?", (int(client_id_env),)
                )
                if client_row and client_row.get("telegram_chat_id"):
                    import threading
                    from utils.notifier import notify_trade_entry
                    _chat_id = client_row["telegram_chat_id"]
                    _e_data = {
                        'ce_strike': ce['strike'],
                        'pe_strike': pe['strike'],
                        'ce_price': ce['ltp'],
                        'pe_price': pe['ltp'],
                        'instrument': self.instrument_name,
                        'broker': os.environ.get('CLIENT_BROKER', 'V3'),
                        'reason': reason,
                    }
                    threading.Thread(
                        target=notify_trade_entry, args=(_chat_id, _e_data), daemon=True
                    ).start()
        except Exception as _te:
            logger.error(f"[SellManagerV3] Telegram entry notify failed: {_te}")

    async def _execute_full_exit(self, timestamp, reason, cooldown=True, stop_for_day=False):
        logger.info(f"[SellManagerV3] Full Exit: {reason}")
        tasks = []
        expiry = self.orchestrator.atm_manager.get_expiry_by_mode('sell', 'signal')
        product_type = self._cfg('product_type', 'NRML')

        try:
            ref_broker = next(iter(self.orchestrator.broker_manager.brokers.values()), None)
            mult = ref_broker.config_manager.get_int(ref_broker.instance_name, 'quantity', 1) if ref_broker else 1
        except: mult = 1

        total_pnl = 0.0
        _tg_notification_data = []  # collect per-trade data; dispatched after orders are awaited

        # Enrichment: Capture technical snapshot at exit
        v3_data = self.v3_dashboard_data
        exit_details = v3_data.get('exit_details', [])
        if exit_details:
            exit_snap = " | ".join([f"{d['label']}:{d['val']}" for d in exit_details])
        else:
            m_rsi = v3_data.get('macro_rsi')
            m_vwap = "FAIL" if v3_data.get('macro_vwap_fail') else "OK"
            g_roc = v3_data.get('guardrail_roc')
            g_roc_tf = self._v3_cfg('guardrail_roc.tf', 15, int)
            exit_snap = f"RSI:{m_rsi or '--'}, VWAP:{m_vwap}, ROC({g_roc_tf}m):{g_roc or '--'}"

        for side in ['CE', 'PE']:
            trade = self.active_trades.get(side)
            if not trade or not trade.get('contract'): continue

            for broker in self.orchestrator.broker_manager.brokers.values():
                if broker.is_configured_for_instrument(self.instrument_name):
                    qty = broker.config_manager.get_int(broker.instance_name, 'quantity', 1) * trade['lot_size']
                    tasks.append(self._place_order_on_broker(broker, trade['contract'], 'BUY', qty, expiry, product_type))

            exit_price = self.orchestrator.state_manager.option_prices.get(trade['key']) or trade['entry_price']
            pnl_pts = trade['entry_price'] - exit_price
            pnl_rs = pnl_pts * trade['lot_size'] * mult
            total_pnl += pnl_rs

            if getattr(self.orchestrator, 'trade_log', None):
                self.orchestrator.trade_log.add(LiveTradeLog.make_entry(
                    'SELL', side, trade['strike'], trade['entry_price'], exit_price,
                    pnl_pts, pnl_rs, reason, '', timestamp, trade.get('entry_index_price'),
                    entry_indicators=trade.get('entry_indicators'),
                    exit_indicators=exit_snap,
                    entry_time=trade.get('entry_time')
                ))

            if self.orchestrator.pnl_tracker:
                self.orchestrator.pnl_tracker.trade_history.append({
                    'instrument_key': trade['key'], 'entry_price': trade['entry_price'], 'exit_price': exit_price,
                    'entry_timestamp': trade['entry_time'], 'exit_timestamp': timestamp, 'pnl': pnl_rs,
                    'lot_size': trade['lot_size'], 'quantity': mult, 'status': 'CLOSED', 'side': side,
                    'strike_price': trade['strike'], 'contract': trade['contract'], 'exit_reason': reason, 'entry_type': 'SELL'
                })

            # DB persistence
            client_id, inst_id = os.environ.get('CLIENT_ID'), os.environ.get('CLIENT_INSTANCE_ID')
            if client_id and inst_id:
                try:
                    from web.db import db_execute
                    db_execute("""
                        INSERT INTO trade_history (instance_id, client_id, trade_type, direction, strike, entry_price, exit_price, pnl_pts, pnl_rs, quantity, broker, exit_reason, instrument, trading_mode, opened_at, entry_index_price, closed_at, entry_indicators, exit_indicators)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (int(inst_id), int(client_id), 'SELL', side, float(trade['strike']), float(trade['entry_price']), float(exit_price), float(pnl_pts), float(pnl_rs), int(trade['lot_size'] * mult), 'V3', reason, self.instrument_name, os.environ.get('CLIENT_TRADING_MODE', 'PAPER').upper(), trade['entry_time'].isoformat(), float(trade.get('entry_index_price', 0) or 0), timestamp.isoformat(), str(trade.get('entry_indicators', '--')), str(exit_snap)))
                except Exception as e: logger.error(f"[SellManagerV3] DB Persistence failed: {e}")

                # Collect Telegram data (dispatched after broker orders are awaited)
                try:
                    trading_mode = os.environ.get('CLIENT_TRADING_MODE', 'PAPER').upper()
                    if trading_mode == 'LIVE':
                        _tg_notification_data.append({
                            'direction': side,
                            'pnl_pts': float(pnl_pts),
                            'pnl_rs': float(pnl_rs),
                            'exit_reason': reason,
                            'broker': os.environ.get('CLIENT_BROKER', 'V3'),
                            'trading_mode': trading_mode,
                            'instrument': self.instrument_name,
                            '_client_id': int(client_id),
                            '_stop_for_day': stop_for_day,
                            'strike': float(trade.get('strike', 0)),
                            'exit_price': float(exit_price),
                            'lots': int(trade.get('lot_size', 1) * mult),
                        })
                except Exception as _te:
                    logger.error(f"[SellManagerV3] Telegram data collection failed: {_te}")

        for session in self.orchestrator.user_sessions.values():
            session.state_manager.total_pnl += total_pnl

        # Track cumulative session points for Guardrails
        ref_trade = next(iter(self.active_trades.values())) if self.active_trades else None
        ls = ref_trade.get('lot_size') if ref_trade else None
        self.session_points_pnl += (total_pnl / (ls * mult)) if (ls and mult) else 0.0

        # Increment Trade Limit counter (Only on full exits, not rollovers)
        if cooldown:
            self.trades_completed_today += 1

        if tasks: await asyncio.gather(*tasks)
        self.active_trades = {}

        # Fire Telegram notifications AFTER orders are dispatched (non-blocking threads)
        if _tg_notification_data:
            try:
                from web.db import db_fetchone as _db_fetchone
                client_row = _db_fetchone(
                    "SELECT telegram_chat_id FROM users WHERE id=?",
                    (_tg_notification_data[0]['_client_id'],)
                )
                if client_row and client_row.get("telegram_chat_id"):
                    import threading
                    _chat_id = client_row["telegram_chat_id"]
                    # Use explicit stop_for_day flag (all EOD/kill-switch callers set this)
                    is_squareoff = bool(_tg_notification_data[0].get('_stop_for_day'))
                    if is_squareoff:
                        from utils.notifier import notify_squareoff
                        _ce = next((t for t in _tg_notification_data if t['direction'] == 'CE'), {})
                        _pe = next((t for t in _tg_notification_data if t['direction'] == 'PE'), {})
                        _sq = {
                            'instrument': _tg_notification_data[0]['instrument'],
                            'broker': _tg_notification_data[0]['broker'],
                            'reason': reason,
                            'total_pnl_rs': sum(t['pnl_rs'] for t in _tg_notification_data),
                            'total_pnl_pts': sum(t['pnl_pts'] for t in _tg_notification_data),
                            'ce_strike': _ce.get('strike', '—'),
                            'ce_exit_price': _ce.get('exit_price', 0),
                            'pe_strike': _pe.get('strike', '—'),
                            'pe_exit_price': _pe.get('exit_price', 0),
                            'lots': _tg_notification_data[0].get('lots', 1),
                        }
                        threading.Thread(target=notify_squareoff, args=(_chat_id, _sq), daemon=True).start()
                    else:
                        from utils.notifier import notify_trade
                        for _td in _tg_notification_data:
                            threading.Thread(target=notify_trade, args=(_chat_id, _td), daemon=True).start()
            except Exception as _te:
                logger.error(f"[SellManagerV3] Telegram post-exit notify failed: {_te}")
        self.session_min_vwap = float('inf')
        # Reset Trailing SL Lock, but preserve for UI visibility
        if self.tsl_high_lock > 0:
            self.last_trade_locked_pnl = self.tsl_high_lock
        self.tsl_high_lock = 0.0
        if cooldown: self._last_exit_timestamp = timestamp
        if stop_for_day:
             self.strangle_closed = True
             logger.info(f"[SellManagerV3] STOP_FOR_DAY triggered by {reason}")
        self.save_state()

    async def _place_order_on_broker(self, broker, contract, trans_type, qty, expiry, product_type):
        """
        Generic order placement wrapper that handles different broker types
        and respects the unified BaseBroker interface.
        """
        if self.orchestrator.is_backtest or getattr(broker, 'paper_trade', False):
            return "BACKTEST_ORDER_ID"

        try:
            # Zerodha specific parameter
            m_prot = self._v3_cfg('zerodha_market_protection', None)

            # Use to_thread for blocking SDK calls inside the broker's place_order
            return await asyncio.to_thread(
                broker.place_order,
                contract,
                trans_type,
                qty,
                expiry,
                product_type=product_type,
                market_protection=m_prot
            )
        except Exception as e:
            logger.error(f"[SellManagerV3] Order placement failed on {broker.instance_name}: {e}", exc_info=True)
        return None

    async def close_all(self, timestamp):
        if self.active_trades: await self._execute_full_exit(timestamp, "EOD Square-off", stop_for_day=True)

    def reconnect_positions(self):
        if not self.active_trades: return
        expiry = self.orchestrator.atm_manager.signal_expiry_date
        if not expiry: return

        # Ensure expiry is a date object for lookup
        expiry_date = expiry.date() if isinstance(expiry, datetime.datetime) else expiry

        for side, trade in self.active_trades.items():
            strike = float(trade['strike'])
            contract = self.orchestrator.atm_manager.contract_lookup.get(expiry_date, {}).get(strike, {}).get(side)
            if contract:
                trade['contract'] = contract
                logger.info(f"[SellManagerV3] Reconnected {side} {strike} position.")
            else: logger.error(f"[SellManagerV3] Failed to reconnect {side} {strike} position!")
