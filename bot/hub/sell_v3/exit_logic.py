import datetime
import asyncio
from utils.logger import logger
from .base import SellV3Base

class ExitLogic(SellV3Base):
    """Handles technical and rule-based exit logic for Sell V3 positions."""

    async def check_exits(self, ticks, timestamp):
        ce = self.manager.active_trades.get('CE')
        pe = self.manager.active_trades.get('PE')
        if not ce or not pe: return

        # --- EOD FORCE SQUARE-OFF (Highest Priority) ---
        # If the clock has passed square_off_time, force-exit ALL open positions
        # regardless of P&L and skip all further exit evaluation for this tick.
        sq_off_str = self._v3_cfg('square_off_time', '15:15', timestamp=timestamp)
        try:
            sq_parts = str(sq_off_str).replace('.', ':').split(':')
            sq_off_time = datetime.time(int(sq_parts[0]), int(sq_parts[1]))
        except Exception:
            sq_off_time = datetime.time(15, 15)

        if timestamp.time() >= sq_off_time:
            eod_bucket = f"eod_sqoff_{timestamp.date()}"
            if getattr(self.manager, '_last_eod_sqoff_bucket', None) != eod_bucket:
                self.manager._last_eod_sqoff_bucket = eod_bucket
                logger.info(
                    f"[SellManagerV3] EOD FORCE SQUARE-OFF triggered at "
                    f"{timestamp.strftime('%H:%M:%S')} "
                    f"(square_off_time={sq_off_str}) — closing all open positions."
                )
                await self.manager._execute_full_exit(timestamp, "EOD_SQUAREOFF", stop_for_day=True)
            return  # Past square-off time: skip all further exit checks

        do_log = getattr(self.manager, 'do_log', False)
        # Use state manager for LTP to be robust against filtered tick dictionaries
        ltp_ce = self.orchestrator.state_manager.get_ltp(ce['key'])
        ltp_pe = self.orchestrator.state_manager.get_ltp(pe['key'])

        # In backtest, fallback to ticks dict if state_manager is not yet updated
        if ltp_ce is None: ltp_ce = ticks.get(ce['key'], {}).get('ltp', 0)
        if ltp_pe is None: ltp_pe = ticks.get(pe['key'], {}).get('ltp', 0)

        # BACKTEST ROBUSTNESS: If one price is missing for a second,
        # use the last known price instead of skipping the exit check.
        if not ltp_ce: ltp_ce = ce['entry_price']
        if not ltp_pe: ltp_pe = pe['entry_price']

        combined_entry = ce['entry_price'] + pe['entry_price']
        combined_ltp = ltp_ce + ltp_pe
        profit_pct = (combined_entry - combined_ltp) / combined_entry * 100
        current_trade_pts = round(combined_entry - combined_ltp, 2)

        # --- MANDATORY GLOBAL GUARDRAILS (Highest Priority) ---
        # B. PnL Guardrail (Cumulative Session Points)
        if self._v3_cfg('guardrail_pnl.enabled', False, bool, timestamp=timestamp):
            g_pnl_target = self._v3_cfg('guardrail_pnl.target_pts', None, float, timestamp=timestamp)
            if g_pnl_target is None: g_pnl_target = 100.0

            g_pnl_sl_raw = self._v3_cfg('guardrail_pnl.stoploss_pts', None, float, timestamp=timestamp)
            if g_pnl_sl_raw is None: g_pnl_sl = -100.0
            else: g_pnl_sl = -abs(g_pnl_sl_raw) # Auto-negate SL for robustness

            # Cumulative = Past Trades Points + Current Trade Points
            pts_pnl = round(self.manager.session_points_pnl + current_trade_pts, 2)

            # Log PnL status if Guardrails are active
            # Robustness: Also log if within 10 pts of SL or Target to give visibility in the tail
            is_near_sl = (g_pnl_sl < 0 and pts_pnl <= (g_pnl_sl + 10))
            is_near_target = (g_pnl_target > 0 and pts_pnl >= (g_pnl_target - 10))

            if (do_log or is_near_sl or is_near_target) and (g_pnl_target > 0 or g_pnl_sl < 0):
                logger.info(f"[SellManagerV3] PnL Guardrail Check: Cumulative={pts_pnl:+.2f} Pts (Session:{self.manager.session_points_pnl:+.2f}, Current:{current_trade_pts:+.2f}) | Target={g_pnl_target} | SL={g_pnl_sl}")

            # To allow 0.0 as a valid SL/Target, we check if they are set and if PnL has moved.
            if g_pnl_target >= 0 and pts_pnl >= g_pnl_target:
                # Add a tiny movement buffer to prevent immediate exit on entry when Target=0.0
                if pts_pnl != 0 or current_trade_pts > 0:
                    logger.info(f"[SellManagerV3] PNL TARGET REACHED: {pts_pnl:.2f} >= {g_pnl_target}")
                    await self.manager._execute_full_exit(timestamp, f"Global PnL Target ({pts_pnl:.1f} Pts)", stop_for_day=True)
                    return
            if g_pnl_sl <= 0 and pts_pnl <= g_pnl_sl:
                if pts_pnl != 0 or current_trade_pts < 0:
                    logger.info(f"[SellManagerV3] PNL STOP LOSS REACHED: {pts_pnl:.2f} <= {g_pnl_sl}")
                    await self.manager._execute_full_exit(timestamp, f"Global PnL SL ({pts_pnl:.1f} Pts)", stop_for_day=True)
                    return

        # 0. Single Trade Target/SL (Points) - Non-permanent exit
        # BACKTEST FIX: Pass timestamp to ensure day-wise overrides work in simulation
        st_target = self._v3_cfg('single_trade_target_pts', 0.0, float, timestamp=timestamp)
        st_sl_raw = self._v3_cfg('single_trade_stoploss_pts', 0.0, float, timestamp=timestamp)
        # Robustness: stoploss must be negative. Auto-correct if entered as positive in UI.
        st_sl = -abs(st_sl_raw) if st_sl_raw != 0 else 0.0

        if st_target > 0 and current_trade_pts >= st_target:
            logger.info(f"[SellManagerV3] Single Trade Target Hit: {current_trade_pts:.2f} >= {st_target}. CE Entry/LTP: {ce['entry_price']}/{ltp_ce}, PE Entry/LTP: {pe['entry_price']}/{ltp_pe}")
            await self.manager._execute_full_exit(timestamp, f"Single Trade Target ({current_trade_pts:.1f} Pts)")
            return
        if st_sl < 0 and current_trade_pts <= st_sl:
            logger.info(f"[SellManagerV3] Single Trade SL Hit: {current_trade_pts:.2f} <= {st_sl}. CE Entry/LTP: {ce['entry_price']}/{ltp_ce}, PE Entry/LTP: {pe['entry_price']}/{ltp_pe}")
            await self.manager._execute_full_exit(timestamp, f"Single Trade SL ({current_trade_pts:.1f} Pts)")
            return

        # 1. Target Profit % -> Smart Roll
        if self._v3_cfg('profit_target_enabled', False, bool, timestamp=timestamp):
            target_pct = self._v3_cfg('profit_target_pct', 12.0, float, timestamp=timestamp)
            if profit_pct >= target_pct:
                await self.perform_smart_roll(timestamp, ticks, f"Profit Target {profit_pct:.1f}%")
                return

        # 2. LTP Decay -> Smart Roll (Higher Priority than Ratio Breach)
        if self._v3_cfg('ltp_decay_enabled', False, bool, timestamp=timestamp):
            decay_thresh = self._v3_cfg('ltp_exit_min', 20.0, float, timestamp=timestamp)
            if ltp_ce < decay_thresh or ltp_pe < decay_thresh:
                await self.perform_smart_roll(timestamp, ticks, f"LTP Decay (<{decay_thresh})")
                return

        # 3. Ratio Exit 3:1 -> Full Exit
        if self._v3_cfg('ratio_exit.enabled', False, bool, timestamp=timestamp):
            ratio_threshold = self._v3_cfg('ratio_exit.threshold', 3.0, float, timestamp=timestamp)
            if min(ltp_ce, ltp_pe) > 0:
                current_ratio = max(ltp_ce, ltp_pe) / min(ltp_ce, ltp_pe)
                if current_ratio >= ratio_threshold:
                    await self.manager._execute_full_exit(timestamp, f"Ratio Breach {current_ratio:.1f}")
                    return

        # 4. Scalable TSL (Rupee-based Trailing SL)
        if self._v3_cfg('tsl_scalable.enabled', False, bool, timestamp=timestamp):
            base_profit = self._v3_cfg('tsl_scalable.base_profit', None, float, timestamp=timestamp)
            if base_profit is None: base_profit = 1000.0

            base_lock = self._v3_cfg('tsl_scalable.base_lock', None, float, timestamp=timestamp)
            if base_lock is None: base_lock = 250.0

            step_profit = self._v3_cfg('tsl_scalable.step_profit', None, float, timestamp=timestamp)
            if step_profit is None: step_profit = 250.0

            step_lock = self._v3_cfg('tsl_scalable.step_lock', None, float, timestamp=timestamp)
            if step_lock is None: step_lock = 250.0

            # Resolve total quantity across all brokers
            ref_broker = next(iter(self.orchestrator.broker_manager.brokers.values()), None)
            qty_multiplier = ref_broker.config_manager.get_int(ref_broker.instance_name, 'quantity', 1) if ref_broker else 1

            # Per-Lot Scaling: Multiply thresholds and locks by the number of lots (qty_multiplier)
            base_profit *= qty_multiplier
            base_lock *= qty_multiplier
            step_profit *= qty_multiplier
            step_lock *= qty_multiplier

            # V3 Correction: Use contract lot size if available, fallback to instrument default
            lot_size = ce.get('lot_size')
            if not lot_size:
                lot_size = self.orchestrator.config_manager.get_int(self.instrument_name, 'lot_size', 50)

            total_qty = lot_size * qty_multiplier

            profit_rs = (combined_entry - combined_ltp) * total_qty

            # Initialize high lock if not present
            if not hasattr(self.manager, 'tsl_high_lock'): self.manager.tsl_high_lock = 0.0

            if profit_rs >= base_profit:
                # V3 Refinement: Calculate steps based on the profit amount ABOVE base_profit.
                # Example (1 Lot): PnL 3500, Base 1000, Step 250 -> Steps = (3500-1000)/250 = 10 steps.
                # Lock = 250 (Base) + 10 * 250 (Step) = 2750.
                num_steps = int((profit_rs - base_profit) // step_profit)
                current_calc_lock = base_lock + (num_steps * step_lock)

                # Persist the highest lock reached in manager state to ensure monotonicity
                if current_calc_lock > self.manager.tsl_high_lock:
                    self.manager.tsl_high_lock = current_calc_lock
                    self.manager.save_state()

            locked_rs = self.manager.tsl_high_lock

            if locked_rs > 0:
                if do_log:
                    logger.info(f"[SellManagerV3] Scalable TSL: Profit_Rs={profit_rs:.2f}, Highest_Locked_Rs={locked_rs:.2f} (Base:{base_profit}, Step:{step_profit})")

                if profit_rs < locked_rs:
                    # Reset high lock state for next trade
                    self.manager.tsl_high_lock = 0.0
                    await self.manager._execute_full_exit(timestamp, f"Scalable TSL locked at {locked_rs:.0f} (Profit: {profit_rs:.0f})")
                    return

        # 5. Global Guardrails (Independent Timeframes)
        # A. ROC Guardrail (Evaluated at TF boundary)
        if self._v3_cfg('guardrail_roc.enabled', False, bool, timestamp=timestamp):
            g_roc_tf = self._v3_cfg('guardrail_roc.tf', 15, int, timestamp=timestamp)

            # Boundary Check: Every 15 minutes (or g_roc_tf)
            is_roc_boundary = (timestamp.minute % g_roc_tf == 0)
            if not self.orchestrator.is_backtest:
                is_roc_boundary = is_roc_boundary and (0 <= timestamp.second <= 15)

            if is_roc_boundary:
                # Suppress duplicate logging on the same boundary
                bucket_roc = f"roc_{timestamp.replace(second=0, microsecond=0)}"
                if getattr(self.manager, '_last_roc_log_bucket', None) != bucket_roc:
                    # Set bucket BEFORE await to prevent concurrent entry
                    self.manager._last_roc_log_bucket = bucket_roc

                    g_roc_len = self._v3_cfg('guardrail_roc.length', 9, int, timestamp=timestamp)
                    g_roc_target = self._v3_cfg('guardrail_roc.target', None, float, timestamp=timestamp)
                    if g_roc_target is None: g_roc_target = -20.0

                    g_roc_sl = self._v3_cfg('guardrail_roc.stoploss', None, float, timestamp=timestamp)
                    if g_roc_sl is None: g_roc_sl = 10.0

                    # STRICT: Use finalized boundary to avoid look-ahead bias
                    f_anchor = self.get_finalized_anchor(timestamp, g_roc_tf)
                    roc_val = await self.orchestrator.indicator_manager.calculate_combined_roc(ce['key'], pe['key'], f_anchor, tf=g_roc_tf, length=g_roc_len, include_current=False)

                    if roc_val is not None:
                        logger.info(f"[SellManagerV3] Global ROC Guardrail ({g_roc_tf}m): Value={roc_val:+.2f} | Target={g_roc_target} | SL={g_roc_sl}")

                        # Sell Side ROC Guardrails:
                        # Profit Target is hit when ROC drops BELOW a negative target.
                        # Stop Loss is hit when ROC rises ABOVE a positive stoploss.
                        # We use >=/<= for SL/Target to allow 0.0 as a valid trigger.
                        if g_roc_target < 0 and roc_val <= g_roc_target:
                            await self.manager._execute_full_exit(timestamp, f"Global ROC Target ({roc_val:.1f})")
                            return
                        if g_roc_sl >= 0 and roc_val >= g_roc_sl:
                            await self.manager._execute_full_exit(timestamp, f"Global ROC SL ({roc_val:.1f})")
                            return

        # 6. Technical Boundary-Based Exits
        v_slope_tf = self._v3_cfg('v_slope_entry.tf', 1, int, timestamp=timestamp)
        rsi_tf_macro = self._v3_cfg('rsi_exit.tf', 15, int, timestamp=timestamp)
        vwap_tf_macro = self._v3_cfg('vwap_exit.tf', 15, int, timestamp=timestamp)
        vslope_tf_macro = self._v3_cfg('v_slope_exit.tf', 15, int, timestamp=timestamp)
        macro_tf = max(rsi_tf_macro, vwap_tf_macro, vslope_tf_macro)

        is_entry_tf_boundary = (timestamp.minute % v_slope_tf == 0)
        is_macro_tf_boundary = (timestamp.minute % macro_tf == 0)
        market_open = self._get_market_open_time(timestamp)
        is_session_ready = (timestamp - market_open).total_seconds() >= (2 * v_slope_tf * 60)

        can_check_1m = is_entry_tf_boundary and is_session_ready
        can_check_macro = is_macro_tf_boundary

        if not self.orchestrator.is_backtest:
            can_check_1m = can_check_1m and (5 <= timestamp.second <= 15)
            can_check_macro = can_check_macro and (5 <= timestamp.second <= 15)

        # A. 1m Safety / VWAP Rise Check
        vwap_rise_sl_enabled = self.orchestrator.get_strat_cfg("sell.exit_indicators.combined_vwap_slope.enabled", False, bool)
        # Step 7: VWAP should be tick-based. We remove the 'can_check_1m' minute-boundary gate
        # to allow tick-by-tick monitoring of the VWAP Rise SL.
        if vwap_rise_sl_enabled and is_session_ready:
            # We still throttle the log to once per minute, but check every tick
            combined_vwap = (await self.orchestrator.indicator_manager.calculate_vwap(ce['key'], timestamp) or 0) + \
                            (await self.orchestrator.indicator_manager.calculate_vwap(pe['key'], timestamp) or 0)

            if combined_vwap > 0 and self.manager.session_min_vwap != float('inf'):
                    rise_pct = (combined_vwap - self.manager.session_min_vwap) / self.manager.session_min_vwap * 100
                    thresh = self.orchestrator.get_strat_cfg("sell.v3.vwap_rise_sl.threshold", None, float)
                    if thresh is None: thresh = self.orchestrator.get_strat_cfg("sell.exit_indicators.combined_vwap_slope.threshold", 1.0, float)
                    if thresh < 0.2: thresh *= 100.0

                    if do_log:
                        f_anchor = self.get_finalized_anchor(timestamp, v_slope_tf)
                        res_sl = await self.orchestrator.indicator_manager.get_vwap_slope_pair(ce['key'], pe['key'], f_anchor + datetime.timedelta(seconds=1), v_slope_tf)
                        curr_s, prev_s = res_sl[0], res_sl[1]
                        s_str = f" | V-Slope({v_slope_tf}m): Curr={curr_s:.5f}" if curr_s is not None else ""
                        logger.info(f"[SellManagerV3] Exit Gate Check (VWAP Rise): Rise={rise_pct:.2f}%, Threshold={thresh}% (TF: {v_slope_tf}m){s_str}")

                    if rise_pct >= thresh:
                        await self.manager._execute_full_exit(timestamp, f"VWAP Rise SL {rise_pct:.2f}% (Curr:{combined_vwap:.2f}, Low:{self.manager.session_min_vwap:.2f})")
                        return

        # B. Dynamic Technical Exit Rules (Reversal Detection)
        exit_rules = self._v3_cfg('exit_rules', [], timestamp=timestamp)
        if exit_rules:
            # Determine the maximum timeframe (TF) among all rules to set the boundary
            # This ensures rules like "ROC 5m" or "VWAP 15m" only evaluate at their slowest shared boundary
            rule_tfs = [int(r.get('tf', 1)) for r in exit_rules]
            max_rule_tf = max(rule_tfs) if rule_tfs else 1

            if timestamp.minute % max_rule_tf == 0 and (self.orchestrator.is_backtest or (5 <= timestamp.second <= 15)):
                bucket_id = f"dyn_exit_{max_rule_tf}m_{timestamp.replace(second=0, microsecond=0)}"
                if getattr(self.manager, '_last_dyn_exit_bucket', None) != bucket_id:
                    self.manager._last_dyn_exit_bucket = bucket_id

                    logger.info(f"[SellManagerV3] {max_rule_tf}m Dynamic Exit Boundary Check triggered at {timestamp.strftime('%H:%M:%S')}")
                    # Evaluate dynamic rules using the helper
                    is_exit_met, exit_reason_str = await self.evaluate_rules(exit_rules, ce['key'], pe['key'], timestamp, is_entry=False, do_log=True, return_reason=True)

                    if is_exit_met:
                        await self.manager._execute_full_exit(timestamp, f"Dynamic Technical Exit ({exit_reason_str})")
                        return

    async def perform_smart_roll(self, timestamp, ticks, reason):
        """
        Refined Roll Logic: Irrespective of any kind of rollover,
        re-entry technical gates MUST be checked.
        """
        if not self._v3_cfg('smart_rolling_enabled', True, bool, timestamp=timestamp):
            await self.manager._execute_full_exit(timestamp, reason)
            return

        logger.info(f"[SellManagerV3] Smart Roll triggered: {reason}. Validating technical gates for re-entry...")

        # Determine candidates via pool scan (standard re-entry logic)
        v_slope_tf = self._v3_cfg('v_slope_entry.tf', 1, int, timestamp=timestamp)
        anchor_ts = timestamp.replace(minute=(timestamp.minute // v_slope_tf) * v_slope_tf, second=0, microsecond=0) - datetime.timedelta(seconds=1)

        candidates = await self.manager.entry_logic._scan_v_slope_pool(ticks, timestamp, anchor_ts, do_log=True)

        if not candidates:
            logger.info(f"[SellManagerV3] Roll Rejected: No candidates passed technical gates. Performing Full Exit.")
            await self.manager._execute_full_exit(timestamp, f"Full Exit ({reason} - Gates Rejected)")
            return

        # candidate format is [(ce, pe, slope)]
        ce_new, pe_new, slope_new = candidates[0]

        ce_old = self.manager.active_trades.get('CE')
        pe_old = self.manager.active_trades.get('PE')

        if ce_old and pe_old and ce_new['strike'] == ce_old['strike'] and pe_new['strike'] == pe_old['strike']:
            logger.info(f"[SellManagerV3] Virtual Roll: Current strikes passed technical gates. Refreshing entry prices.")
            self.manager.active_trades['CE']['entry_price'] = ce_new['ltp']
            self.manager.active_trades['PE']['entry_price'] = pe_new['ltp']
            # Enrichment: reset entry time for TSL math stability if desired
            self.manager.active_trades['CE']['entry_time'] = timestamp
            self.manager.active_trades['PE']['entry_time'] = timestamp
            self.manager.tsl_high_lock = 0.0
            self.manager.save_state()
            return

        await self.manager._execute_full_exit(timestamp, f"Physical Roll: {reason}", cooldown=False)
        await self.manager._execute_straddle_entry(ce_new, pe_new, timestamp, f"Roll Re-entry: {reason}", slope_data=slope_new)
