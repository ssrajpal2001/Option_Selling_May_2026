import datetime
import asyncio
from utils.logger import logger
from .base import SellV3Base

class EntryLogic(SellV3Base):
    """Handles candidate selection and technical gating for Sell V3 entries."""

    async def _resolve_current_candidate_pair(self, ticks, timestamp):
        """Unified helper to find the best candidate strike pair for the current pulse."""
        is_beg = "beginning" in self.manager.workflow_phase.lower()
        rules = self._v3_cfg('entry_rules_beginning' if is_beg else 'entry_rules_reentry', [])

        # Determine scanning boundary
        rule_tfs = [int(r.get('tf', 1)) for r in rules]
        v_slope_active = any(r.get('indicator') in ('slope', 'vwap_slope') for r in rules)
        v_slope_tf = self._v3_cfg('v_slope_entry.tf', 1, int, timestamp=timestamp)
        active_tfs = rule_tfs
        if v_slope_active: active_tfs.append(v_slope_tf)
        max_tf = max(active_tfs) if active_tfs else 1
        anchor_ts = timestamp.replace(minute=(timestamp.minute // max_tf) * max_tf, second=0, microsecond=0) - datetime.timedelta(seconds=1)

        # CONCEPT A: BEGINNING (ATM-focused)
        workflow_mode = self._v3_cfg('entry_workflow_mode', 'hybrid', timestamp=timestamp)
        use_beg_concept = workflow_mode == 'beginning_only' or (workflow_mode == 'hybrid' and is_beg)

        if use_beg_concept:
            ce, pe = await self._get_strictly_lower_balanced_pair(ticks, timestamp)
            if ce and pe: return ce['key'], pe['key'], ce['strike'], pe['strike']
        else:
            # CONCEPT B: RE-ENTRY (Pool-focused)
            cands = await self._scan_v_slope_pool(ticks, timestamp, anchor_ts, rules, do_log=False)
            if cands:
                ce, pe, _ = cands[0]
                return ce['key'], pe['key'], ce['strike'], pe['strike']
        return None

    async def check_entry(self, ticks, timestamp):
        do_log = getattr(self.manager, 'do_log', False)

        is_beginning = "beginning" in self.manager.workflow_phase.lower()
        rules = self._v3_cfg('entry_rules_beginning' if is_beginning else 'entry_rules_reentry', [], timestamp=timestamp)

        active_tfs = [int(r.get('tf', 1)) for r in rules]
        if any(r.get('indicator') in ('slope', 'vwap_slope') for r in rules):
            active_tfs.append(self._v3_cfg('v_slope_entry.tf', 1, int, timestamp=timestamp))

        max_entry_tf = max(active_tfs) if active_tfs else 1

        # 2. Synchronized Pulse Logic (Boundary + Buffer)
        # User Requirement: Strictly follow Close Candle. Check at Boundary + 5 seconds in Live.
        # OPTIMIZATION: In Backtest, no buffer is required as data is already finalized.
        pulse_seconds = 0 if self.orchestrator.is_backtest else 5

        # User Requirement: If Rules are OFF, take trade immediately when LTP targets are met.
        # Evaluated BEFORE priming so that rules-less (immediate) mode is never blocked by the
        # startup/market-open priming wait — the beginning concept uses only current ATM LTPs,
        # it does not need any historical indicator data to be primed.
        is_immediate = not rules

        # Calculate if we are at or just past the boundary
        boundary_minute = (timestamp.minute // max_entry_tf) * max_entry_tf
        boundary_ts = timestamp.replace(minute=boundary_minute, second=0, microsecond=0)
        pulse_ts = boundary_ts + datetime.timedelta(seconds=pulse_seconds)

        # Only allow evaluation if current timestamp >= pulse_ts
        if not is_immediate and timestamp < pulse_ts:
            return

        last_pulse = getattr(self.manager, '_last_entry_pulse_ts', None)
        if last_pulse and last_pulse == boundary_ts:
            return

        # 3. One Entry Per Candle rule (based on Max Active Entry TF)
        current_bucket = str(boundary_ts)
        if self.manager.last_entry_bucket == current_bucket:
            return

        # Record this pulse
        self.manager._last_entry_pulse_ts = boundary_ts

        # Mark as boundary for logging
        if not self.manager.active_trades:
            do_log = True
            self.manager.do_log = True

        # 0. Trade Limit Check
        max_trades = self._v3_cfg('max_trades_per_day', 0, int, timestamp=timestamp)
        if max_trades > 0 and self.manager.trades_completed_today >= max_trades:
            if do_log:
                logger.info(f"[SellManagerV3] Trade limit reached: {self.manager.trades_completed_today} >= {max_trades}. No more entries today.")
            return

        # Priming wait: guards indicator-dependent logic.
        # Immediate mode (no rules) bypasses priming — beginning concept is ATM-based, no history needed.
        is_priming = self._is_in_priming_wait(timestamp)
        if is_priming and not is_immediate:
            if do_log:
                now_val = int(timestamp.timestamp()) if self.orchestrator.is_backtest else asyncio.get_event_loop().time()
                if (now_val - getattr(self.manager, '_last_wait_log_time', 0) >= 60):
                    logger.info(f"[SellManagerV3] {self.manager._wait_reason}")
                    self.manager._last_wait_log_time = now_val
            return

        end_time_str = self._v3_cfg('entry_end_time', '14:00:00', timestamp=timestamp)
        if not end_time_str:
            end_time = datetime.time(14, 0)
        else:
            try:
                if len(str(end_time_str).split(':')) == 2:
                    end_time = datetime.datetime.strptime(str(end_time_str), '%H:%M').time()
                else:
                    end_time = datetime.datetime.strptime(str(end_time_str), '%H:%M:%S').time()
            except Exception as e:
                logger.warning(f"[SellManagerV3] Invalid entry_end_time format '{end_time_str}': {e}. Using default 14:00:00")
                end_time = datetime.time(14, 0)

        if timestamp.time() > end_time: return

        if not self.orchestrator.is_backtest:
            if self.manager._last_exit_timestamp and (timestamp - self.manager._last_exit_timestamp).total_seconds() < 60:
                return

        anchor_ts = timestamp.replace(minute=(timestamp.minute // max_entry_tf) * max_entry_tf, second=0, microsecond=0) - datetime.timedelta(seconds=1)

        # 4. Entry Workflow Mode Selection
        # options: 'hybrid', 'beginning_only', 'reentry_only'
        workflow_mode = self._v3_cfg('entry_workflow_mode', 'hybrid', timestamp=timestamp)

        use_beginning_concept = False
        if workflow_mode == 'beginning_only':
            use_beginning_concept = True
        elif workflow_mode == 'reentry_only':
            use_beginning_concept = False
        else: # hybrid
            use_beginning_concept = is_beginning

        # 5. Candidate Selection & Execution
        if use_beginning_concept:
            # --- CONCEPT A: BEGINNING CONCEPT (ATM Anchor/Partner Logic) ---
            if do_log: logger.info(f"[SellManagerV3] Selection: Using BEGINNING CONCEPT (ATM Selection)...")
            ce_cand, pe_cand = await self._get_strictly_lower_balanced_pair(ticks, timestamp)

            if ce_cand and pe_cand:
                # Evaluate technical rules for the selected pair
                passed, reason = await self.evaluate_rules(rules, ce_cand['key'], pe_cand['key'], timestamp, is_entry=True, anchor_ts=anchor_ts, do_log=do_log, return_reason=True)

                if passed:
                    _, _, slopes = await self._check_v_slope_gate_for_strikes(ce_cand['key'], pe_cand['key'], timestamp, do_log=False)
                    await self.manager._execute_straddle_entry(ce_cand, pe_cand, timestamp, f"Beginning Concept ({reason})", slope_data=slopes)
                    return
                elif do_log:
                    logger.info(f"[SellManagerV3] Beginning Concept technicals FAILED: {reason}. Searching next ATM in next pulse...")

            # Hybrid transition: Beginning failed → switch to pool scan regardless of is_immediate
            if is_beginning and workflow_mode == 'hybrid':
                logger.info(f"[SellManagerV3] Hybrid Transition: Beginning Concept failed. Switching to Re-entry Concept (Pool Scan) for next pulse.")
                self.manager.workflow_phase = 'CONTINUE'
            return

        else:
            # --- CONCEPT B: RE-ENTRY CONCEPT (Pool Scan Logic) ---
            if do_log: logger.info(f"[SellManagerV3] Selection: Using RE-ENTRY CONCEPT (Pool Scan)...")
            candidates = await self._scan_v_slope_pool(ticks, timestamp, anchor_ts, rules, do_log)

            if not candidates:
                # If hybrid beginning pool scan failed, transition for next pulse
                if is_beginning and workflow_mode == 'hybrid' and not is_immediate:
                     logger.info(f"[SellManagerV3] Hybrid Transition: Re-entry Concept rules failed. Retrying next pulse.")
                     self.manager.workflow_phase = 'CONTINUE'
                return

            if not isinstance(candidates, list): candidates = [candidates]

            metric_label = self._v3_cfg('reentry_best_metric', 'roc')
            for cand in candidates:
                ce_f, pe_f, slope_f = cand[0], cand[1], cand[2]
                await self.manager._execute_straddle_entry(ce_f, pe_f, timestamp, f"Re-entry Concept ({metric_label})", slope_data=slope_f)
                break

    async def _scan_v_slope_pool(self, ticks, timestamp, anchor_ts, rules, do_log=False):
        """
        Step 1-4: Scan for candidate strikes following ATM Anchor logic.
        1. Identify ATM side with GREATER LTP (Steps 2-3).
        2. Scan OTHER side for strikes LOWER than Anchor LTP (Step 4).
        3. Filter by Technical Rules (Step 10: VWAP -> Slope -> RSI -> ROC).
        """
        interval = self.orchestrator.config_manager.get_int(self.instrument_name, 'strike_interval', 50)
        anchor_price = self.orchestrator.get_anchor_price()
        if not anchor_price: return None
        atm = int(round(anchor_price / interval) * interval)
        expiry = self.orchestrator.atm_manager.signal_expiry_date

        # Priority hierarchy for LTP Target
        ltp_target = self._v3_cfg('ltp_target', None, float, timestamp=timestamp)
        if ltp_target is None: ltp_target = self._cfg('ltp_min', 50.0, float)

        # User Requirement: N x N Matrix Scan for Pool Range
        reentry_offset = self._v3_cfg('v_slope_pool_offset', None, int, timestamp=timestamp)
        if reentry_offset is None: reentry_offset = self._v3_cfg('reentry_offset', 4, int, timestamp=timestamp)

        strikes = [atm + i * interval for i in range(-reentry_offset, reentry_offset + 1)]
        candidates = []
        v_slope_tf = self._v3_cfg('v_slope_entry.tf', 1, int, timestamp=timestamp)
        metric = self._v3_cfg('reentry_best_metric', 'balanced_premium', timestamp=timestamp)

        # ATM Bias Detection for Matrix Filter
        ce_key_atm = self.orchestrator.atm_manager.find_instrument_key_by_strike(atm, 'CE', expiry)
        pe_key_atm = self.orchestrator.atm_manager.find_instrument_key_by_strike(atm, 'PE', expiry)
        ce_ltp_atm = ticks.get(ce_key_atm, {}).get('ltp', 0) if ce_key_atm else 0
        pe_ltp_atm = ticks.get(pe_key_atm, {}).get('ltp', 0) if pe_key_atm else 0

        if do_log:
            logger.info(f"[SellManagerV3] Pool Scan (Matrix {len(strikes)}x{len(strikes)}) - ATM Bias: {'CE' if ce_ltp_atm > pe_ltp_atm else 'PE'} Stronger")

        # N x N Matrix Loop
        for s_ce in strikes:
            ce_key = self.orchestrator.atm_manager.find_instrument_key_by_strike(s_ce, 'CE', expiry)
            ce_ltp = ticks.get(ce_key, {}).get('ltp', 0) if ce_key else 0
            if ce_ltp == 0: continue

            for s_pe in strikes:
                pe_key = self.orchestrator.atm_manager.find_instrument_key_by_strike(s_pe, 'PE', expiry)
                pe_ltp = ticks.get(pe_key, {}).get('ltp', 0) if pe_key else 0
                if pe_ltp == 0: continue

                # Filter by LTP target
                if ce_ltp < ltp_target or pe_ltp < ltp_target:
                    continue

                # USER REQUIREMENT: Opposite Bias Filter (Flip Point)
                # If CE > PE at ATM, we ONLY consider combinations where PE > CE (to capture reversal/balance)
                if ce_ltp_atm > pe_ltp_atm:
                    if ce_ltp >= pe_ltp: continue
                else:
                    if pe_ltp >= ce_ltp: continue

                # Balanced Score (Relative Difference)
                balanced_score = abs(ce_ltp - pe_ltp) / (ce_ltp + pe_ltp) if (ce_ltp + pe_ltp) > 0 else 999.0

                # Evaluate technical gates (VWAP -> V-Slope -> RSI -> ROC) (Step 10)
                passed, tech_reason = await self.evaluate_rules(rules, ce_key, pe_key, timestamp, is_entry=True, anchor_ts=anchor_ts, do_log=False, return_reason=True)

                if not passed:
                    if do_log:
                        logger.info(f"  - Strike {s_ce}/{s_pe} [Key: {ce_key}/{pe_key}] [Score: {balanced_score:.4f}] Rejected Technicals: {tech_reason}")
                    continue

                # Metrics for selection
                ref_ts = anchor_ts or (timestamp.replace(second=0, microsecond=0) - datetime.timedelta(seconds=1))

                # 1. ROC Metric
                roc_target = 0.0
                roc_len = self._v3_cfg('roc.length', 9, int)
                for r in rules:
                    if r.get('indicator') == 'roc':
                        roc_target = float(r.get('target', 0.0))
                        roc_len = int(r.get('length', roc_len))
                        break
                roc_val = await self.orchestrator.indicator_manager.calculate_combined_roc(ce_key, pe_key, ref_ts, tf=v_slope_tf, length=roc_len, include_current=False)
                roc_dist = abs((roc_val or 999.0) - roc_target)

                # 2. VWAP % Metric
                vwap_dist = 999.0
                v1 = await self.orchestrator.indicator_manager.calculate_vwap(ce_key, ref_ts)
                v2 = await self.orchestrator.indicator_manager.calculate_vwap(pe_key, ref_ts)
                if v1 is not None and v2 is not None:
                    vwap_dist = ((v1 + v2) - (ce_ltp + pe_ltp)) / (ce_ltp + pe_ltp)

                # Resolve Slope for logging
                _, _, slopes = await self._check_v_slope_gate_for_strikes(ce_key, pe_key, timestamp, do_log=False)

                if do_log:
                    logger.info(f"  + Strike {s_ce}/{s_pe} [Key: {ce_key}/{pe_key}] | LTP: {ce_ltp:.2f}/{pe_ltp:.2f} | Score: {balanced_score:.4f} | PASS")

                candidates.append({
                    'ce': {'strike': s_ce, 'key': ce_key, 'ltp': ce_ltp},
                    'pe': {'strike': s_pe, 'key': pe_key, 'ltp': pe_ltp},
                    'slope': slopes, 'roc_dist': roc_dist, 'roc_raw': roc_val or 0,
                    'vwap_dist': vwap_dist, 'balanced_score': balanced_score
                })

        if not candidates: return []

        # Winner Selection Logic
        method_label = "Matrix Selection" if metric == 'balanced_premium' else "Pool Scan Selection"
        if metric == 'balanced_premium':
            candidates.sort(key=lambda x: x['balanced_score'])
            best = candidates[0]
            if do_log: logger.info(f"[SellManagerV3] {method_label}: {best['ce']['strike']}/{best['pe']['strike']} with Min Balanced Score: {best['balanced_score']:.4f}")
        elif metric == 'vwap_pct':
            candidates.sort(key=lambda x: x['vwap_dist'])
            best = candidates[0]
            if do_log: logger.info(f"[SellManagerV3] {method_label}: {best['ce']['strike']} with Min VWAP% Dev: {best['vwap_dist']*100:.2f}%")
        else:
            candidates.sort(key=lambda x: x['roc_dist'])
            best = candidates[0]
            if do_log: logger.info(f"[SellManagerV3] {method_label}: {best['ce']['strike']} with ROC {best['roc_raw']:.2f}")

        return [(best['ce'], best['pe'], best['slope'])]

    async def _check_v_slope_gate_for_strikes(self, ce_key, pe_key, timestamp, do_log=False):
        tf = self._v3_cfg('v_slope_entry.tf', 5, int, timestamp=timestamp)
        boundary = timestamp.replace(minute=(timestamp.minute // tf) * tf, second=0, microsecond=0)
        is_priming = self._is_in_priming_wait(timestamp)
        res = await self.orchestrator.indicator_manager.get_vwap_slope_pair(ce_key, pe_key, boundary, tf)
        curr_slope, prev_slope, v_curr, v_prev, v_prev2 = res
        if curr_slope is None:
            if do_log and not is_priming:
                logger.info(f"[SellManagerV3] V-Slope Data NOT READY for {ce_key}/{pe_key} at {boundary.time()}")
            return False, False, (None, None, None, None, None)
        # User requirement: CURR SLOPE < PRE SLOPE
        # User requirement: CURR SLOPE <= 0
        is_passed = (curr_slope <= 0)
        if do_log:
            if not hasattr(self.manager, '_last_v_slope_res'): self.manager._last_v_slope_res = {}
            if not hasattr(self.manager, '_last_v_slope_log_time'): self.manager._last_v_slope_log_time = {}
            key = f"{ce_key}/{pe_key}"
            now_val = int(timestamp.timestamp()) if self.orchestrator.is_backtest else asyncio.get_event_loop().time()
            if self.manager._last_v_slope_res.get(key) != is_passed or (now_val - self.manager._last_v_slope_log_time.get(key, 0) >= 60):
                logger.info(f"[SellManagerV3] V-Slope Check: {key} | Val: {curr_slope:.4f} | Result: {'PASS' if is_passed else 'REJECT'} (Thresh: <= 0)")
                self.manager._last_v_slope_res[key] = is_passed
                self.manager._last_v_slope_log_time[key] = now_val
        return is_passed, True, (curr_slope, prev_slope, v_curr, v_prev, v_prev2)

    async def _try_scanner_entry_with_candidates(self, ce_cand, pe_cand, ticks, timestamp, anchor_ts, do_log=False, slope_data=None):
        passed, tech_reason = await self._check_technical_gates(ce_cand, pe_cand, ticks, timestamp, anchor_ts, do_log=do_log, return_reason=True)
        if passed:
            await self.manager._execute_straddle_entry(ce_cand, pe_cand, timestamp, f"Scanner Entry ({tech_reason})", slope_data=slope_data)
            return True
        elif do_log: logger.info(f"[SellManagerV3] Scanner Entry gate rejected for {ce_cand['strike']}.")
        return False

    async def _check_technical_gates(self, ce_cand, pe_cand, ticks, timestamp, anchor_ts, do_log=False, return_reason=False):
        """Legacy helper now delegating to dynamic rule engine for consistency."""
        is_beg = "beginning" in self.manager.workflow_phase.lower()
        rules = self._v3_cfg('entry_rules_beginning' if is_beg else 'entry_rules_reentry', [])
        return await self.evaluate_rules(rules, ce_cand['key'], pe_cand['key'], timestamp, is_entry=True, anchor_ts=anchor_ts, do_log=do_log, return_reason=return_reason)

    async def _get_strictly_lower_balanced_pair(self, ticks, timestamp):
        """
        Beginning Concept Logic:
        1. Select ATM for both sides.
        2. Side with LOWER LTP is the Anchor.
        3. Search OTHER side for strike whose LTP < Anchor LTP.
        """
        interval = self.orchestrator.config_manager.get_int(self.instrument_name, 'strike_interval', 50)
        anchor_price = self.orchestrator.get_anchor_price()
        if not anchor_price: return None, None

        expiry = self.orchestrator.atm_manager.get_expiry_by_mode('sell', 'signal')

        # Priority hierarchy for LTP Target
        ltp_target = self._v3_cfg('ltp_target', None, float, timestamp=timestamp)
        if ltp_target is None: ltp_target = self._cfg('ltp_min', None, float)
        if ltp_target is None: ltp_target = 50.0

        # 1. Resolve ATM strike
        atm = int(round(anchor_price / interval) * interval)
        ce_key_atm = self.orchestrator.atm_manager.find_instrument_key_by_strike(atm, 'CE', expiry)
        pe_key_atm = self.orchestrator.atm_manager.find_instrument_key_by_strike(atm, 'PE', expiry)

        ce_ltp_atm = ticks.get(ce_key_atm, {}).get('ltp', 0) if ce_key_atm else 0
        pe_ltp_atm = ticks.get(pe_key_atm, {}).get('ltp', 0) if pe_key_atm else 0

        if ce_ltp_atm == 0 or pe_ltp_atm == 0:
            return None, None

        # Select anchor using corrected (time-value-only) LTP to avoid ITM distortion.
        # CE intrinsic = max(0, spot - strike), PE intrinsic = max(0, strike - spot).
        # Whichever side has LESS time value becomes the anchor; raw LTP used for all downstream logic.
        ce_corrected = ce_ltp_atm - max(0.0, anchor_price - atm)
        pe_corrected = pe_ltp_atm - max(0.0, atm - anchor_price)

        if ce_corrected < pe_corrected:
            anchor_cand = {'strike': atm, 'key': ce_key_atm, 'ltp': ce_ltp_atm, 'side': 'CE'}
            partner_side = 'PE'
        else:
            anchor_cand = {'strike': atm, 'key': pe_key_atm, 'ltp': pe_ltp_atm, 'side': 'PE'}
            partner_side = 'CE'

        # 2. Check LTP Threshold for Anchor
        if anchor_cand['ltp'] < ltp_target:
            if getattr(self.manager, 'do_log', False):
                logger.info(f"[SellV3] Beginning Concept: Anchor {anchor_cand['side']} {anchor_cand['strike']} LTP {anchor_cand['ltp']:.2f} below target {ltp_target}")
            return None, None

        # 3. Select Partner: Strictly lower than Anchor LTP and closest to it
        # User Requirement: Search wide (ATM and OTM) to find the best balanced premium.
        best_partner = None
        search_results = []

        # Scan range based on Pool Range config, default 4 steps
        pool_range = self._v3_cfg('v_slope_pool_offset', None, int, timestamp=timestamp)
        if pool_range is None: pool_range = self._v3_cfg('reentry_offset', 4, int, timestamp=timestamp)

        do_log = getattr(self.manager, 'do_log', False)
        if do_log:
            logger.info(f"[SellManagerV3] Beginning Selection - Anchor: {anchor_cand['side']} {anchor_cand['strike']} (@{anchor_cand['ltp']:.2f}, Key: {anchor_cand['key']})")

        for i in range(-pool_range, pool_range + 1):
            s = atm + i * interval
            key = self.orchestrator.atm_manager.find_instrument_key_by_strike(s, partner_side, expiry)
            if not key: continue
            ltp = ticks.get(key, {}).get('ltp', 0)

            # Logic: Strictly lower than anchor, but meets minimum floor
            is_valid = (ltp_target <= ltp < anchor_cand['ltp'])

            if do_log:
                status = "MATCH" if is_valid else "REJECT"
                rej = ""
                if not is_valid:
                    rej = f" (LTP {ltp:.2f} out of range {ltp_target}-{anchor_cand['ltp']:.2f})"
                logger.info(f"  - Checking {partner_side} {s} [Key: {key}] | LTP: {ltp:.2f} | {status}{rej}")

            if is_valid:
                search_results.append({'strike': s, 'key': key, 'ltp': ltp})

        if search_results:
            # Sort by LTP descending to find the one closest to the anchor from below
            search_results.sort(key=lambda x: x['ltp'], reverse=True)
            best_partner = search_results[0]

            if getattr(self.manager, 'do_log', False):
                logger.info(f"[SellV3] Strike Selection: Anchor={anchor_cand['side']} {anchor_cand['strike']} (@{anchor_cand['ltp']:.2f}) | "
                            f"Partner={partner_side} {best_partner['strike']} (@{best_partner['ltp']:.2f})")

        if not best_partner:
            return None, None

        ce_final = anchor_cand if anchor_cand['side'] == 'CE' else best_partner
        pe_final = anchor_cand if anchor_cand['side'] == 'PE' else best_partner

        return ce_final, pe_final

    async def _prime_indicators_for_strikes(self, ce_key, pe_key, timestamp):
        """
        Step 8 & 9: Prime technical indicators (RSI/ROC) using REST API history for new strikes.
        Ensures accuracy even when CSV/Aggregator history is short (e.g., at 09:15 in backtest).
        """
        # We only prime if the strike is not already being tracked by aggregators with sufficient history.
        # get_robust_ohlc (called by RSI/ROC) handles the API fetch and stitching if buffer is insufficient.
        # We trigger the fetch now to ensure readiness for evaluation.
        tf = self._v3_cfg('rsi_entry.tf', 1, int)
        rsi_period = self._v3_cfg('rsi.period', 14, int)
        roc_length = self._v3_cfg('roc.length', 9, int)
        await self.orchestrator.indicator_manager.calculate_combined_rsi(ce_key, pe_key, timestamp, tf=tf, period=rsi_period)
        await self.orchestrator.indicator_manager.calculate_combined_roc(ce_key, pe_key, timestamp, tf=tf, length=roc_length)

    async def get_best_straddle_candidates(self, ticks, timestamp):
        interval = self.orchestrator.config_manager.get_int(self.instrument_name, 'strike_interval', 50)
        anchor_price = self.orchestrator.get_anchor_price()
        if not anchor_price: return None, None
        atm = int(round(anchor_price / interval) * interval)
        expiry = self.orchestrator.atm_manager.get_expiry_by_mode('sell', 'signal')

        ltp_target = self._v3_cfg('ltp_target', None, float)
        if ltp_target is None: ltp_target = self._cfg('ltp_min', None, float)
        if ltp_target is None: ltp_target = 50.0

        offset = self._v3_cfg('v_slope_pool_offset', None, int)
        if offset is None: offset = self._v3_cfg('reentry_offset', 2, int)
        candidates = []
        for i in range(-offset, offset + 1):
            strike = atm + i * interval
            ce_key = self.orchestrator.atm_manager.find_instrument_key_by_strike(strike, 'CE', expiry)
            pe_key = self.orchestrator.atm_manager.find_instrument_key_by_strike(strike, 'PE', expiry)
            ce_ltp = ticks.get(ce_key, {}).get('ltp', 0) if ce_key else 0
            pe_ltp = ticks.get(pe_key, {}).get('ltp', 0) if pe_key else 0
            if ce_ltp >= ltp_target and pe_ltp >= ltp_target:
                candidates.append({'ce': {'strike': strike, 'key': ce_key, 'ltp': ce_ltp}, 'pe': {'strike': strike, 'key': pe_key, 'ltp': pe_ltp}, 'diff': abs(ce_ltp - pe_ltp), 'dist': abs(strike - anchor_price)})
        if not candidates: return None, None
        candidates.sort(key=lambda x: (x['diff'], x['dist']))
        return candidates[0]['ce'], candidates[0]['pe']
