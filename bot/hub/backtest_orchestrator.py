import asyncio
from .base_orchestrator import BaseOrchestrator
from utils.logger import logger
import pandas as pd
from .backtest_data_manager import BacktestDataManager

class BacktestOrchestrator(BaseOrchestrator):
    def __init__(self, *args, **kwargs):
        kwargs['is_backtest'] = True
        super().__init__(*args, **kwargs)
        self.current_timestamp = None
        self.index_instrument_key = self.config_manager.get(self.instrument_name, 'instrument_symbol')
        self.backtest_data_mgr = BacktestDataManager(self)
        v3_mode = self.json_config.get_value(f"{self.instrument_name}.v3_mode")
        if str(v3_mode).lower() == 'true':
            from hub.sell_manager_v3 import SellManagerV3
            self.sell_manager = SellManagerV3(self)
            logger.info(f"[{self.instrument_name}] Backtest: SellManagerV3 initialized.")
        else:
            from hub.sell_manager import SellManager
            self.sell_manager = SellManager(self)
        self._backtest_strangle_triggered = False
        self.profit_target_hit = False

        from hub.status_writer import StatusWriter
        self.status_writer = StatusWriter(self)
        self._backtest_tick_count = 0

    async def prepare_backtest(self):
        """Pre-fetches all necessary data before starting the backtest."""
        self.json_config.load()
        if hasattr(self.websocket, 'backtest_date') and self.websocket.backtest_date:
            backtest_date = self.websocket.backtest_date
            backtest_date_str = backtest_date.strftime('%Y-%m-%d')
            await self.data_manager.load_contracts()
            self.atm_manager.all_contracts = self.data_manager.all_options
            self.atm_manager.near_expiry_date = self.data_manager.near_expiry_date
            self.atm_manager.monthly_expiries = self.data_manager.monthly_expiries
            self.atm_manager._build_contract_lookup_table()
            self.atm_manager.set_ready()
            self.atm_manager._determine_expiries(backtest_date)
            await self.backtest_data_mgr.pre_fetch_underlying_data(backtest_date_str)

            # Warm up DataManager's OHLC cache for indicators (RSI/ROC/VWAP)
            if self.index_instrument_key and self.backtest_data_mgr._index_ohlc_cache is not None:
                self.data_manager.backtest_ohlc_data[self.index_instrument_key] = self.backtest_data_mgr._index_ohlc_cache
            if self.futures_instrument_key and self.backtest_data_mgr._futures_ohlc_cache is not None:
                self.data_manager.backtest_ohlc_data[self.futures_instrument_key] = self.backtest_data_mgr._futures_ohlc_cache

            import os, csv
            if not hasattr(self.state_manager, 'atp_history'):
                self.state_manager.atp_history = {}
            if not hasattr(self.state_manager, 'ltp_history'):
                self.state_manager.ltp_history = {}
            if not hasattr(self.state_manager, 'rsi_history'):
                self.state_manager.rsi_history = {}
            if not hasattr(self.state_manager, 'roc_history'):
                self.state_manager.roc_history = {}

            atp_candidates = [
                os.path.join(os.getcwd(), f"atp_data_{self.instrument_name}_{backtest_date_str}.csv"),
                os.path.join(os.getcwd(), "Selling_Using_C", f"atp_data_{self.instrument_name}_{backtest_date_str}.csv"),
                os.path.join(os.path.dirname(os.getcwd()), "Selling_Using_C", f"atp_data_{self.instrument_name}_{backtest_date_str}.csv")
            ]
            atp_file = next((c for c in atp_candidates if os.path.exists(c)), None)
            if atp_file:
                atp_df = pd.read_csv(atp_file)
                atp_df['minute_ts'] = pd.to_datetime(atp_df['minute_ts'], utc=True).dt.tz_convert('Asia/Kolkata')

                # Group by instrument to inject high-fidelity OHLC data
                for ikey, group in atp_df.groupby('instrument_key'):
                    # Cache technical metrics in StateManager history for exact minute lookups
                    if 'atp' in group.columns:
                        self.state_manager.atp_history[ikey] = group.set_index('minute_ts')['atp'].dropna().to_dict()
                    if 'ltp' in group.columns:
                        self.state_manager.ltp_history[ikey] = group.set_index('minute_ts')['ltp'].dropna().to_dict()
                    if 'rsi' in group.columns:
                        self.state_manager.rsi_history[ikey] = group.set_index('minute_ts')['rsi'].dropna().to_dict()
                    if 'roc' in group.columns:
                        self.state_manager.roc_history[ikey] = group.set_index('minute_ts')['roc'].dropna().to_dict()

                    # Inject as synthetic OHLC into DataManager to support resampling/fallback
                    # VWAP = ATP in these datasets. We create a minimal 1m OHLC where Open/High/Low/Close = LTP
                    # and we inject a dummy volume so VWAP calculation works.
                    ohlc_synth = pd.DataFrame({
                        'open': group['ltp'], 'high': group['ltp'], 'low': group['ltp'], 'close': group['ltp'],
                        'volume': 1.0 # Ensures (sum(Price*Vol)/sum(Vol)) works
                    }, index=group['minute_ts']).dropna()

                    # Deduplicate in case the CSV has multiple entries per minute
                    ohlc_synth = ohlc_synth[~ohlc_synth.index.duplicated(keep='last')].sort_index()

                    # If ATP is present, we can use it to force calculate_vwap to be extremely accurate
                    self.data_manager.backtest_ohlc_data[ikey] = ohlc_synth

                logger.info(f"[Backtest] Loaded high-fidelity data for {len(atp_df['instrument_key'].unique())} instruments from {atp_file}")
            else:
                logger.warning(f"[Backtest] ATP file not found: {atp_file}. Slope will use synthetic OHLC VWAP.")
        else:
            logger.error("Backtest date not found. Cannot pre-fetch data.")

    def _get_timestamp(self):
        return self.current_timestamp

    def _is_trade_active(self):
        v2_active = self.pnl_tracker and self.pnl_tracker.is_trade_active()
        # V3 active check
        v3_active = False
        if hasattr(self, 'sell_manager'):
            # Use strangle_placed if available, fallback to ce_placed
            v3_active = getattr(self.sell_manager, 'strangle_placed',
                                getattr(self.sell_manager, 'ce_placed', False))
        return v2_active or v3_active

    async def run_backtest_strategy_for_timestamp(self, timestamp, current_group):
        # PERFORMANCE: Throttle backtest strategy evaluation to 1-second resolution
        # Most tick files have multiple sub-second updates. Strategy logic doesn't need to run that fast.
        last_eval_ts = getattr(self, '_last_bt_eval_ts', None)
        if last_eval_ts and (timestamp - last_eval_ts).total_seconds() < 1.0:
            return
        self._last_bt_eval_ts = timestamp

        self.current_timestamp = timestamp

        # PERFORMANCE: Throttle StatusWriter in backtest (sim clock)
        # 1-minute simulated throttle
        l_status_min = getattr(self, '_last_bt_status_min', None)
        bucket_min = timestamp.replace(second=0, microsecond=0)
        if l_status_min is None or l_status_min != bucket_min:
             self._last_bt_status_min = bucket_min
             self.status_writer.maybe_write(timestamp, self.atm_manager.strikes.get('atm'))

        # Manually feed underlyings to aggregators if they exist in current_group
        aggregators = [self.entry_aggregator, self.exit_aggregator, self.one_min_aggregator, self.five_min_aggregator]
        if not current_group.empty:
            spot = current_group['spot_price'].iloc[0] if 'spot_price' in current_group.columns else 0
            idx = current_group['index_price'].iloc[0] if 'index_price' in current_group.columns else 0
            if spot > 0:
                for agg in aggregators: agg.add_tick(self.futures_instrument_key, spot, timestamp)
            if idx > 0:
                for agg in aggregators: agg.add_tick(self.index_instrument_key, idx, timestamp)

            # Feed option prices from the CSV row to the aggregators
            for _, row in current_group.iterrows():
                strike = row.get('strike_price')
                if strike:
                    # FORCE RE-FETCH CONTRACTS IF EXPIRY DATE IS IN THE PAST COMPARED TO TICK
                    if self.atm_manager.signal_expiry_date and timestamp.date() > self.atm_manager.signal_expiry_date:
                         self.atm_manager._determine_expiries(timestamp.date())

                    # In backtest, resolve instrument keys using the date of the tick if needed
                    # but signal_expiry_date is already set in prepare_backtest.
                    ce_k = self.atm_manager.find_instrument_key_by_strike(strike, 'CALL', self.atm_manager.signal_expiry_date)
                    pe_k = self.atm_manager.find_instrument_key_by_strike(strike, 'PUT', self.atm_manager.signal_expiry_date)
                    if ce_k and row.get('ce_ltp'):
                        for agg in aggregators: agg.add_tick(ce_k, float(row['ce_ltp']), timestamp)
                    if pe_k and row.get('pe_ltp'):
                        for agg in aggregators: agg.add_tick(pe_k, float(row['pe_ltp']), timestamp)

        await self._populate_state_for_tick(timestamp, current_group)

        # Sell Side Strategy
        from hub.sell_manager_v3 import SellManagerV3
        if isinstance(self.sell_manager, SellManagerV3):
            # V3 uses integrated on_tick
            sell_ticks = {k: {'ltp': v} for k, v in self.state_manager.option_prices.items() if v}
            await self.sell_manager.on_tick(sell_ticks, timestamp)
        else:
            # Legacy Sell Side
            import datetime as _dt
            _start_str = self.config_manager.get('settings', 'strangle_start_time', fallback='09:16:00')
            try:
                if len(_start_str.split(':')) == 2:
                    _strangle_start = _dt.datetime.strptime(_start_str, '%H:%M').time()
                else:
                    _strangle_start = _dt.datetime.strptime(_start_str, '%H:%M:%S').time()
            except Exception as e:
                logger.warning(f"[Backtest] Invalid strangle_start_time '{_start_str}': {e}. Using 09:16:00")
                _strangle_start = _dt.time(9, 16)
            if not self._backtest_strangle_triggered and timestamp.time() >= _strangle_start:
                await self.sell_manager.build_candidates_for_all_sides(timestamp)
                await self.sell_manager.execute_short_strangle_backtest(timestamp)
                self._backtest_strangle_triggered = True

        self.orchestrator_state.v2_target_strike_pair = self.strike_manager.find_and_get_target_strike_pair(
            expiry=self.atm_manager.signal_expiry_date
        )
        if self.orchestrator_state.v2_target_strike_pair:
             target_strike = self.orchestrator_state.v2_target_strike_pair['strike']
             self.state_manager.target_strike = target_strike
             for session in self.user_sessions.values():
                 session.state_manager.target_strike = target_strike

        # Feeding aggregators
        aggregators = [self.entry_aggregator, self.exit_aggregator, self.one_min_aggregator, self.five_min_aggregator]
        if self.index_instrument_key and self.state_manager.index_price:
            for agg in aggregators: agg.add_tick(self.index_instrument_key, self.state_manager.index_price, timestamp)
        if self.futures_instrument_key and self.state_manager.spot_price:
            for agg in aggregators: agg.add_tick(self.futures_instrument_key, self.state_manager.spot_price, timestamp)

        keys_needing_ticks = self._get_keys_needing_ticks()
        # Ensure underlyings are included
        if self.index_instrument_key: keys_needing_ticks.add(self.index_instrument_key)
        if self.futures_instrument_key: keys_needing_ticks.add(self.futures_instrument_key)

        # Update StateManager and Aggregators using the most accurate available data
        # STRICT: We use prices from 'current_group' if available, otherwise last known price.
        # This prevents look-ahead bias by NOT fetching the 'close' of the current minute's candle.
        tick_map = {}
        for _, row in current_group.iterrows():
            strike = row.get('strike_price')
            if strike:
                ck = self.atm_manager.find_instrument_key_by_strike(strike, 'CALL', self.atm_manager.signal_expiry_date)
                pk = self.atm_manager.find_instrument_key_by_strike(strike, 'PUT', self.atm_manager.signal_expiry_date)
                if ck and pd.notna(row.get('ce_ltp')): tick_map[ck] = float(row['ce_ltp'])
                if pk and pd.notna(row.get('pe_ltp')): tick_map[pk] = float(row['pe_ltp'])

        for inst_key in keys_needing_ticks:
            ltp = tick_map.get(inst_key)
            if ltp is None:
                # If not in current group, use last known price from state or fallback to history (without current candle)
                ltp = self.state_manager.option_prices.get(inst_key)
                if ltp is None:
                    ltp = await self._get_ltp_for_backtest_instrument(inst_key, timestamp)

            if ltp is not None:
                self.state_manager.option_prices[inst_key] = ltp
                for agg in aggregators:
                    agg.add_tick(inst_key, ltp, timestamp)

        if current_group.duplicated(subset='strike_price').any():
            current_group = current_group.drop_duplicates(subset='strike_price', keep='first')

        current_data_for_logic = self.state_manager.option_data
        current_atm = self.atm_manager.strikes.get('atm')

        # PERFORMANCE: Update global PnL once per tick instead of per-session
        if self.pnl_tracker.is_trade_active():
             await self._update_pnl_for_active_trades(timestamp)

        summary_extra_info = ""
        is_min_boundary = (timestamp.second == 0)
        for session in self.user_sessions.values():
            # PERFORMANCE: Only check crossover breach at minute boundaries during backtests
            if is_min_boundary:
                await session.signal_monitor.check_crossover_breach(timestamp=timestamp, current_atm=current_atm)
            if session.is_in_trade():
                pos = session.state_manager.call_position or session.state_manager.put_position
                await session.manage_active_trades(timestamp=timestamp, current_ticks=current_data_for_logic, current_atm=current_atm)
                if pos and not summary_extra_info:
                    summary_extra_info = self._get_summary_extra(pos)

        from utils.logger import log_trade_summary
        pnl = self.pnl_tracker.get_real_time_pnl() if self.pnl_tracker else 0

        # Include V3 Open P&L in the summary
        if hasattr(self, 'sell_manager') and hasattr(self.sell_manager, 'active_trades'):
            v3_open_pnl = 0
            # Get quantity multiplier from first active broker
            try:
                ref_broker = next(iter(self.broker_manager.brokers.values()), None)
                v3_qty_mult = (ref_broker.config_manager.get_int(
                    ref_broker.instance_name, 'quantity', 1) if ref_broker else 1)
            except: v3_qty_mult = 1

            for side, trade in self.sell_manager.active_trades.items():
                ltp = self.state_manager.option_prices.get(trade['key'])
                # BACKTEST ROBUSTNESS: If current second's tick is missing, the above is already
                # the last known price. We only fallback to entry price if no price was EVER seen.
                if ltp:
                    pnl_pts = trade['entry_price'] - ltp
                    v3_open_pnl += pnl_pts * trade['lot_size'] * v3_qty_mult
                else:
                    # If still no price, we use entry price (0 PnL) to avoid "incomplete" jumps
                    # but log a warning if it happens during an active trade
                    v3_open_pnl += 0
            pnl += v3_open_pnl

        # --- Profit target exit check ---
        if not self.profit_target_hit and self.pnl_tracker:
            pt_cfg = (self.json_config.get_value(
                f"{self.instrument_name}.profit_target_exit") or {})
            if pt_cfg.get('enabled'):
                realized = sum(t.get('pnl', 0) for t in self.pnl_tracker.trade_history)
                total_pnl = realized + pnl
                lot = self.config_manager.get_int(self.instrument_name, 'lot_size', 1)
                threshold = pt_cfg.get('points', 60) * lot
                if total_pnl >= threshold:
                    logger.info(
                        f"[{self.instrument_name}] PROFIT TARGET HIT: "
                        f"₹{total_pnl:.2f} >= ₹{threshold:.2f} "
                        f"({pt_cfg['points']} pts × lot={lot}). "
                        f"Closing all positions — done for today."
                    )
                    self.profit_target_hit = True
                    await self.close_open_backtest_positions(timestamp)

        # Realized PnL
        realized_pnl = sum(t.get('pnl', 0) for t in self.pnl_tracker.trade_history) if self.pnl_tracker else 0
        total_pnl = realized_pnl + pnl

        v3_trade_pnl_str = ""
        if hasattr(self, 'sell_manager') and hasattr(self.sell_manager, 'v3_dashboard_data'):
            t_pnl = self.sell_manager.v3_dashboard_data.get('trade_pnl_rs', 0)
            if t_pnl != 0:
                v3_trade_pnl_str = f" | Trade P&L: {t_pnl:.2f}"

        log_trade_summary(f"Timestamp: {timestamp} | ATM: {current_atm} | Status: {'RUNNING' if self._is_trade_active() else 'IDLE'} | P&L: {total_pnl:.2f}{v3_trade_pnl_str}{summary_extra_info}")

        # --- Update Live UI Status ---
        self._backtest_tick_count += 1

    def _get_keys_needing_ticks(self):
        keys = set()
        if self.orchestrator_state.v2_target_strike_pair:
            ts = self.orchestrator_state.v2_target_strike_pair['strike']
            exp = self.atm_manager.signal_expiry_date
            for s in ['CALL', 'PUT']:
                k = self.atm_manager.find_instrument_key_by_strike(ts, s, exp)
                if k: keys.add(k)
        for session in self.user_sessions.values():
            for p in [session.state_manager.call_position, session.state_manager.put_position]:
                if p:
                    if p.get('instrument_key'): keys.add(p['instrument_key'])
                    ms = p.get('s1_monitoring_strike')
                    if ms:
                        sd = 'CE' if p.get('direction') == 'CALL' else 'PE'
                        mk = self.atm_manager.find_instrument_key_by_strike(ms, sd, self.atm_manager.signal_expiry_date)
                        if mk: keys.add(mk)

        # V3 Active trade keys
        if hasattr(self, 'sell_manager') and hasattr(self.sell_manager, 'active_trades'):
            for trade in self.sell_manager.active_trades.values():
                if trade.get('key'):
                    keys.add(trade['key'])
        return keys

    def _get_summary_extra(self, pos):
        info = ""
        mode = 'buy' if pos.get('entry_type') == 'BUY' else 'sell'
        try:
            exit_formula = self.json_config.get_value(f"{self.instrument_name}.{mode}.exit_formula") or ''
        except Exception:
            exit_formula = ''
        f_lower = exit_formula.lower()
        sr_indicators = ['s1_low', 'r1_high', 's1_double_drop', 'r1_falling', 'r1_low_breach', 's1_confirm']

        tgt = pos.get('current_target')
        if tgt: info += f" | TARGET: {float(tgt):.1f}"
        tf = pos.get('active_s1_tf', 'N/A')
        s1 = pos.get('active_s1', 0)
        tsl = pos.get('trailing_sl', 0)
        if s1 and any(ind in f_lower for ind in sr_indicators):
            info += f" | {pos.get('s1_label', 'S1LOW')}({tf}m): {float(s1):.1f}"
        if tsl and any(ind in f_lower for ind in ['tsl', 'atr_tsl']):
            info += f" | TSL: {float(tsl):.1f}"
        return info

    async def _check_backtest_exit_conditions(self, timestamp):
        """Deprecated in V2: Exit logic is now handled per-session in run_backtest_strategy_for_timestamp."""
        pass

    async def _update_pnl_for_active_trades(self, timestamp):
        """Update PnL for active trades using fast synchronous LTP lookups."""
        for side in ['CALL', 'PUT']:
            trade = self.pnl_tracker.active_call_trade if side == 'CALL' else self.pnl_tracker.active_put_trade
            if trade:
                # Use sync fast-path for LTP
                ltp = self._get_ltp_for_backtest_instrument_sync(trade['instrument_key'], timestamp)
                if ltp is not None:
                    self.pnl_tracker.update_pnl(side, ltp, timestamp)
                    # Sync to session states
                    for session in self.user_sessions.values():
                        pos = session.state_manager.call_position if side == 'CALL' else session.state_manager.put_position
                        if pos:
                            pos['ltp'] = ltp
                            pos['pnl'] = trade.get('pnl', 0)

    def _get_ltp_for_backtest_instrument_sync(self, instrument_key, timestamp):
        """Ultra-fast synchronous LTP lookup for backtests."""
        bucket_ts = timestamp.replace(second=0, microsecond=0)
        if bucket_ts.tzinfo is None:
            import pytz
            bucket_ts = pytz.timezone('Asia/Kolkata').localize(bucket_ts)

        # 1. Try high-fidelity LTP from ATP file first
        ltp_hist = getattr(self.state_manager, 'ltp_history', {}).get(instrument_key, {})
        if ltp_hist:
            val = ltp_hist.get(bucket_ts)
            if val is not None: return float(val)

        # 2. Fallback to OHLC synthetic data injected by prepare_backtest
        ohlc_df = self.data_manager.backtest_ohlc_data.get(instrument_key)
        if ohlc_df is not None and not ohlc_df.empty:
            # Use index lookup instead of slicing for speed
            if bucket_ts in ohlc_df.index:
                return float(ohlc_df.at[bucket_ts, 'close'])

        return self.state_manager.option_prices.get(instrument_key)

    async def _get_ltp_for_backtest_instrument(self, instrument_key, timestamp):
        """Async wrapper for backward compatibility, preferring sync fast path."""
        return self._get_ltp_for_backtest_instrument_sync(instrument_key, timestamp)

    def _get_oi_for_backtest_instrument_sync(self, instrument_key, timestamp):
        """Ultra-fast synchronous OI lookup for backtests."""
        bucket_ts = timestamp.replace(second=0, microsecond=0)
        if bucket_ts.tzinfo is None:
            import pytz
            bucket_ts = pytz.timezone('Asia/Kolkata').localize(bucket_ts)

        oi_hist = getattr(self.state_manager, 'roc_history', {}) # BacktestOrchestrator uses roc_history key for OI from ATP
        # Actually check where OI was stored in prepare_backtest.
        # Looking at prepare_backtest:
        # self.state_manager.roc_history[ikey] = group.set_index('minute_ts')['roc'].dropna().to_dict()
        # Wait, prepare_backtest doesn't seem to load OI explicitly into a separate dict?
        # It uses 'rsi', 'roc', 'atp', 'ltp'.

        # Fallback to option_oi if present
        return self.state_manager.option_oi.get(instrument_key)

    async def _get_oi_for_backtest_instrument(self, instrument_key, timestamp):
        return self._get_oi_for_backtest_instrument_sync(instrument_key, timestamp)

    async def _populate_state_for_tick(self, timestamp, tick_df):
        if tick_df.empty: return
        fut_p = tick_df['spot_price'].iloc[0] if 'spot_price' in tick_df.columns and pd.notna(tick_df['spot_price'].iloc[0]) else 0
        if not fut_p: fut_p = self.backtest_data_mgr.get_futures_price(timestamp) or 0
        idx_p = tick_df['index_price'].iloc[0] if 'index_price' in tick_df.columns and pd.notna(tick_df['index_price'].iloc[0]) else 0
        if not idx_p: idx_p = self.backtest_data_mgr.get_index_price(timestamp) or fut_p

        self.state_manager.timestamp = timestamp
        self.state_manager.spot_price = fut_p
        self.state_manager.index_price = idx_p
        self.state_manager.option_data.clear()

        await self.atm_manager.update_strikes_and_subscribe(fut_p)
        atm = self.atm_manager.strikes.get('atm')
        self.state_manager.atm_strike = atm

        watchlist = set(self.strike_manager.get_strike_watchlist(atm))

        v3_on = self.json_config.get_value(f"{self.instrument_name}.v3_mode")
        if str(v3_on).lower() == 'true':
            interval = self.config_manager.get_int(self.instrument_name, 'strike_interval', 50)
            for i in range(-10, 11): watchlist.add(float(atm + i * interval))
            if hasattr(self, 'sell_manager') and hasattr(self.sell_manager, 'active_trades'):
                for trade in self.sell_manager.active_trades.values():
                    if trade.get('strike'): watchlist.add(float(trade['strike']))

        for session in self.user_sessions.values():
            if session.state_manager.dual_sr_monitoring_data: watchlist.add(float(session.state_manager.dual_sr_monitoring_data['target_strike']))
            for p in [session.state_manager.call_position, session.state_manager.put_position]:
                if p:
                    for k in ['strike_price', 'signal_strike', 's1_monitoring_strike', 'exit_monitoring_strike']:
                        if p.get(k): watchlist.add(float(p[k]))

        # PERFORMANCE: Vectorize Tick Data Lookup (Pandas .loc is too slow in a loop)
        tick_data_map = {}
        if not tick_df.empty:
            cols = ['strike_price', 'ce_ltp', 'pe_ltp', 'ce_delta', 'pe_delta']
            relevant_cols = [c for c in cols if c in tick_df.columns]
            tick_data_map = tick_df[relevant_cols].drop_duplicates(subset='strike_price').set_index('strike_price').to_dict('index')

        # PERFORMANCE: Batch re-fetch LTP & OI for all strikes in watchlist
        exp = self.atm_manager.signal_expiry_date
        strike_to_keys = {}
        all_keys = []
        for strike in watchlist:
            ck = self.atm_manager.find_instrument_key_by_strike(strike, 'CALL', exp)
            pk = self.atm_manager.find_instrument_key_by_strike(strike, 'PUT', exp)
            strike_to_keys[strike] = (ck, pk)
            if ck: all_keys.append(ck)
            if pk: all_keys.append(pk)

        batch_ltps = {}
        batch_ois = {}
        if all_keys:
            needed_ltp_keys = []
            needed_oi_keys = []
            bucket_ts = timestamp.replace(second=0, microsecond=0)
            if bucket_ts.tzinfo is None: bucket_ts = pd.Timestamp(bucket_ts).tz_localize('Asia/Kolkata')

            for k in all_keys:
                h_ltp = self.state_manager.ltp_history.get(k, {}).get(bucket_ts)
                if h_ltp is not None: batch_ltps[k] = h_ltp
                else: needed_ltp_keys.append(k)

                h_oi = self.state_manager.option_oi.get(k)
                if h_oi is not None: batch_ois[k] = h_oi
                else: needed_oi_keys.append(k)

            if needed_ltp_keys:
                res_ltps = await asyncio.gather(*[self._get_ltp_for_backtest_instrument(k, timestamp) for k in needed_ltp_keys])
                batch_ltps.update(dict(zip(needed_ltp_keys, res_ltps)))

            # PERFORMANCE: Throttle OI fetching to once per simulated minute
            if needed_oi_keys and (getattr(self, '_last_oi_fetch_min', None) != bucket_ts):
                self._last_oi_fetch_min = bucket_ts
                res_ois = await asyncio.gather(*[self._get_oi_for_backtest_instrument(k, timestamp) for k in needed_oi_keys])
                batch_ois.update(dict(zip(needed_oi_keys, res_ois)))

        for strike in watchlist:
            ck, pk = strike_to_keys[strike]
            api_ce, api_pe = batch_ltps.get(ck), batch_ltps.get(pk)
            s_data = tick_data_map.get(strike, {})
            ce_p = s_data.get('ce_ltp') if s_data.get('ce_ltp') and s_data.get('ce_ltp') > 0 else api_ce
            pe_p = s_data.get('pe_ltp') if s_data.get('pe_ltp') and s_data.get('pe_ltp') > 0 else api_pe

            self.state_manager.option_data[strike] = {
                'ce_ltp': ce_p, 'pe_ltp': pe_p,
                'ce_delta': s_data.get('ce_delta'), 'pe_delta': s_data.get('pe_delta')
            }
            if ck and ce_p: self.state_manager.option_prices[ck] = ce_p
            if pk and pe_p: self.state_manager.option_prices[pk] = pe_p
            if ck and batch_ois.get(ck): self.state_manager.option_oi[ck] = batch_ois[ck]
            if pk and batch_ois.get(pk): self.state_manager.option_oi[pk] = batch_ois[pk]

        for session in self.user_sessions.values():
            session.state_manager.timestamp = timestamp
            session.state_manager.spot_price = fut_p
            session.state_manager.index_price = idx_p
            session.state_manager.option_prices.update(self.state_manager.option_prices)
            session.state_manager.option_data.update(self.state_manager.option_data)
            session.state_manager.option_oi.update(self.state_manager.option_oi)

    async def _get_ltp_for_strike(self, strike, side, timestamp):
        expiry = self.atm_manager.signal_expiry_date
        key = self.atm_manager.find_instrument_key_by_strike(strike, side, expiry)
        return await self._get_ltp_for_backtest_instrument(key, timestamp) if key else None

    async def close_open_backtest_positions(self, timestamp):
        if not self.pnl_tracker: return
        for side in ['CALL', 'PUT']:
            trade = self.pnl_tracker.active_call_trade if side == 'CALL' else self.pnl_tracker.active_put_trade
            if trade:
                ohlc = await self.data_manager.get_historical_ohlc(trade['instrument_key'], 1, current_timestamp=timestamp, for_full_day=True)
                ltp = ohlc.asof(timestamp)['close'] if ohlc is not None and not ohlc.empty else None
                if ltp: self.pnl_tracker.exit_trade(side, ltp, timestamp, reason="End of backtest")

        if hasattr(self, 'sell_manager') and self.sell_manager.strangle_placed and not self.sell_manager.strangle_closed:
            await self.sell_manager.close_all(timestamp)

        # Final UI status write
        self.status_writer.maybe_write(timestamp, self.atm_manager.strikes.get('atm'), force=True)
