import json
import os
import datetime
from utils.logger import logger


class SellManager:
    """
    Manages the short-strangle sell legs (V3 strategy).

    V3 design — both legs always enter and exit together:
    - Entry: pick strike from ATM + N ITM candidates; gate via vwap_slope sticky,
      cross_slope comparison, and optional 5-min V-slope toggle (all OR/AND configurable).
    - Exit: ALL exits close BOTH legs simultaneously:
        (a) LTP below min on either leg
        (b) Profit target % of combined entry premium
        (c) Ratio exit: max/min LTP >= threshold
        (d) Scalable TSL: per-lot rupee profit lock
        (e) Legacy TSL: rupee high-water mark
        (f) VWAP slope exit: combined VWAP rises >threshold% above session low
    - Smart Rolling: after (a/b/c) exits, re-entry attempted immediately if conditions met.
    - EOD: close_all() closes any open legs.
    """

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.state_file = f'config/sell_state_{orchestrator.instrument_name}.json'

        # ── Per-side trade state ──────────────────────────────────────────
        self.ce_placed = False
        self.pe_placed = False
        self.ce_strike = None
        self.pe_strike = None
        self.ce_key = None
        self.pe_key = None
        self.ce_entry_ltp = None
        self.pe_entry_ltp = None
        self.ce_entry_timestamp = None
        self.pe_entry_timestamp = None
        self.ce_contract = None
        self.pe_contract = None

        # ── Search state ──────────────────────────────────────────────────
        self.ce_searching = False   # True after candidates built
        self.pe_searching = False
        self.ce_candidates = []     # list of (strike, inst_key)
        self.pe_candidates = []

        # ── V3 exit state ─────────────────────────────────────────────────
        self.total_entry_premium = None       # CE_entry_ltp + PE_entry_ltp at entry
        self.tsl_scalable_lock_points = 0.0  # locked profit in points (scalable TSL)
        self.tsl_rupee_high_hit = False       # legacy TSL: high-water flag
        self._last_exit_timestamp = None      # for 60-sec cooldown after VWAP/TSL exits

        # ── EOD / misc state ─────────────────────────────────────────────
        self.strangle_closed = False
        self.expiry = None
        self.hedge_offset = None    # kept for potential future use

        # Backward-compat aliases (engine_manager uses buy_ce_key / buy_pe_key)
        self.buy_ce_key = None
        self.buy_pe_key = None

        # ── Sticky vwap_slope + cross_slope tracking ──────────────────────
        self._vwap_slope_sticky = {'CE': False, 'PE': False}
        self._cross_slope_count = {'CE': 0, 'PE': 0}
        self._last_sell_close_minute = None
        self._cross_slope_entry_ready = {'CE': False, 'PE': False}

    # ─────────────────────────────────────────────────────────────────────
    # Backward-compat properties (engine_manager checks .strangle_placed)
    # ─────────────────────────────────────────────────────────────────────

    @property
    def strangle_placed(self):
        return self.ce_placed or self.pe_placed

    @property
    def sell_ce_strike(self):
        return self.ce_strike

    @property
    def sell_pe_strike(self):
        return self.pe_strike

    @property
    def sell_ce_key(self):
        return self.ce_key

    @property
    def sell_pe_key(self):
        return self.pe_key

    @property
    def sell_ce_entry_ltp(self):
        return self.ce_entry_ltp

    @property
    def sell_pe_entry_ltp(self):
        return self.pe_entry_ltp

    # ─────────────────────────────────────────────────────────────────────
    # JSON config helpers
    # ─────────────────────────────────────────────────────────────────────

    def _cfg(self, key, type_func=float, fallback=None):
        """Read a value from NIFTY.sell.<key> in strategy_logic.json."""
        val = self.orchestrator.json_config.get_value(
            f"{self.orchestrator.instrument_name}.sell.{key}")
        return type_func(val) if val is not None else fallback

    def _exit_cfg(self, key, type_func=float, fallback=None):
        """Read a value from NIFTY.sell.exit_indicators.<key>."""
        val = self.orchestrator.json_config.get_value(
            f"{self.orchestrator.instrument_name}.sell.exit_indicators.{key}")
        return type_func(val) if val is not None else fallback

    def _is_v3_mode(self):
        """Check if V3 mode is enabled globally (NIFTY.v3_mode)."""
        val = self.orchestrator.json_config.get_value(
            f"{self.orchestrator.instrument_name}.v3_mode")
        return val is not None and str(val).lower() == 'true'

    def _v3_cfg(self, key, type_func=float, fallback=None):
        """Read a value from NIFTY.sell.v3.<key> in strategy_logic.json."""
        val = self.orchestrator.json_config.get_value(
            f"{self.orchestrator.instrument_name}.sell.v3.{key}")
        return type_func(val) if val is not None else fallback

    # ─────────────────────────────────────────────────────────────────────
    # Candidate building  (called once at sell start time)
    # ─────────────────────────────────────────────────────────────────────

    def _build_candidate_list(self, chain, side, atm, interval, itm_count):
        """
        Return [(strike, inst_key)] for ATM + `itm_count` ITM strikes.
        CE ITM = strikes BELOW spot: ATM, ATM-interval, ATM-2*interval, …
        PE ITM = strikes ABOVE spot: ATM, ATM+interval, ATM+2*interval, …
        List is sorted ATM-first.
        """
        options_key = 'call_options' if side == 'CE' else 'put_options'
        if side == 'CE':
            target_strikes = {float(atm + i * interval)
                              for i in range(0, -(itm_count + 1), -1)}
        else:
            target_strikes = {float(atm + i * interval)
                              for i in range(0, itm_count + 1)}

        candidates = []
        for entry in chain:
            strike = entry.get('strike_price')
            if strike is None or float(strike) not in target_strikes:
                continue
            side_data = entry.get(options_key) or {}
            inst_key = side_data.get('instrument_key')
            if inst_key:
                candidates.append((float(strike), inst_key))

        candidates.sort(key=lambda x: abs(x[0] - atm))
        logger.info(
            f"[SellManager] {side} candidates (ATM={atm}, ITM={itm_count}): "
            f"{[(int(s), k[:25]) for s, k in candidates]}")
        return candidates

    async def build_candidates_for_all_sides(self, timestamp):
        """
        Fetch option chain and build ATM+ITM candidate lists for CE and PE.
        Called once when the clock reaches sell.start_time.
        """
        expiry = self.orchestrator.atm_manager.signal_expiry_date
        if not expiry:
            logger.error("[SellManager] Cannot build candidates — signal_expiry_date not set.")
            return False

        self.expiry = expiry
        index_key = self.orchestrator.index_instrument_key
        interval = self.orchestrator.config_manager.get_int(
            self.orchestrator.instrument_name, 'strike_interval', 50)
        itm_count = self._cfg('itm_count', int, 2)

        # ATM for SELL candidates: Use Futures for MCX, Index for others.
        anchor_price = self.orchestrator.get_anchor_price()
        if not anchor_price or not interval:
            logger.error("[SellManager] ATM or strike interval unavailable — cannot build candidates.")
            return False
        atm = int(round(anchor_price / interval) * interval)

        price_type = "FUTURES" if self.orchestrator.is_mcx else "INDEX"
        logger.info(
            f"[SellManager] Fetching option chain: {index_key} expiry={expiry} "
            f"{price_type}={anchor_price:.2f} → ATM={atm}")
        chain = await self.orchestrator.rest_client.get_option_chain(index_key, expiry)
        if not chain:
            logger.error("[SellManager] Empty option chain — aborting candidate build.")
            return False

        self.ce_candidates = self._build_candidate_list(chain, 'CE', atm, interval, itm_count)
        self.pe_candidates = self._build_candidate_list(chain, 'PE', atm, interval, itm_count)

        if not self.ce_placed:
            self.ce_searching = True
        if not self.pe_placed:
            self.pe_searching = True

        # Subscribe all candidate keys to websocket so LTPs arrive in ticks
        all_keys = [k for _, k in self.ce_candidates + self.pe_candidates]
        if all_keys and hasattr(self.orchestrator, 'websocket') and self.orchestrator.websocket:
            self.orchestrator.websocket.subscribe(all_keys)
            logger.info(f"[SellManager] Subscribed {len(all_keys)} candidate keys to WS.")

        # Pre-subscribe INDEX-based BUY execution strikes (ATM ± 1 interval, both sides).
        # This runs ~5-10s before any BUY signal can fire, so ticks are already cached at entry.
        buy_mode_expiries = self.orchestrator.atm_manager.mode_expiries.get('buy')
        buy_trade_expiry = (buy_mode_expiries['trade'] if buy_mode_expiries
                            else self.orchestrator.atm_manager.trade_expiry_date)
        if buy_trade_expiry and hasattr(self.orchestrator, 'websocket') and self.orchestrator.websocket:
            exec_keys = []
            for offset in [0, interval, -interval]:
                exec_strike = atm + offset
                for side in ['CALL', 'PUT']:
                    key = self.orchestrator.atm_manager.find_instrument_key_by_strike(
                        exec_strike, side, buy_trade_expiry)
                    if key:
                        exec_keys.append(key)
            if exec_keys:
                self.orchestrator.websocket.subscribe(exec_keys)
                logger.info(
                    f"[SellManager] Pre-subscribed {len(exec_keys)} BUY execution keys "
                    f"at ATM={atm} ± {interval} (expiry={buy_trade_expiry})")
                if hasattr(self.orchestrator, 'price_feed_handler'):
                    self.orchestrator.price_feed_handler._rebuild_relevant_keys()
                    logger.info(f"[SellManager] Forced _relevant_keys_cache rebuild to include BUY execution keys.")

        logger.info(
            f"[SellManager] Ready — CE searching={self.ce_searching} "
            f"PE searching={self.pe_searching}")
        return True

    # ─────────────────────────────────────────────────────────────────────
    # LTP candidate selection helpers
    # ─────────────────────────────────────────────────────────────────────

    def _get_best_ltp_candidate(self, candidates, ticks, ltp_min, ltp_target):
        """
        From live tick data pick the candidate with LTP >= ltp_min and
        minimum |ltp - ltp_target|.
        Returns (strike, inst_key, ltp) or (None, None, None).
        """
        best_strike = best_key = best_ltp = None
        best_diff = float('inf')
        for strike, inst_key in candidates:
            tick = ticks.get(inst_key) or {}
            ltp = tick.get('ltp', 0) or 0
            if ltp >= ltp_min:
                diff = abs(ltp - ltp_target)
                if diff < best_diff:
                    best_diff = diff
                    best_strike, best_key, best_ltp = strike, inst_key, ltp
        return best_strike, best_key, best_ltp

    # ─────────────────────────────────────────────────────────────────────
    # VWAP slope helpers
    # ─────────────────────────────────────────────────────────────────────

    async def _check_slope_decreasing(self, inst_key, timestamp, side=None):
        """
        Returns True if the VWAP slope for inst_key is currently falling
        (diff_pct < threshold). If sticky=true in config and `side` is provided,
        once the slope has been declining it stays latched True until entry or exit
        resets it — so a brief recovery does not cancel the entry signal.
        """
        tf = self._cfg('indicators.vwap_slope.tf', int, 1)
        threshold = self._cfg('indicators.vwap_slope.threshold', float, 0.0)
        sticky = self._cfg('indicators.vwap_slope.sticky', lambda x: str(x).lower() == 'true', False)

        live_vwap = await self.orchestrator.indicator_manager.calculate_vwap(
            inst_key, timestamp)
        if live_vwap is None:
            return self._vwap_slope_sticky.get(side, False) if (sticky and side) else False

        result = await self.orchestrator.indicator_manager.get_vwap_slope_status(
            inst_key, timestamp, tf, count=1, live_vwap=live_vwap)
        if result is None or result[0] is None:
            return self._vwap_slope_sticky.get(side, False) if (sticky and side) else False

        _, _, v_curr, v_prev, _, _ = result
        if v_prev and v_prev > 0:
            declining = (v_curr - v_prev) / v_prev < threshold
        else:
            declining = False

        if side and sticky:
            if declining:
                if not self._vwap_slope_sticky[side]:
                    logger.info(f"[SellManager] vwap_slope sticky latched for {side}")
                self._vwap_slope_sticky[side] = True
                return True
            else:
                if self._vwap_slope_sticky[side]:
                    logger.debug(
                        f"[SellManager] {side} slope recovered — sticky still holding.")
                    return True
                return False
        return declining

    async def _get_slope_pct(self, inst_key, tf, timestamp):
        """
        Returns the VWAP slope percentage (v_curr - v_prev) / v_prev for inst_key,
        or None if data is unavailable.
        """
        live_vwap = await self.orchestrator.indicator_manager.calculate_vwap(
            inst_key, timestamp)
        if live_vwap is None:
            return None
        result = await self.orchestrator.indicator_manager.get_vwap_slope_status(
            inst_key, timestamp, tf, count=1, live_vwap=live_vwap)
        if result is None or result[2] is None or result[3] is None:
            return None
        _, _, v_curr, v_prev, _, _ = result
        if v_prev and v_prev > 0:
            return (v_curr - v_prev) / v_prev
        return None

    async def _check_cross_slope_comparison(self, side, ce_key, pe_key, timestamp, is_exit=False):
        """
        Compare CE VWAP slope % vs PE VWAP slope % to confirm entry or exit signal.

        ENTRY logic (is_exit=False):
          - CE side: fire when PE slope% > CE slope% (CE declining faster)
          - PE side: fire when CE slope% > PE slope% (PE declining faster)

        EXIT logic (is_exit=True):
          - CE side: fire when CE slope% > PE slope% (CE premium rising faster → exit short)
          - PE side: fire when PE slope% > CE slope% (PE premium rising faster → exit short)

        Returns True (pass-through) when cross_slope_comparison is disabled in config.
        Requires min_occurrences consecutive confirmations before returning True.
        """
        enabled = self._cfg(
            'indicators.cross_slope_comparison.enabled',
            lambda x: str(x).lower() == 'true', False)
        if not enabled:
            return True

        if not ce_key or not pe_key:
            logger.debug(f"[SellManager] cross_slope: missing key for {side} — skipping.")
            return False

        tf = self._cfg('indicators.cross_slope_comparison.tf', int, 1)
        min_occ = self._cfg('indicators.cross_slope_comparison.min_occurrences', int, 3)

        ce_slope = await self._get_slope_pct(ce_key, tf, timestamp)
        pe_slope = await self._get_slope_pct(pe_key, tf, timestamp)

        if ce_slope is None or pe_slope is None:
            logger.debug(
                f"[SellManager] cross_slope: slope data unavailable "
                f"CE={'N/A' if ce_slope is None else f'{ce_slope*100:.4f}%'} "
                f"PE={'N/A' if pe_slope is None else f'{pe_slope*100:.4f}%'}")
            return False

        if is_exit:
            condition = (ce_slope > pe_slope) if side == 'CE' else (pe_slope > ce_slope)
        else:
            condition = (pe_slope > ce_slope) if side == 'CE' else (ce_slope > pe_slope)

        if condition:
            self._cross_slope_count[side] += 1
        else:
            self._cross_slope_count[side] = 0

        mode = 'EXIT' if is_exit else 'ENTRY'
        logger.info(
            f"[SellManager] cross_slope CE={ce_slope*100:.4f}% PE={pe_slope*100:.4f}% "
            f"side={side} {mode} count={self._cross_slope_count[side]}/{min_occ}")

        return self._cross_slope_count[side] >= min_occ

    async def _get_vwap(self, inst_key, timestamp):
        return await self.orchestrator.indicator_manager.calculate_vwap(
            inst_key, timestamp)

    # ─────────────────────────────────────────────────────────────────────
    # Main per-tick method  (called from tick_processor every tick)
    # ─────────────────────────────────────────────────────────────────────

    async def on_tick(self, ticks, timestamp):
        """
        ticks: dict {inst_key: {'ltp': float, ...}} for live mode.
        Called every processed tick from tick_processor.
        V3: all exits close BOTH legs. Smart rolling attempted after LTP/profit/ratio exits.
        """
        if self.strangle_closed:
            return
        if not self.ce_candidates and not self.pe_candidates:
            return   # Candidates not built yet (before 9:20)

        current_minute = timestamp.replace(second=0, microsecond=0)
        if self._last_sell_close_minute is None or current_minute != self._last_sell_close_minute:
            self._last_sell_close_minute = current_minute
            await self._on_candle_close(timestamp)

        ltp_min = self._cfg('ltp_min', float, 50.0)
        ltp_target = self._cfg('ltp_target', float, 50.0)
        ltp_exit_min = self._v3_cfg('ltp_exit_min', float, 20.0)

        # ── Collect live LTPs for both legs ───────────────────────────
        ce_ltp = 0.0
        pe_ltp = 0.0
        if self.ce_placed and self.ce_key:
            tick = ticks.get(self.ce_key) or {}
            ce_ltp = float(tick.get('ltp', 0) or 0)
        if self.pe_placed and self.pe_key:
            tick = ticks.get(self.pe_key) or {}
            pe_ltp = float(tick.get('ltp', 0) or 0)

        # Only run exit checks when at least one leg is active
        if self.ce_placed or self.pe_placed:

            # ── 1. LTP decay exit → Smart Roll ────────────────────────
            if self.strangle_closed:
                return
            ltp_triggered = False
            if self.ce_placed and ce_ltp > 0 and ce_ltp < ltp_exit_min:
                logger.info(
                    f"[SellManager] CE LTP {ce_ltp:.2f} < exit_min {ltp_exit_min:.0f} "
                    f"— attempting smart roll.")
                ltp_triggered = True
            if not ltp_triggered and self.pe_placed and pe_ltp > 0 and pe_ltp < ltp_exit_min:
                logger.info(
                    f"[SellManager] PE LTP {pe_ltp:.2f} < exit_min {ltp_exit_min:.0f} "
                    f"— attempting smart roll.")
                ltp_triggered = True
            if ltp_triggered:
                rolled = await self._perform_smart_roll(
                    timestamp, ticks, f"LTP below {ltp_exit_min}")
                if not rolled:
                    await self.exit_both_legs(
                        timestamp, reason=f"LTP below {ltp_exit_min}", cooldown=False)
                return

            # ── 2. Profit target % exit → Smart Roll ──────────────────
            if self.strangle_closed:
                return
            profit_target_pct = self._v3_cfg('profit_target_pct', float, 0.0)
            if (profit_target_pct > 0 and self.total_entry_premium and
                    self.total_entry_premium > 0 and ce_ltp > 0 and pe_ltp > 0):
                current_premium = ce_ltp + pe_ltp
                profit_pts = self.total_entry_premium - current_premium
                target_pts = (profit_target_pct / 100.0) * self.total_entry_premium
                if profit_pts >= target_pts:
                    logger.info(
                        f"[SellManager] Profit target {profit_target_pct}% hit: "
                        f"entry={self.total_entry_premium:.2f} current={current_premium:.2f} "
                        f"profit={profit_pts:.1f}pts — attempting smart roll.")
                    rolled = await self._perform_smart_roll(
                        timestamp, ticks,
                        f"Profit target {profit_target_pct}% ({profit_pts:.1f}pts)")
                    if not rolled:
                        await self.exit_both_legs(
                            timestamp,
                            reason=f"Profit target {profit_target_pct}% hit",
                            cooldown=False)
                    return

            # ── 3. Ratio exit → Smart Roll ─────────────────────────────
            if self.strangle_closed:
                return
            ratio_enabled = self._v3_cfg(
                'ratio_exit.enabled', lambda x: str(x).lower() == 'true', False)
            if ratio_enabled and ce_ltp > 0 and pe_ltp > 0:
                ratio_threshold = self._v3_cfg('ratio_exit.threshold', float, 3.0)
                high_ltp = max(ce_ltp, pe_ltp)
                low_ltp = min(ce_ltp, pe_ltp)
                if low_ltp > 0:
                    current_ratio = high_ltp / low_ltp
                    if current_ratio >= ratio_threshold:
                        logger.info(
                            f"[SellManager] Ratio exit: {current_ratio:.2f} >= "
                            f"{ratio_threshold} — attempting smart roll.")
                        rolled = await self._perform_smart_roll(
                            timestamp, ticks,
                            f"Ratio {current_ratio:.2f} >= {ratio_threshold}")
                        if not rolled:
                            await self.exit_both_legs(
                                timestamp,
                                reason=f"Ratio exit {current_ratio:.2f}",
                                cooldown=False)
                        return

            # ── 4. Scalable TSL (per-lot rupee profit lock) ────────────
            if self.strangle_closed:
                return
            tsl_scalable_enabled = self._v3_cfg(
                'tsl_scalable.enabled', lambda x: str(x).lower() == 'true', False)
            if (tsl_scalable_enabled and self.total_entry_premium and
                    ce_ltp > 0 and pe_ltp > 0):
                current_premium = ce_ltp + pe_ltp
                profit_pts = self.total_entry_premium - current_premium

                base_profit_rs = self._v3_cfg('tsl_scalable.base_profit', float, 1000.0)
                base_lock_rs   = self._v3_cfg('tsl_scalable.base_lock',   float,  200.0)
                step_profit_rs = self._v3_cfg('tsl_scalable.step_profit',  float,  250.0)
                step_lock_rs   = self._v3_cfg('tsl_scalable.step_lock',    float,  200.0)

                ref_broker = next(
                    (b for b in self.orchestrator.broker_manager.brokers.values()
                     if b.is_configured_for_instrument(self.orchestrator.instrument_name)),
                    None)
                qty = (ref_broker.config_manager.get_int(
                    ref_broker.instance_name, 'quantity', 1) if ref_broker else 1)
                lot_size = (self.ce_contract.lot_size if self.ce_contract
                            else self.pe_contract.lot_size if self.pe_contract else 50)
                units = qty * lot_size

                base_profit_pts = base_profit_rs / lot_size
                base_lock_pts   = base_lock_rs   / lot_size
                step_profit_pts = step_profit_rs / lot_size
                step_lock_pts   = step_lock_rs   / lot_size

                if self.tsl_scalable_lock_points == 0 and profit_pts >= base_profit_pts:
                    self.tsl_scalable_lock_points = base_lock_pts
                    logger.info(
                        f"[SellManager] Scalable TSL: first lock triggered at "
                        f"{profit_pts:.2f}pts — locking {self.tsl_scalable_lock_points:.2f}pts "
                        f"(₹{self.tsl_scalable_lock_points * units:.0f})")

                if self.tsl_scalable_lock_points > 0:
                    extra = profit_pts - base_profit_pts
                    if extra >= step_profit_pts:
                        steps = int(extra // step_profit_pts)
                        new_lock = base_lock_pts + steps * step_lock_pts
                        if new_lock > self.tsl_scalable_lock_points:
                            self.tsl_scalable_lock_points = new_lock
                            logger.info(
                                f"[SellManager] Scalable TSL: lock raised to "
                                f"{self.tsl_scalable_lock_points:.2f}pts "
                                f"(₹{self.tsl_scalable_lock_points * units:.0f})")

                if (self.tsl_scalable_lock_points > 0 and
                        profit_pts <= self.tsl_scalable_lock_points):
                    await self.exit_both_legs(
                        timestamp,
                        reason=(f"Scalable TSL: profit {profit_pts:.2f}pts fell below "
                                f"lock {self.tsl_scalable_lock_points:.2f}pts"),
                        cooldown=True)
                    return

            # ── 5. Legacy TSL (rupee high-water mark) ─────────────────
            if self.strangle_closed:
                return
            tsl_legacy_enabled = self._v3_cfg(
                'tsl.enabled', lambda x: str(x).lower() == 'true', False)
            if (tsl_legacy_enabled and self.total_entry_premium and
                    ce_ltp > 0 and pe_ltp > 0):
                tsl_value_rs = self._v3_cfg('tsl.value', float, 0.0)
                current_premium = ce_ltp + pe_ltp
                profit_pts = self.total_entry_premium - current_premium
                ref_broker = next(
                    (b for b in self.orchestrator.broker_manager.brokers.values()
                     if b.is_configured_for_instrument(self.orchestrator.instrument_name)),
                    None)
                qty = (ref_broker.config_manager.get_int(
                    ref_broker.instance_name, 'quantity', 1) if ref_broker else 1)
                lot_size = (self.ce_contract.lot_size if self.ce_contract
                            else self.pe_contract.lot_size if self.pe_contract else 50)
                pnl_rs = profit_pts * qty * lot_size

                if not self.tsl_rupee_high_hit and pnl_rs >= tsl_value_rs:
                    self.tsl_rupee_high_hit = True
                    logger.info(
                        f"[SellManager] Legacy TSL high-water hit: "
                        f"PnL ₹{pnl_rs:.0f} >= ₹{tsl_value_rs:.0f}. Now locking.")
                if self.tsl_rupee_high_hit and pnl_rs <= tsl_value_rs:
                    await self.exit_both_legs(
                        timestamp,
                        reason=f"Legacy TSL: PnL ₹{pnl_rs:.0f} fell to lock ₹{tsl_value_rs:.0f}",
                        cooldown=True)
                    return

            # ── 6. VWAP slope exit evaluated at candle close (see _on_candle_close) ─
            pass

        # ── 7. Attempt entry for each searching side ───────────────────
        if self.strangle_closed:
            return

        # 60-second cooldown after VWAP/TSL exits
        if self._last_exit_timestamp is not None:
            elapsed = (timestamp - self._last_exit_timestamp).total_seconds()
            if elapsed < 60:
                return

        v3_mode = self._is_v3_mode()
        if v3_mode and not self.ce_placed and not self.pe_placed:
            ce_armed = (self.ce_searching and self.ce_candidates
                        and self._cross_slope_entry_ready.get('CE'))
            pe_armed = (self.pe_searching and self.pe_candidates
                        and self._cross_slope_entry_ready.get('PE'))
            if not (ce_armed and pe_armed):
                return
            ce_strike, ce_inst, ce_ltp = self._get_best_ltp_candidate(
                self.ce_candidates, ticks, ltp_min, ltp_target)
            pe_strike, pe_inst, pe_ltp = self._get_best_ltp_candidate(
                self.pe_candidates, ticks, ltp_min, ltp_target)
            if ce_strike is None or pe_strike is None:
                logger.debug(
                    f"[SellManager][V3] Dual pre-check failed: "
                    f"CE={ce_strike} PE={pe_strike} — waiting.")
                return
            await self._try_enter_side('CE', timestamp, ticks,
                                       self.ce_candidates, ltp_min, ltp_target)
            await self._try_enter_side('PE', timestamp, ticks,
                                       self.pe_candidates, ltp_min, ltp_target)
            if self.ce_placed and not self.pe_placed:
                logger.error("[SellManager][V3] CE placed but PE failed — single-leg state!")
            elif not self.ce_placed and self.pe_placed:
                logger.error("[SellManager][V3] PE placed but CE failed — single-leg state!")
        else:
            for side in ['CE', 'PE']:
                searching = self.ce_searching if side == 'CE' else self.pe_searching
                if not searching:
                    continue
                candidates = self.ce_candidates if side == 'CE' else self.pe_candidates
                if not candidates:
                    continue
                await self._try_enter_side(side, timestamp, ticks, candidates,
                                           ltp_min, ltp_target)

    # ─────────────────────────────────────────────────────────────────────
    # Candle-close gate evaluation
    # ─────────────────────────────────────────────────────────────────────

    async def _on_candle_close(self, timestamp):
        """
        Called once at each new 1-minute boundary.

        V3 mode (NIFTY.v3_mode=true):
          Slope-pair comparison is the canonical entry gate.
          Both CE and PE must show decelerating 5-min VWAP slopes
          (curr_slope < prev_slope) simultaneously.

        Legacy mode (v3_mode=false):
          Gate 1 (vwap_slope sticky, 1-min) OR Gate 2 (cross_slope comparison)
          arms each side independently.
        """
        last_close_ts = timestamp.replace(second=0, microsecond=0) - datetime.timedelta(minutes=1)
        v3_mode = self._is_v3_mode()

        if v3_mode:
            v_slope_tf = self._v3_cfg('v_slope_entry.tf', int, 5)
            ce_key_v = self.ce_key or (self.ce_candidates[0][1] if self.ce_candidates else None)
            pe_key_v = self.pe_key or (self.pe_candidates[0][1] if self.pe_candidates else None)

            both_armed = False
            if ce_key_v and pe_key_v:
                # UPDATED: V-Slope now takes both keys and returns combined slopes
                res = await self.orchestrator.indicator_manager.get_vwap_slope_pair(
                    ce_key_v, pe_key_v, last_close_ts, v_slope_tf)
                combined_curr, combined_prev = res[0], res[1]

                if (combined_curr is not None and combined_prev is not None):
                    both_armed = combined_curr < combined_prev
                    logger.info(
                        f"[SellManager][V3] combined slope: "
                        f"curr={combined_curr:.4f} prev={combined_prev:.4f} "
                        f"→ armed={both_armed}")

            self._cross_slope_entry_ready['CE'] = (both_armed
                and self.ce_searching and bool(self.ce_candidates))
            self._cross_slope_entry_ready['PE'] = (both_armed
                and self.pe_searching and bool(self.pe_candidates))
            if both_armed and self.ce_searching and self.pe_searching:
                logger.info("[SellManager][V3] Both sides armed for simultaneous entry.")
        else:
            tf = self._cfg('indicators.vwap_slope.tf', int, 1)
            for side in ['CE', 'PE']:
                searching = self.ce_searching if side == 'CE' else self.pe_searching
                if not searching:
                    continue
                candidates = self.ce_candidates if side == 'CE' else self.pe_candidates
                if not candidates:
                    continue

                inst_key = candidates[0][1]

                gate1 = False
                result = await self.orchestrator.indicator_manager.get_vwap_slope_status(
                    inst_key, last_close_ts, tf, count=1)
                if result is not None:
                    _, is_falling, _, _, _, _ = result
                    if is_falling:
                        if not self._vwap_slope_sticky[side]:
                            logger.info(f"[SellManager] vwap_slope sticky latched for {side}")
                        self._vwap_slope_sticky[side] = True
                    gate1 = self._vwap_slope_sticky[side]

                gate2 = False
                ce_key = self.ce_key or (self.ce_candidates[0][1] if self.ce_candidates else None)
                pe_key = self.pe_key or (self.pe_candidates[0][1] if self.pe_candidates else None)
                if ce_key and pe_key:
                    gate2 = await self._check_cross_slope_comparison(
                        side, ce_key, pe_key, last_close_ts, is_exit=False)

                self._cross_slope_entry_ready[side] = gate1 or gate2
                if self._cross_slope_entry_ready[side]:
                    logger.info(
                        f"[SellManager] Entry armed for {side} at close "
                        f"(gate1={gate1}, gate2={gate2}).")

        # ── VWAP slope exit: TF-boundary check when strangle is open ────────
        if self.ce_placed and self.pe_placed and not self.strangle_closed:
            await self._check_vwap_slope_exit(timestamp)

    # ─────────────────────────────────────────────────────────────────────
    # Entry attempt
    # ─────────────────────────────────────────────────────────────────────

    async def _try_enter_side(self, side, timestamp, ticks, candidates,
                               ltp_min, ltp_target):
        strike, inst_key, ltp = self._get_best_ltp_candidate(
            candidates, ticks, ltp_min, ltp_target)
        if strike is None:
            logger.debug(
                f"[SellManager] {side}: no candidate with LTP >= {ltp_min}. Still scanning.")
            return

        if not self._cross_slope_entry_ready[side]:
            logger.debug(
                f"[SellManager] {side} {int(strike)}: LTP={ltp:.2f} OK "
                f"but entry not armed (waiting for candle-close gate).")
            return

        # Resolve contract object for lot-size and order placement
        expiry_strikes = self.orchestrator.atm_manager.contract_lookup.get(
            self.expiry, {})
        contract = expiry_strikes.get(float(strike), {}).get(side)
        if not contract:
            logger.error(
                f"[SellManager] {side} strike {strike} not in contract_lookup — skipping.")
            return

        product_type = self._cfg('product_type', str, 'NRML')
        brokers = self.orchestrator.broker_manager.brokers.values()
        for broker in brokers:
            if not broker.is_configured_for_instrument(self.orchestrator.instrument_name):
                continue
            qty = (broker.config_manager.get_int(broker.instance_name, 'quantity', 1)
                   * contract.lot_size)
            if self.orchestrator.is_backtest or getattr(broker, 'paper_trade', False):
                logger.info(
                    f"[SellManager][PAPER SELL {product_type}] "
                    f"{side}: strike={int(strike)} LTP={ltp:.2f} qty={qty}")
            else:
                order_id = broker.place_order(
                    contract, 'SELL', qty, self.expiry, product_type=product_type)
                logger.info(
                    f"[SellManager] Sold {side} {int(strike)} "
                    f"order_id={order_id} LTP={ltp:.2f}")

        # Persist state
        if side == 'CE':
            self.ce_placed = True
            self.ce_searching = False
            self.ce_strike = strike
            self.ce_key = inst_key
            self.ce_entry_ltp = ltp
            self.ce_entry_timestamp = timestamp
            self.ce_contract = contract
        else:
            self.pe_placed = True
            self.pe_searching = False
            self.pe_strike = strike
            self.pe_key = inst_key
            self.pe_entry_ltp = ltp
            self.pe_entry_timestamp = timestamp
            self.pe_contract = contract

        # Subscribe placed key so LTP arrives immediately
        if not self.orchestrator.is_backtest:
            ws = getattr(self.orchestrator, 'websocket', None)
            if ws:
                ws.subscribe([inst_key])

        # Reset sticky, cross_slope count — entry placed, fresh state
        self._vwap_slope_sticky[side] = False
        self._cross_slope_count[side] = 0
        self._cross_slope_entry_ready[side] = False

        # When BOTH legs are now placed, record total entry premium and reset V3 exit state
        if self.ce_placed and self.pe_placed:
            ce_e = self.ce_entry_ltp or 0
            pe_e = self.pe_entry_ltp or 0
            self.total_entry_premium = ce_e + pe_e
            self.tsl_scalable_lock_points = 0.0
            self.tsl_rupee_high_hit = False
            logger.info(
                f"[SellManager] Both legs placed — total_entry_premium={self.total_entry_premium:.2f} "
                f"(CE={ce_e:.2f} + PE={pe_e:.2f}). V3 exit state reset.")

        self.save_state()
        logger.info(
            f"[SellManager] ✔ {side} leg entered: strike={int(strike)} "
            f"LTP={ltp:.2f} key={inst_key} product={product_type}")

    # ─────────────────────────────────────────────────────────────────────
    # V3 VWAP slope exit — TF-boundary combined VWAP comparison
    # ─────────────────────────────────────────────────────────────────────

    async def _check_vwap_slope_exit(self, timestamp):
        """
        V3 VWAP slope exit: TF-boundary check evaluated at each candle close.
        Compares current TF combined VWAP (CE+PE) against the previous TF combined
        VWAP. Exits BOTH legs when curr_combined > prev_combined * (1 + threshold),
        where threshold is read from sell.exit_indicators.combined_vwap_slope.threshold
        (stored as decimal, e.g. 0.01 = 1%).

        Config path: NIFTY.sell.exit_indicators.combined_vwap_slope
          enabled   (bool)   — feature flag
          tf        (int)    — timeframe in minutes for VWAP slope (default 1)
          threshold (float)  — decimal threshold, e.g. 0.01 (UI shows 1.0 via pct conversion)
        """
        enabled = self._exit_cfg(
            'combined_vwap_slope.enabled', lambda x: str(x).lower() == 'true', False)
        if not enabled:
            return
        if not (self.ce_placed and self.pe_placed):
            return
        if not self.ce_key or not self.pe_key:
            return

        tf = self._exit_cfg('combined_vwap_slope.tf') or 1
        threshold = float(self._exit_cfg('combined_vwap_slope.threshold') or 0.01)

        # Get curr and prev VWAP for CE
        ce_result = await self.orchestrator.indicator_manager.get_vwap_slope_status(
            self.ce_key, timestamp, int(tf), count=1)
        if ce_result is None:
            return
        _, _, ce_curr, ce_prev, _, _ = ce_result

        # Get curr and prev VWAP for PE
        pe_result = await self.orchestrator.indicator_manager.get_vwap_slope_status(
            self.pe_key, timestamp, int(tf), count=1)
        if pe_result is None:
            return
        _, _, pe_curr, pe_prev, _, _ = pe_result

        if None in (ce_curr, ce_prev, pe_curr, pe_prev):
            return
        if ce_prev <= 0 or pe_prev <= 0:
            return

        combined_curr = ce_curr + pe_curr
        combined_prev = ce_prev + pe_prev
        trigger_level = combined_prev * (1.0 + threshold)

        if combined_curr > trigger_level:
            rise_pct = (combined_curr - combined_prev) / combined_prev * 100
            logger.info(
                f"[SellManager] VWAP slope exit: combined_curr={combined_curr:.2f} > "
                f"combined_prev={combined_prev:.2f} by {rise_pct:.2f}% "
                f"(threshold={threshold*100:.1f}%) — exiting both legs.")
            await self.exit_both_legs(
                timestamp,
                reason=(f"VWAP slope exit: curr={combined_curr:.2f} vs "
                        f"prev={combined_prev:.2f} (+{rise_pct:.2f}%)"),
                cooldown=True)
        else:
            logger.debug(
                f"[SellManager] VWAP slope check: curr={combined_curr:.2f} "
                f"prev={combined_prev:.2f} threshold={threshold*100:.1f}% — holding.")

    # ─────────────────────────────────────────────────────────────────────
    # Exit both legs (straddle/strangle — always exit together)
    # ─────────────────────────────────────────────────────────────────────

    async def exit_both_legs(self, timestamp, reason='Exit', cooldown=False):
        """
        Exit both CE and PE legs simultaneously and close the strangle.
        V3 sell strategy: no partial exits — both legs always exit together.
        cooldown=True → set _last_exit_timestamp so re-entry is blocked for 60s.
        """
        logger.info(f"[SellManager] Exiting BOTH legs — reason: {reason}")
        for side in ['CE', 'PE']:
            placed = self.ce_placed if side == 'CE' else self.pe_placed
            if placed:
                await self.exit_side(side, timestamp, reason=reason)

        # Reset V3 exit state
        self.total_entry_premium = None
        self.tsl_scalable_lock_points = 0.0
        self.tsl_rupee_high_hit = False
        if cooldown:
            self._last_exit_timestamp = timestamp
            logger.info(
                f"[SellManager] 60-second re-entry cooldown started after: {reason}")

        self.strangle_closed = True
        self.save_state()
        logger.info(f"[SellManager] Both legs exited. Strangle closed for the day.")

    # ─────────────────────────────────────────────────────────────────────
    # Exit one leg
    # ─────────────────────────────────────────────────────────────────────

    async def exit_side(self, side, timestamp, reason='Exit'):
        """
        Buy back one sell leg and restart the search for that side.
        """
        strike = self.ce_strike if side == 'CE' else self.pe_strike
        key = self.ce_key if side == 'CE' else self.pe_key
        entry_ltp = self.ce_entry_ltp if side == 'CE' else self.pe_entry_ltp
        entry_ts = self.ce_entry_timestamp if side == 'CE' else self.pe_entry_timestamp
        contract = self.ce_contract if side == 'CE' else self.pe_contract

        if strike is None or contract is None:
            logger.warning(
                f"[SellManager] exit_side({side}): no active position — skipping.")
            return

        exit_ltp = None
        product_type = self._cfg('product_type', str, 'NRML')
        brokers = self.orchestrator.broker_manager.brokers.values()

        for broker in brokers:
            if not broker.is_configured_for_instrument(self.orchestrator.instrument_name):
                continue
            qty = (broker.config_manager.get_int(broker.instance_name, 'quantity', 1)
                   * contract.lot_size)
            if self.orchestrator.is_backtest or getattr(broker, 'paper_trade', False):
                if self.orchestrator.is_backtest and key:
                    exit_ltp = await self.orchestrator._get_ltp_for_backtest_instrument(
                        key, timestamp)
                logger.info(
                    f"[SellManager][PAPER BUY {product_type}] {side}: "
                    f"strike={int(strike)} qty={qty} reason={reason}")
            else:
                exit_ltp = self.orchestrator.state_manager.get_ltp(key) or entry_ltp
                order_id = broker.place_order(
                    contract, 'BUY', qty, self.expiry, product_type=product_type)
                logger.info(
                    f"[SellManager] Closed {side} {int(strike)} "
                    f"order_id={order_id} exit_ltp={exit_ltp:.2f} reason={reason}")

        # Record PnL — always log to live_trade_log; also log to pnl_tracker in backtest
        if entry_ltp and exit_ltp:
            try:
                from .live_trade_log import LiveTradeLog
                trade_log = getattr(self.orchestrator, 'trade_log', None)
                if trade_log:
                    pnl_pts_raw = entry_ltp - exit_ltp
                    trade_log.add(LiveTradeLog.make_entry(
                        trade_type='SELL',
                        direction=side,
                        strike=strike,
                        entry_price=entry_ltp,
                        exit_price=exit_ltp,
                        pnl_pts=pnl_pts_raw,
                        pnl_rs=None,
                        reason=reason,
                        order_id=str(order_id) if order_id else '',
                        timestamp=timestamp,
                        entry_time=entry_ts
                    ))
            except Exception as _tl_ex:
                logger.debug(f"[SellManager] trade_log append failed: {_tl_ex}")

        if entry_ltp and exit_ltp and self.orchestrator.pnl_tracker:
            ref_broker = next(
                (b for b in brokers
                 if b.is_configured_for_instrument(self.orchestrator.instrument_name)), None)
            broker_qty = (ref_broker.config_manager.get_int(
                ref_broker.instance_name, 'quantity', 1) if ref_broker else 1)
            pnl = (entry_ltp - exit_ltp) * contract.lot_size * broker_qty
            pnl_side = 'CALL' if side == 'CE' else 'PUT'
            logger.info(
                f"[SellManager] {side} PnL: entry={entry_ltp:.2f} "
                f"exit={exit_ltp:.2f} pnl=₹{pnl:+.2f}")
            self.orchestrator.pnl_tracker.trade_history.append({
                'instrument_key': key,
                'entry_price': entry_ltp,
                'exit_price': exit_ltp,
                'entry_timestamp': entry_ts,
                'exit_timestamp': timestamp,
                'pnl': pnl,
                'lot_size': contract.lot_size,
                'quantity': broker_qty,
                'status': 'CLOSED',
                'side': pnl_side,
                'strike_price': strike,
                'contract': contract,
                'strategy_log': f'SELL {product_type} leg — {reason}',
                'entry_type': 'SELL',
            })

        # Void sticky, cross_slope count — exit event means fresh checking starts
        self._vwap_slope_sticky[side] = False
        self._cross_slope_count[side] = 0
        self._cross_slope_entry_ready[side] = False

        # Reset this side → restart search
        if side == 'CE':
            self.ce_placed = False
            self.ce_strike = None
            self.ce_key = None
            self.ce_entry_ltp = None
            self.ce_entry_timestamp = None
            self.ce_contract = None
            self.ce_searching = True
        else:
            self.pe_placed = False
            self.pe_strike = None
            self.pe_key = None
            self.pe_entry_ltp = None
            self.pe_entry_timestamp = None
            self.pe_contract = None
            self.pe_searching = True

        self.save_state()
        logger.info(f"[SellManager] {side} exited ({reason}). Sticky voided. Fresh search started.")

    # ─────────────────────────────────────────────────────────────────────
    # Smart Rolling — exit + immediate re-entry after profitable exits
    # ─────────────────────────────────────────────────────────────────────

    async def _perform_smart_roll(self, timestamp, ticks, reason):
        """
        Smart Roll (V3 rollover): after LTP/profit/ratio exits, attempt immediate re-entry.
        - Blocked if smart_rolling_enabled=false or past entry_end_time (14:00).
        - Same strikes: virtual roll — no broker orders, just reset premium reference.
        - Different strikes: exit current positions + place new orders immediately.
        Returns True if successfully rolled (position maintained or reset),
        False if caller should fall through to full exit_both_legs().
        """
        smart_enabled = self._v3_cfg(
            'smart_rolling_enabled', lambda x: str(x).lower() == 'true', False)
        if not smart_enabled:
            return False

        entry_end_str = self._v3_cfg('entry_end_time', str, '14:00:00')
        try:
            if len(entry_end_str.split(':')) == 2:
                end_t = datetime.datetime.strptime(entry_end_str, '%H:%M').time()
            else:
                end_t = datetime.datetime.strptime(entry_end_str, '%H:%M:%S').time()
        except Exception:
            end_t = datetime.time(14, 0, 0)
        if timestamp.time() >= end_t:
            logger.info(
                f"[SellManager] Smart roll skipped — past entry end time {entry_end_str}.")
            return False

        ltp_min = self._cfg('ltp_min', float, 50.0)
        ltp_target = self._cfg('ltp_target', float, 50.0)

        new_ce_strike, new_ce_key, new_ce_ltp = self._get_best_ltp_candidate(
            self.ce_candidates, ticks, ltp_min, ltp_target)
        new_pe_strike, new_pe_key, new_pe_ltp = self._get_best_ltp_candidate(
            self.pe_candidates, ticks, ltp_min, ltp_target)

        if new_ce_strike is None or new_pe_strike is None:
            logger.info(
                f"[SellManager] Smart roll: no valid candidates (LTP >= {ltp_min}). "
                f"Falling back to full exit.")
            return False

        old_ce_strike = self.ce_strike
        old_pe_strike = self.pe_strike
        same_strikes = (new_ce_strike == old_ce_strike and new_pe_strike == old_pe_strike)

        expiry_strikes = self.orchestrator.atm_manager.contract_lookup.get(
            self.expiry, {})
        new_ce_contract = expiry_strikes.get(float(new_ce_strike), {}).get('CE')
        new_pe_contract = expiry_strikes.get(float(new_pe_strike), {}).get('PE')

        if not new_ce_contract or not new_pe_contract:
            logger.warning(
                "[SellManager] Smart roll: contract lookup failed. Full exit.")
            return False

        product_type = self._cfg('product_type', str, 'NRML')

        if same_strikes:
            # Virtual roll — same strikes still held, just reset premium reference
            self.ce_entry_ltp = new_ce_ltp
            self.pe_entry_ltp = new_pe_ltp
            self.ce_entry_timestamp = timestamp
            self.pe_entry_timestamp = timestamp
            self.total_entry_premium = (new_ce_ltp or 0) + (new_pe_ltp or 0)
            self.tsl_scalable_lock_points = 0.0
            self.tsl_rupee_high_hit = False
            self.save_state()
            logger.info(
                f"[SellManager] Virtual roll: same strikes CE={int(new_ce_strike)} "
                f"PE={int(new_pe_strike)} — no orders placed, premium reset to "
                f"{self.total_entry_premium:.2f}")
            return True

        # Different strikes: exit current, place new orders immediately
        logger.info(
            f"[SellManager] Smart roll: new strikes CE={int(new_ce_strike)} "
            f"PE={int(new_pe_strike)} — exiting current and re-entering.")

        for side in ['CE', 'PE']:
            placed = self.ce_placed if side == 'CE' else self.pe_placed
            if placed:
                await self.exit_side(side, timestamp, reason=f"Smart Roll: {reason}")

        # Reset V3 state
        self.tsl_scalable_lock_points = 0.0
        self.tsl_rupee_high_hit = False
        self._last_exit_timestamp = None  # no cooldown for smart rolls

        # Place new orders for both legs
        brokers = self.orchestrator.broker_manager.brokers.values()
        for side, strike, key, ltp, contract in [
            ('CE', new_ce_strike, new_ce_key, new_ce_ltp, new_ce_contract),
            ('PE', new_pe_strike, new_pe_key, new_pe_ltp, new_pe_contract),
        ]:
            for broker in brokers:
                if not broker.is_configured_for_instrument(self.orchestrator.instrument_name):
                    continue
                qty = (broker.config_manager.get_int(broker.instance_name, 'quantity', 1)
                       * contract.lot_size)
                if self.orchestrator.is_backtest or getattr(broker, 'paper_trade', False):
                    logger.info(
                        f"[SellManager][PAPER ROLL {product_type}] "
                        f"{side}: strike={int(strike)} LTP={ltp:.2f} qty={qty}")
                else:
                    order_id = broker.place_order(
                        contract, 'SELL', qty, self.expiry, product_type=product_type)
                    logger.info(
                        f"[SellManager] Roll: Sold {side} {int(strike)} "
                        f"order_id={order_id} LTP={ltp:.2f}")

        # Persist new leg state
        self.ce_placed = True
        self.ce_searching = False
        self.ce_strike = new_ce_strike
        self.ce_key = new_ce_key
        self.ce_entry_ltp = new_ce_ltp
        self.ce_entry_timestamp = timestamp
        self.ce_contract = new_ce_contract

        self.pe_placed = True
        self.pe_searching = False
        self.pe_strike = new_pe_strike
        self.pe_key = new_pe_key
        self.pe_entry_ltp = new_pe_ltp
        self.pe_entry_timestamp = timestamp
        self.pe_contract = new_pe_contract

        self.total_entry_premium = (new_ce_ltp or 0) + (new_pe_ltp or 0)
        self.save_state()
        logger.info(
            f"[SellManager] Smart roll complete: CE={int(new_ce_strike)} "
            f"PE={int(new_pe_strike)} new_premium={self.total_entry_premium:.2f}")
        return True

    # ─────────────────────────────────────────────────────────────────────
    # EOD close — close whatever is still open
    # ─────────────────────────────────────────────────────────────────────

    async def close_all(self, timestamp):
        """Called at end-of-day to buy back any open sell legs."""
        if self.strangle_closed:
            return

        closed_any = False
        for side in ['CE', 'PE']:
            placed = self.ce_placed if side == 'CE' else self.pe_placed
            if placed:
                await self.exit_side(side, timestamp, reason='EOD Close')
                closed_any = True

        self.strangle_closed = True
        self.save_state()
        if not closed_any:
            logger.info("[SellManager] close_all: no open legs to close.")
        else:
            logger.info("[SellManager] EOD close completed.")

    # ─────────────────────────────────────────────────────────────────────
    # Backward-compat stub used by base_orchestrator.broadcast_signal
    # ─────────────────────────────────────────────────────────────────────

    def get_buy_strike(self, direction):
        """
        No longer drives hedge selection — buy strike is always ATM
        (handled by signal_monitor / trade_executor).
        Returning (None, None) allows broadcast_signal to proceed normally.
        """
        return None, None

    # ─────────────────────────────────────────────────────────────────────
    # Backtest support
    # ─────────────────────────────────────────────────────────────────────

    async def _find_best_backtest_candidate(self, candidates, timestamp, ltp_min, ltp_target):
        """Backtest: fetch real historical LTPs and apply the same selection logic."""
        best_diff = float('inf')
        best_strike = best_key = best_ltp = None
        for strike, inst_key in candidates:
            hist_ltp = await self.orchestrator._get_ltp_for_backtest_instrument(
                inst_key, timestamp)
            if hist_ltp and hist_ltp >= ltp_min:
                diff = abs(hist_ltp - ltp_target)
                if diff < best_diff:
                    best_diff = diff
                    best_strike, best_key, best_ltp = strike, inst_key, hist_ltp
        return best_strike, best_key, best_ltp

    async def execute_short_strangle_backtest(self, timestamp):
        """
        Backtest entry point — evaluate candidates at `timestamp`, pick best by
        LTP, check slope, enter each side that qualifies.
        Called from backtest orchestrator when the sell start time is reached.
        """
        if self.strangle_closed or (self.ce_placed and self.pe_placed):
            return
        if not self.ce_candidates and not self.pe_candidates:
            logger.warning("[SellManager][Backtest] No candidates built — skipping entry.")
            return

        ltp_min = self._cfg('ltp_min', float, 50.0)
        ltp_target = self._cfg('ltp_target', float, 50.0)
        product_type = self._cfg('product_type', str, 'NRML')

        for side in ['CE', 'PE']:
            placed = self.ce_placed if side == 'CE' else self.pe_placed
            if placed:
                continue
            candidates = self.ce_candidates if side == 'CE' else self.pe_candidates
            strike, inst_key, ltp = await self._find_best_backtest_candidate(
                candidates, timestamp, ltp_min, ltp_target)
            if strike is None:
                logger.info(
                    f"[SellManager][Backtest] {side}: no candidate with "
                    f"hist LTP >= {ltp_min} at {timestamp}.")
                continue

            slope_ok = await self._check_slope_decreasing(inst_key, timestamp)
            if not slope_ok:
                logger.info(
                    f"[SellManager][Backtest] {side} {int(strike)}: "
                    f"LTP={ltp:.2f} OK but slope not decreasing at {timestamp}.")
                continue

            expiry_strikes = self.orchestrator.atm_manager.contract_lookup.get(
                self.expiry, {})
            contract = expiry_strikes.get(float(strike), {}).get(side)
            if not contract:
                logger.error(
                    f"[SellManager][Backtest] {side} strike {strike} "
                    f"not in contract_lookup.")
                continue

            logger.info(
                f"[SellManager][Backtest PAPER SELL {product_type}] "
                f"{side}: strike={int(strike)} histLTP={ltp:.2f}")

            if side == 'CE':
                self.ce_placed = True
                self.ce_searching = False
                self.ce_strike = strike
                self.ce_key = inst_key
                self.ce_entry_ltp = ltp
                self.ce_entry_timestamp = timestamp
                self.ce_contract = contract
            else:
                self.pe_placed = True
                self.pe_searching = False
                self.pe_strike = strike
                self.pe_key = inst_key
                self.pe_entry_ltp = ltp
                self.pe_entry_timestamp = timestamp
                self.pe_contract = contract

        self.save_state()

    # ─────────────────────────────────────────────────────────────────────
    # State persistence
    # ─────────────────────────────────────────────────────────────────────

    def save_state(self):
        state = {
            'ce_placed': self.ce_placed,
            'pe_placed': self.pe_placed,
            'ce_strike': self.ce_strike,
            'pe_strike': self.pe_strike,
            'ce_key': self.ce_key,
            'pe_key': self.pe_key,
            'ce_entry_ltp': self.ce_entry_ltp,
            'pe_entry_ltp': self.pe_entry_ltp,
            'strangle_closed': self.strangle_closed,
            'expiry': self.expiry.isoformat() if self.expiry else None,
            'total_entry_premium': self.total_entry_premium,
            'tsl_scalable_lock_points': self.tsl_scalable_lock_points,
            'tsl_rupee_high_hit': self.tsl_rupee_high_hit,
        }
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"[SellManager] Failed to save state: {e}")

    def load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            expiry_str = state.get('expiry')
            expiry = datetime.date.fromisoformat(expiry_str) if expiry_str else None
            if expiry and expiry < datetime.date.today():
                logger.info(f"[SellManager] Stale state (expiry {expiry}), ignoring.")
                return
            self.ce_placed = state.get('ce_placed', False)
            self.pe_placed = state.get('pe_placed', False)
            self.ce_strike = state.get('ce_strike')
            self.pe_strike = state.get('pe_strike')
            self.ce_key = state.get('ce_key')
            self.pe_key = state.get('pe_key')
            self.ce_entry_ltp = state.get('ce_entry_ltp')
            self.pe_entry_ltp = state.get('pe_entry_ltp')
            self.strangle_closed = state.get('strangle_closed', False)
            self.expiry = expiry
            self.total_entry_premium = state.get('total_entry_premium')
            self.tsl_scalable_lock_points = state.get('tsl_scalable_lock_points', 0.0)
            self.tsl_rupee_high_hit = state.get('tsl_rupee_high_hit', False)
            logger.info(
                f"[SellManager] State loaded: CE={self.ce_placed} PE={self.pe_placed} "
                f"expiry={expiry} total_premium={self.total_entry_premium}")
        except Exception as e:
            logger.error(f"[SellManager] Failed to load state: {e}")
