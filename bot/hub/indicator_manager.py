from utils.logger import logger
from utils.support_resistance import SupportResistanceCalculator
import pandas as pd
import asyncio
import pytz
import datetime
from datetime import time
from hub.indicators.roc import ROCIndicator
from hub.indicators.rsi import RSIIndicator
from hub.indicators.vwap import VWAPIndicator
from hub.sell_v3.rust_bridge import RustBridge

class IndicatorManager:
    def __init__(self, parent):
        # Support both global Orchestrator and isolated UserSession
        if hasattr(parent, 'user_id'):
            self.user_session = parent
            self.orchestrator = parent.orchestrator
            self.state_manager = parent.state_manager
        else:
            self.user_session = None
            self.orchestrator = parent
            self.state_manager = parent.state_manager

        self.data_manager = self.orchestrator.data_manager
        self.config_manager = self.orchestrator.config_manager
        self.atm_manager = self.orchestrator.atm_manager

        self._vwap_state = {}
        self._vwap_slope_cache = {}
        self._sr_cache = {}
        self._r1_profit_cache = {}
        self._index_915_range = {} # (index_key, date) -> (high, low)

    async def get_robust_ohlc(self, inst_key, timeframe_minutes, timestamp, include_current=True, skip_api=False, buffer_count=20):
        """
        Returns OHLC data for the given instrument and timeframe.
        Guarantees a sufficient buffer by stitching historical API data with live aggregator data.
        """
        import re
        parsed_minutes = 1
        if isinstance(timeframe_minutes, int):
            parsed_minutes = timeframe_minutes
        elif isinstance(timeframe_minutes, str):
            match = re.search(r'\d+', timeframe_minutes)
            if match: parsed_minutes = int(match.group())

        if self.orchestrator.is_backtest:
            # OPTIMIZED: Delegate history fulfillment to DataManager's internal day-by-day logic.
            # This avoids the redundant nested loop and inconsistent lookback boundaries.
            return await self.data_manager.get_historical_ohlc(
                inst_key, parsed_minutes, current_timestamp=timestamp,
                include_current=include_current,
                min_candles=buffer_count
            )

        # 1. Try Live Aggregator
        aggregator = None
        if parsed_minutes == self.orchestrator.entry_aggregator.interval_minutes:
            aggregator = self.orchestrator.entry_aggregator
        elif parsed_minutes == self.orchestrator.one_min_aggregator.interval_minutes:
            aggregator = self.orchestrator.one_min_aggregator
        elif parsed_minutes == self.orchestrator.five_min_aggregator.interval_minutes:
            aggregator = self.orchestrator.five_min_aggregator

        live_ohlc = None
        if aggregator:
            live_ohlc = aggregator.get_historical_ohlc(inst_key)
            if aggregator.interval_minutes == 1 and include_current:
                current = aggregator.get_all_current_ohlc().get(inst_key)
                if current:
                    curr_df = pd.DataFrame([current]).set_index('timestamp')
                    curr_df.index = pd.to_datetime(curr_df.index)
                    curr_tz = getattr(curr_df.index, 'tzinfo', getattr(curr_df.index, 'tz', None))
                    if curr_tz is None:
                        curr_df.index = curr_df.index.tz_localize('Asia/Kolkata')
                    else:
                        curr_df.index = curr_df.index.tz_convert('Asia/Kolkata')
                    live_ohlc = pd.concat([live_ohlc, curr_df]) if live_ohlc is not None else curr_df
                    live_ohlc = live_ohlc[~live_ohlc.index.duplicated(keep='last')].sort_index()

        # 2. Resampling fallback for live data (if specific TF aggregator is empty/slow)
        if (live_ohlc is None or live_ohlc.empty) and parsed_minutes > 1:
            one_min_live = self.orchestrator.entry_aggregator.get_historical_ohlc(inst_key)
            if one_min_live is not None and not one_min_live.empty:
                # Filter up to timestamp to respect "Closed Candles" if requested via anchor_ts
                if timestamp:
                    ts_filter = pd.Timestamp(timestamp)
                    if ts_filter.tzinfo is None:
                        ts_filter = ts_filter.tz_localize('Asia/Kolkata')
                    else:
                        ts_filter = ts_filter.tz_convert('Asia/Kolkata')

                    if one_min_live.index.tz is None:
                        one_min_live.index = one_min_live.index.tz_localize('Asia/Kolkata')
                    else:
                        one_min_live.index = one_min_live.index.tz_convert('Asia/Kolkata')

                    one_min_live = one_min_live[one_min_live.index <= ts_filter]

                resample_freq = f"{parsed_minutes}min"
                live_ohlc = one_min_live.resample(resample_freq).agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()

                # STRICT: Drop last bucket if it doesn't contain a full timeframe's worth of 1m candles
                # This ensures we "ALWAYS GET TEH CLOSED CANDELS DATA" as requested.
                if not live_ohlc.empty:
                    last_bucket_ts = live_ohlc.index[-1]
                    count = len(one_min_live[one_min_live.index >= last_bucket_ts])
                    if count < parsed_minutes:
                        live_ohlc = live_ohlc.iloc[:-1]

        # 3. Historical stitching logic
        if skip_api:
            return live_ohlc

        ohlc = live_ohlc
        # If live data is missing or insufficient for technical period, fetch from API day-by-day
        # User requirement: Check one day before, if found, don't check next prev day.
        # We check day-by-day descending and STOP as soon as buffer is met or a day yields data.
        if ohlc is None or len(ohlc) < buffer_count:
            # OPTIMIZATION: Delegate to DataManager's day-by-day fetcher
            hist_api = await self.data_manager.fetch_and_cache_api_ohlc(inst_key, timestamp.date(), days_back=10)

            if hist_api is not None and not hist_api.empty:
                # Stitch with live data
                if ohlc is not None and not ohlc.empty:
                    # Filter API data to be before live data
                    # Ensure matching timezone awareness for comparison
                    if hist_api.index.tz is None: hist_api.index = hist_api.index.tz_localize('Asia/Kolkata')
                    else: hist_api.index = hist_api.index.tz_convert('Asia/Kolkata')

                    if ohlc.index.tz is None: ohlc.index = ohlc.index.tz_localize('Asia/Kolkata')
                    else: ohlc.index = ohlc.index.tz_convert('Asia/Kolkata')

                    hist_api = hist_api[hist_api.index < ohlc.index[0]]
                    ohlc = pd.concat([hist_api, ohlc]).sort_index()
                else:
                    ohlc = hist_api

                # Trim to timestamp
                ts_limit = pd.Timestamp(timestamp)
                if ts_limit.tzinfo is None:
                    ts_limit = ts_limit.tz_localize('Asia/Kolkata')
                else:
                    ts_limit = ts_limit.tz_convert('Asia/Kolkata')

                # Force OHLC index to be tz-aware Asia/Kolkata
                if ohlc.index.tz is None:
                    ohlc.index = ohlc.index.tz_localize('Asia/Kolkata')
                else:
                    ohlc.index = ohlc.index.tz_convert('Asia/Kolkata')

                ohlc = ohlc[ohlc.index <= ts_limit]

        if ohlc is not None and not ohlc.empty:
            if not isinstance(ohlc.index, pd.DatetimeIndex):
                # Index contains raw datetime objects — may be tz-aware or tz-naive.
                # pd.to_datetime() raises ValueError on tz-aware objects unless utc=True.
                try:
                    ohlc.index = pd.to_datetime(ohlc.index)
                except (ValueError, TypeError):
                    ohlc.index = pd.to_datetime(ohlc.index, utc=True)
            idx_tz = getattr(ohlc.index, 'tzinfo', getattr(ohlc.index, 'tz', None))
            if idx_tz is None:
                ohlc.index = ohlc.index.tz_localize('Asia/Kolkata')
            else:
                ohlc.index = ohlc.index.tz_convert('Asia/Kolkata')

        return ohlc

    async def calculate_vwap(self, inst_key, timestamp, strict_history=False):
        """
        High-fidelity VWAP calculation utilizing stored tick-by-tick ATP data.
        Prioritizes the exact minute bucket from state history for precision.
        strict_history=False: Ignores live fallbacks and only uses stored 1m boundaries.
        """
        if not inst_key:
            return None

        # PERFORMANCE: Backtest VWAP Minute Cache
        if self.orchestrator.is_backtest:
            if not hasattr(self, '_bt_vwap_cache'): self._bt_vwap_cache = {}
            # Boundary minute key
            boundary_min = pd.Timestamp(timestamp).replace(second=0, microsecond=0)
            if boundary_min.tzinfo is None:
                boundary_min = boundary_min.tz_localize('Asia/Kolkata')
            else:
                boundary_min = boundary_min.tz_convert('Asia/Kolkata')

            vwap_key = (inst_key, boundary_min)
            if vwap_key in self._bt_vwap_cache: return self._bt_vwap_cache[vwap_key]

        # 1. Try stored ATP history (Live/Backtest cache)
        # In Backtest, we prioritize the ATP value provided for the SPECIFIC instrument key
        # ATP (Average Traded Price) represents the true exchange-reported VWAP.
        atp_hist = getattr(self.state_manager, 'atp_history', {}).get(inst_key, {})
        if atp_hist:
            current_minute = pd.Timestamp(timestamp).replace(second=0, microsecond=0)
            if current_minute.tzinfo is None:
                current_minute = current_minute.tz_localize('Asia/Kolkata')
            else:
                current_minute = current_minute.tz_convert('Asia/Kolkata')

            # Exact match for finalized/historical minute
            if current_minute in atp_hist:
                return float(atp_hist[current_minute])

            if not strict_history or self.orchestrator.is_backtest:
                # Nearest past value match
                candidates = {ts: v for ts, v in atp_hist.items()
                              if isinstance(ts, pd.Timestamp) and ts <= current_minute}
                if candidates:
                    return float(atp_hist[max(candidates.keys())])

        if strict_history and not self.orchestrator.is_backtest:
            return None

        # 2. Backtest fallback: If strict_history (from ATP file) failed, allow falling back to OHLC
        # to ensure indicators like V-Slope don't stay at 0.0 or WaitData.
        if self.orchestrator.is_backtest and strict_history:
             # We proceed to the OHLC calculation below
             pass
        elif not self.orchestrator.is_backtest:
            atps = getattr(self.state_manager, 'option_atps', {})
            live_atp = atps.get(inst_key)
            if live_atp and live_atp > 0:
                return float(live_atp)

        current_day = timestamp.date()
        state_key = (inst_key, current_day)
        last_final_minute = timestamp.replace(second=0, microsecond=0)

        # Live aggregator fallback: use accumulated OHLC bars before hitting broker API.
        # Prevents VWAP returning None for near-expiry option strikes where the API
        # returns HTTP 400 and they get permanently blacklisted in _api_failure_cache.
        if not self.orchestrator.is_backtest:
            agg = getattr(self.orchestrator, 'entry_aggregator', None)
            if agg is not None:
                agg_ohlc = agg.get_historical_ohlc(inst_key)
                if agg_ohlc is not None and not agg_ohlc.empty:
                    agg_day = agg_ohlc[agg_ohlc.index.date == current_day].copy()
                    agg_day = agg_day[agg_day.index <= last_final_minute]
                    if not agg_day.empty:
                        if 'volume' not in agg_day.columns or agg_day['volume'].sum() == 0:
                            agg_day['volume'] = 1.0
                        vwap_val = VWAPIndicator.get_latest_value(agg_day)
                        if vwap_val is not None:
                            self._vwap_state[state_key] = {
                                'vwap': vwap_val,
                                'last_final_minute': last_final_minute
                            }
                            return vwap_val

        state = self._vwap_state.get(state_key)
        if not state or state['last_final_minute'] < last_final_minute:
            # include_current=True ensures we get the candle at 'timestamp' minute if it exists
            ohlc_1m = await self.data_manager.get_historical_ohlc(inst_key, 1, current_timestamp=timestamp, min_candles=20, for_full_day=True, include_current=True)
            if ohlc_1m is not None and not ohlc_1m.empty:
                df = ohlc_1m[(ohlc_1m.index.date == current_day) & (ohlc_1m.index <= last_final_minute)].copy()

                # Backtest fallback: ensure VWAP always calculates even if volume is missing or 0
                if self.orchestrator.is_backtest:
                    if 'volume' not in df.columns:
                        df['volume'] = 1.0
                    elif df['volume'].sum() == 0:
                        df['volume'] = 1.0

                if not df.empty:
                    # Modularized calculation (Rust Optimized if available)
                    vwap_val = RustBridge.calculate_vwap(df)
                    if vwap_val:
                        # For state persistence, we still store sums, but calculation is modular
                        tp = (df['high'] + df['low'] + df['close']) / 3
                        vol = df['volume']
                        state = {
                            'cum_pv': (tp * vol).sum(),
                            'cum_vol': vol.sum(),
                            'last_final_minute': last_final_minute
                        }
                        self._vwap_state[state_key] = state

        if state:
            val = state['cum_pv'] / state['cum_vol'] if state['cum_vol'] > 0 else None
            if self.orchestrator.is_backtest and val is not None:
                self._bt_vwap_cache[vwap_key] = val
                if len(self._bt_vwap_cache) > 50: self._bt_vwap_cache.pop(next(iter(self._bt_vwap_cache)))
            return val
        return None

    def _get_market_open_time(self, timestamp):
        """Delegates to BaseOrchestrator for unified market open time resolution."""
        return self.orchestrator.get_market_open_time(timestamp)

    async def get_vwap_slope_status(self, inst_key, timestamp, timeframe_minutes, count=1, live_vwap=None):
        if not inst_key:
            return None, None, None, None, 0, 0
        cache_key = (inst_key, timeframe_minutes, count, live_vwap, timestamp.date(), timestamp.hour, timestamp.minute)
        cached = self._vwap_slope_cache.get(cache_key)
        if cached and (timestamp - cached['ts']).total_seconds() < 5.0:
            return cached['val']

        if live_vwap is not None:
            atp_hist = getattr(self.state_manager, 'atp_history', {}).get(inst_key, {})
            if atp_hist:
                current_interval_start = timestamp.replace(
                    minute=(timestamp.minute // timeframe_minutes) * timeframe_minutes,
                    second=0, microsecond=0)
                prev_boundary = current_interval_start - pd.Timedelta(minutes=timeframe_minutes)
                candidates = {ts: v for ts, v in atp_hist.items() if isinstance(ts, type(prev_boundary)) and ts <= prev_boundary}
                if candidates:
                    v0 = candidates[max(candidates.keys())]
                    v1 = live_vwap
                    is_rising = v1 > v0
                    is_falling = v1 < v0
                    cons_r = 1 if is_rising else 0
                    cons_f = 1 if is_falling else 0
                    res = (is_rising, is_falling, v1, v0, cons_r, cons_f)
                    self._vwap_slope_cache[cache_key] = {'ts': timestamp, 'val': res}
                    return res

        ohlc_1m = await self.get_robust_ohlc(inst_key, 1, timestamp)
        if ohlc_1m is None or ohlc_1m.empty:
            return None, None, None, None, 0, 0

        current_interval_start = timestamp.replace(minute=(timestamp.minute // timeframe_minutes) * timeframe_minutes, second=0, microsecond=0)
        t0 = current_interval_start - pd.Timedelta(minutes=timeframe_minutes)
        df = ohlc_1m[ohlc_1m.index.date == timestamp.date()].copy()

        if live_vwap is None and (len(df) < 2 or (ohlc_1m.index.empty or ohlc_1m.index[-1] < timestamp.replace(second=0, microsecond=0))):
            return None, None, None, None, 0, 0
        if live_vwap is not None and df.empty:
            return None, None, None, None, 0, 0

        if not df.empty:
            df = df.sort_index()
            anchor_ts = self._get_market_open_time(timestamp)
            boundary_ts = timestamp.replace(second=0, microsecond=0)
            if boundary_ts > anchor_ts:
                expected_range = pd.date_range(start=anchor_ts, end=boundary_ts, freq='1min')
                df_vol = df['volume'] if 'volume' in df.columns else pd.Series(0, index=df.index)
                df = df.reindex(expected_range).ffill()
                df['volume'] = df_vol.reindex(expected_range).fillna(0)

        if not df.empty and 'volume' not in df.columns:
            df['volume'] = 0.0

        def get_vwap_at(ts):
            d = df[df.index <= ts]
            if d.empty: return None
            tp = (d['high'] + d['low'] + d['close']) / 3
            vol = d.get('volume', pd.Series(0, index=d.index))
            return (tp * vol).sum() / vol.sum() if vol.sum() > 0 else tp.mean()

        finalized_vwaps = []
        for i in range(count):
            boundary_ts = t0 - pd.Timedelta(minutes=i * timeframe_minutes)
            val = get_vwap_at(boundary_ts)
            if val is None: break
            finalized_vwaps.append((boundary_ts, val))

        if not finalized_vwaps:
            return False, False, live_vwap, None, 0, 0

        if live_vwap is None:
            live_vwap = get_vwap_at(timestamp)

        last_final_val = finalized_vwaps[0][1]
        is_rising_now = (live_vwap > last_final_val) if live_vwap is not None and last_final_val is not None else False
        is_falling_now = (live_vwap < last_final_val) if live_vwap is not None and last_final_val is not None else False

        cons_rising = 0
        for i in range(len(finalized_vwaps) - 1):
            if finalized_vwaps[i][1] > finalized_vwaps[i+1][1]: cons_rising += 1
            else: break
        cons_falling = 0
        for i in range(len(finalized_vwaps) - 1):
            if finalized_vwaps[i][1] < finalized_vwaps[i+1][1]: cons_falling += 1
            else: break

        res = (is_rising_now and (1 + cons_rising) >= count, is_falling_now and (1 + cons_falling) >= count, live_vwap, last_final_val, (1 + cons_rising) if is_rising_now else 0, (1 + cons_falling) if is_falling_now else 0)

        # Performance: Pre-check logging flag
        if logger.isEnabledFor(10): # DEBUG
            log_msg = f"[IndicatorManager] VWAP Slope for {inst_key} at {timeframe_minutes}m: Rising={res[0]}, Falling={res[1]} (LTP_VWAP={live_vwap:.2f}, Prev={last_final_val:.2f})"
        logger.debug(log_msg)

        self._vwap_slope_cache[cache_key] = {'ts': timestamp, 'val': res}
        return res

    async def get_vwap_slope_pair(self, key1, key2, timestamp, tf_minutes):
        """
        Compute two consecutive VWAP slopes for the COMBINED series (key1 + key2)
        using strictly stored high-fidelity ATP history at timeframe boundaries.

        Anchors (example: timestamp=09:25:00, tf=5):
          t_curr  = 09:25:00 (Current Boundary)
          t_prev  = 09:20:00 (Previous Boundary)
          t_prev2 = Previous Previous Boundary
        """
        if not key1 or not key2:
            return None, None, None, None, None

        # PERFORMANCE: Minute-Pulse Cache for Backtests/High-Freq Ticks
        # Avoid redundant heavy VWAP calculations if the boundary minute haven't changed.
        pulse_min = timestamp.replace(second=0, microsecond=0)
        if hasattr(timestamp, 'tzinfo') and timestamp.tzinfo:
            pulse_min = pd.Timestamp(pulse_min).tz_convert('Asia/Kolkata')
        else:
            pulse_min = pd.Timestamp(pulse_min).tz_localize('Asia/Kolkata')

        cache_key = (key1, key2, tf_minutes, pulse_min)
        if not hasattr(self, '_slope_pulse_cache'): self._slope_pulse_cache = {}
        if cache_key in self._slope_pulse_cache:
            return self._slope_pulse_cache[cache_key]

        # Align with finalized boundary minutes
        t_end = pd.to_datetime(timestamp).replace(second=0, microsecond=0)
        if t_end.tzinfo is None: t_end = t_end.tz_localize('Asia/Kolkata')

        t_curr = t_end
        t_prev = t_curr - pd.Timedelta(minutes=tf_minutes)
        t_prev2 = t_prev - pd.Timedelta(minutes=tf_minutes)

        async def get_combined_vwap_at(ts, label):
            # OPTIMIZED: Parallel VWAP fetch for CE and PE legs
            res = await asyncio.gather(
                self.calculate_vwap(key1, ts, strict_history=False),
                self.calculate_vwap(key2, ts, strict_history=False)
            )
            v1, v2 = res[0], res[1]

            if (v1 is None or v2 is None) and self.orchestrator.is_backtest:
                # Backtest fallback: If option data is missing, use index price as proxy
                idx_v = await self.calculate_vwap(self.orchestrator.index_instrument_key, ts, strict_history=False)
                if idx_v:
                    # Return half of index as proxy for each side if we can't find option data
                    v1 = v1 or (idx_v / 2)
                    v2 = v2 or (idx_v / 2)

            if v1 is None or v2 is None:
                # Log only as debug to avoid noise during startup wait
                logger.debug(f"[V-Slope] Missing anchor {label} at {ts.time()} for {key1}/{key2}")
                return None
            return v1 + v2

        # OPTIMIZED: Parallel fetch for all three time anchors (Current, Prev, Prev2)
        results = await asyncio.gather(
            get_combined_vwap_at(t_curr, "T-0"),
            get_combined_vwap_at(t_prev, "T-1"),
            get_combined_vwap_at(t_prev2, "T-2")
        )
        v_curr, v_prev, v_prev2 = results[0], results[1], results[2]

        if v_curr is None or v_prev is None:
            return None, None, None, None, None

        # V3 Strategy utilizes raw point difference for V-Slope comparisons
        curr_slope = v_curr - v_prev
        prev_slope = (v_prev - v_prev2) if v_prev2 is not None else None

        # Robust logging to avoid TypeError if values are None
        curr_str = f"{curr_slope:.4f}" if curr_slope is not None else "None"
        prev_str = f"{prev_slope:.4f}" if prev_slope is not None else "None"
        v_curr_str = f"{v_curr:.2f}" if v_curr is not None else "None"

        log_msg = f"[IndicatorManager] V-Slope Calculated for {key1}/{key2} at {tf_minutes}m: Curr={curr_str}, Prev={prev_str} (V_curr={v_curr_str})"
        logger.debug(log_msg)

        res = (curr_slope, prev_slope, v_curr, v_prev, v_prev2)
        # Store in pulse cache
        self._slope_pulse_cache[cache_key] = res
        # Performance: Housekeeping (keep last 10 entries)
        if len(self._slope_pulse_cache) > 20:
             self._slope_pulse_cache.pop(next(iter(self._slope_pulse_cache)))

        return res

    async def get_index_open_range(self, index_key, timestamp):
        """Fetches and caches the market open candle high/low."""
        is_mcx = any(x in self.orchestrator.instrument_name.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])
        real_key = self.orchestrator.futures_instrument_key if is_mcx else index_key

        current_date = timestamp.date()
        cache_key = (real_key, current_date)
        if cache_key in self._index_915_range:
            return self._index_915_range[cache_key]

        # Fetch 1m data for the index/futures
        ohlc = await self.data_manager.get_historical_ohlc(real_key, 1, current_timestamp=timestamp, min_candles=20, for_full_day=True)
        if ohlc is not None and not ohlc.empty:
            day_data = ohlc[ohlc.index.date == current_date]

            # Use dynamic open time anchor
            open_ts = self._get_market_open_time(timestamp)
            target_time = open_ts.time()

            anchor = day_data[day_data.index.time == target_time]
            if not anchor.empty:
                res = (float(anchor.iloc[0]['high']), float(anchor.iloc[0]['low']))
                self._index_915_range[cache_key] = res
                logger.debug(f"V2: Market Open Range Captured for {real_key}: High={res[0]:.2f}, Low={res[1]:.2f}")
                return res

        return None, None

    async def get_sr_status(self, inst_key, timeframe_minutes, timestamp):
        cache_key = (inst_key, timeframe_minutes, timestamp.date(), timestamp.hour, timestamp.minute)
        if cache_key in self._sr_cache:
            return self._sr_cache[cache_key]

        ohlc_1m = await self.get_robust_ohlc(inst_key, 1, timestamp)
        res = await SupportResistanceCalculator.get_sr_status_shared(
            self.data_manager, inst_key, timeframe_minutes, timestamp, ohlc_1m
        )
        self._sr_cache[cache_key] = res
        return res

    async def get_nuanced_barrier(self, inst_key, indicator_type, tf, timestamp):
        s1_v, r1_v, s1_est, r1_est, phase, s1_bh, r1_bl, b_lvl = await self.get_sr_status(inst_key, tf, timestamp)
        is_r1 = (indicator_type == 'r1_high')
        is_tracking = (phase == 'R1_TRACKING' if is_r1 else phase == 'S1_TRACKING')

        if is_tracking:
            current_min_ts = timestamp.replace(second=0, microsecond=0)
            anchor_ts = self._get_market_open_time(current_min_ts)
            if current_min_ts >= anchor_ts:
                mins_since_anchor = int((current_min_ts - anchor_ts).total_seconds() / 60)
                last_end_ts = anchor_ts + pd.Timedelta(minutes=(mins_since_anchor // tf) * tf)
                hist = await self.data_manager.get_historical_ohlc(inst_key, tf, last_end_ts + pd.Timedelta(seconds=1), for_full_day=True)
                if hist is not None and not hist.empty:
                    relevant = hist[hist.index <= last_end_ts]
                    if not relevant.empty:
                        prev_candle = relevant.iloc[-1]
                        val = prev_candle['high'] if is_r1 else prev_candle['low']
                        return float(val), ('PrevHigh' if is_r1 else 'PrevLow'), s1_v, r1_v, phase, s1_bh, r1_bl, b_lvl

        val = r1_v if is_r1 else s1_v
        return float(val) if val is not None else None, ('R1' if is_r1 else 'S1'), s1_v, r1_v, phase, s1_bh, r1_bl, b_lvl

    async def get_monotonic_barrier(self, strike, inst_key, tf, indicator_type, timestamp, direction, tracker):
        """
        Retrieves a nuanced barrier and ensures it moves monotonically.
        Returns (mono_val, b_val, label, s1_v, r1_v, phase, s1_bh, r1_bl, b_lvl, prev_val)
        """
        res = await self.get_nuanced_barrier(inst_key, indicator_type, tf, timestamp)
        b_val, label, s1_v, r1_v, phase, s1_bh, r1_bl, b_lvl = res

        if b_val is None:
            return None, None, label, s1_v, r1_v, phase, s1_bh, r1_bl, b_lvl, None

        is_r1 = (indicator_type == 'r1_high')
        if phase == ('R1_TRACKING' if is_r1 else 'S1_TRACKING'):
            return b_val, b_val, label, s1_v, r1_v, phase, s1_bh, r1_bl, b_lvl, b_val

        inst_side = 'CE' if direction == 'CALL' else 'PE'
        strike_key = f"{float(strike):.1f}_{inst_side}_{tf}m_{indicator_type}"

        if indicator_type == 's1_low':
            prev_val = tracker.get(strike_key, 0.0)
            mono_val = max(b_val, prev_val)
            tracker[strike_key] = mono_val
        else:
            prev_val = tracker.get(strike_key, 999999.0)
            mono_val = min(b_val, prev_val)
            tracker[strike_key] = mono_val

        return mono_val, b_val, label, s1_v, r1_v, phase, s1_bh, r1_bl, b_lvl, prev_val

    async def get_r1_profit_status(self, inst_key, timeframe_minutes, timestamp):
        """
        Retrieves R1 status for profit taking for a given timeframe.
        Returns (r1_val, is_established, candle_low).
        """
        tf = timeframe_minutes
        current_boundary = timestamp.replace(second=0, microsecond=0)
        if current_boundary.tzinfo is None:
            current_boundary = pd.Timestamp(current_boundary).tz_localize('Asia/Kolkata')

        if not hasattr(self, '_r1_profit_cache'): self._r1_profit_cache = {}
        cache_key = (inst_key, tf)
        cached = self._r1_profit_cache.get(cache_key)
        if cached and cached['ts'] == current_boundary:
            return cached['val']

        ohlc = await self.get_robust_ohlc(inst_key, tf, timestamp)
        if ohlc is None or ohlc.empty:
            return None, False, None

        today = timestamp.date()
        finalized_ohlc = ohlc[(ohlc.index < current_boundary) & (ohlc.index.date == today)]

        if finalized_ohlc.empty:
            return None, False, None

        calc = SupportResistanceCalculator(None, None)
        for ts, row in finalized_ohlc.iterrows():
            candle_data = {'timestamp': ts, 'open': row['open'], 'high': row['high'], 'low': row['low'], 'close': row['close']}
            calc.process_straddle_candle(inst_key, candle_data)

        state = calc.get_calculated_sr_state(inst_key)
        sr_levels = state.get('sr_levels', {})
        r1 = sr_levels.get('R1')
        r1_val = float(r1['high']) if r1 else None
        is_established = r1.get('is_established', False) if r1 else False
        r1_low = float(r1['low']) if r1 else None

        res = (r1_val, is_established, r1_low)
        self._r1_profit_cache[cache_key] = {'ts': current_boundary, 'val': res}
        return res

    async def calculate_atr(self, inst_key, timeframe_minutes, length, timestamp, current_ltp=None):
        """Calculates the Average True Range (ATR) for an instrument, optionally including current LTP."""
        # Include current candle to use live LTP if requested
        ohlc = await self.get_robust_ohlc(inst_key, timeframe_minutes, timestamp, include_current=True, buffer_count=length+1)
        if ohlc is None or len(ohlc) < length + 1:
            ohlc = await self.data_manager.get_historical_ohlc(inst_key, timeframe_minutes, current_timestamp=timestamp, min_candles=length+1, for_full_day=True, include_current=True)
            if ohlc is None or len(ohlc) < length + 1:
                return None

        df = ohlc.sort_index().copy()

        # If current_ltp is provided, inject it into the last candle to match user requirement
        if current_ltp is not None and not df.empty:
            last_idx = df.index[-1]
            df.at[last_idx, 'close'] = float(current_ltp)
            df.at[last_idx, 'high'] = max(df.at[last_idx, 'high'], float(current_ltp))
            df.at[last_idx, 'low'] = min(df.at[last_idx, 'low'], float(current_ltp))

        df['prev_close'] = df['close'].shift(1)

        # True Range calculation
        df['tr'] = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['prev_close']).abs(),
            (df['low'] - df['prev_close']).abs()
        ], axis=1).max(axis=1)

        # ATR as a simple moving average of True Range
        atr_series = df['tr'].rolling(window=length).mean()
        atr = atr_series.iloc[-1]

        if pd.notna(atr):
            log_msg = f"[IndicatorManager] ATR({length}) for {inst_key} at {timeframe_minutes}m: {atr:.2f}"
            logger.debug(log_msg)

        return float(atr) if pd.notna(atr) else None

    async def calculate_combined_rsi(self, key1, key2, timestamp, tf=5, period=14, skip_api=False):
        """
        Calculates RSI on the combined price series of two instruments.
        Wilder's Smoothing (EMA) is used for standard RSI behavior.
        buffer_count is optimized to fetch exactly the required history.

        Step 8: Use historical data (Rest API) for start and then OHLC aggregator.
        """
        # PERFORMANCE: Backtest Indicator Cache
        if self.orchestrator.is_backtest:
            if not hasattr(self, '_bt_rsi_cache'): self._bt_rsi_cache = {}
            # Boundary-based cache key
            boundary = pd.Timestamp(timestamp).replace(second=0, microsecond=0)
            if boundary.tzinfo is None:
                boundary = boundary.tz_localize('Asia/Kolkata')
            else:
                boundary = boundary.tz_convert('Asia/Kolkata')

            c_key = (key1, key2, tf, period, boundary)
            if c_key in self._bt_rsi_cache: return self._bt_rsi_cache[c_key]

        # Strict Priming: Fetch exactly the required candles for the given timeframe.
        # For 1m TF, buffer = 15. For 5m TF, buffer = 15 (which requests 15 x 5m candles).
        buffer = period + 1

        # 1. Try dynamic calculation on Option Premiums
        # OPTIMIZED: Parallel OHLC fetch for CE and PE legs
        res = await asyncio.gather(
            self.get_robust_ohlc(key1, tf, timestamp, include_current=True, skip_api=skip_api, buffer_count=buffer),
            self.get_robust_ohlc(key2, tf, timestamp, include_current=True, skip_api=skip_api, buffer_count=buffer)
        )
        ohlc1, ohlc2 = res[0], res[1]

        if ohlc1 is not None and ohlc2 is not None and not ohlc1.empty and not ohlc2.empty:
            # Robust alignment for illiquid legs: union indices and ffill
            combined_index = ohlc1.index.union(ohlc2.index).sort_values()
            s1 = ohlc1['close'].reindex(combined_index).ffill()
            s2 = ohlc2['close'].reindex(combined_index).ffill()
            combined_series = (s1 + s2).dropna()

            if len(combined_series) >= period + 1:
                # Rust Optimized Calculation
                val = RustBridge.calculate_rsi(combined_series, period)
                if val is not None:
                    logger.debug(f"[IndicatorManager] RSI({period}) for {key1}/{key2} calculated using {len(combined_series)} historical premium candles.")
                    if self.orchestrator.is_backtest:
                         self._bt_rsi_cache[c_key] = val
                         if len(self._bt_rsi_cache) > 50: self._bt_rsi_cache.pop(next(iter(self._bt_rsi_cache)))
                    return val

            logger.warning(f"[IndicatorManager] RSI insufficient premium data: Got {len(combined_series)}/{period+1} candles for {key1}/{key2}.")

        # 2. Index Proxy fallback removed as per user request.
        # Bot must rely solely on Strike history (hunting back up to 10 days).

        # 3. Last Resort: Backtest History from CSV (ATP file)
        if self.orchestrator.is_backtest:
            # We prefer calculating dynamically from Premium OHLC if it was correctly gap-filled.
            # However, if Premium data is still missing (e.g. at the very start of day), we use the CSV pre-calculated value.
            target_key = key1
            rsi_hist = getattr(self.state_manager, 'rsi_history', {}).get(target_key, {})
            if not rsi_hist:
                 target_key = self.orchestrator.index_instrument_key
                 rsi_hist = getattr(self.state_manager, 'rsi_history', {}).get(target_key, {})

            if rsi_hist:
                current_minute = pd.Timestamp(timestamp).replace(second=0, microsecond=0)
                if current_minute.tzinfo is None:
                    current_minute = current_minute.tz_localize('Asia/Kolkata')
                else:
                    current_minute = current_minute.tz_convert('Asia/Kolkata')

                # Check exact match or nearest past value
                if current_minute in rsi_hist:
                    return float(rsi_hist[current_minute])

                past_vals = {ts: v for ts, v in rsi_hist.items() if ts <= current_minute}
                if past_vals:
                    return float(rsi_hist[max(past_vals.keys())])

        return None

    async def calculate_combined_roc(self, key1, key2, timestamp, tf=1, length=9, include_current=True):
        """
        Calculates the Rate of Change (ROC) on the combined price series of two instruments.
        Uses the ROCIndicator class from the modular environment.

        Step 9: Use historical data (Rest API) for start and then OHLC aggregator.
        """
        # PERFORMANCE: Backtest Indicator Cache
        if self.orchestrator.is_backtest:
            if not hasattr(self, '_bt_roc_cache'): self._bt_roc_cache = {}
            boundary = pd.Timestamp(timestamp).replace(second=0, microsecond=0)
            if boundary.tzinfo is None:
                boundary = boundary.tz_localize('Asia/Kolkata')
            else:
                boundary = boundary.tz_convert('Asia/Kolkata')

            c_key = (key1, key2, tf, length, include_current, boundary)
            if c_key in self._bt_roc_cache: return self._bt_roc_cache[c_key]

        # Strict Priming: Fetch exactly the required candles for the given timeframe.
        buffer = length + 1

        # 1. Try dynamic calculation on Option Premiums
        # OPTIMIZED: Parallel OHLC fetch for CE and PE legs
        res = await asyncio.gather(
            self.get_robust_ohlc(key1, tf, timestamp, include_current=include_current, buffer_count=buffer),
            self.get_robust_ohlc(key2, tf, timestamp, include_current=include_current, buffer_count=buffer)
        )
        ohlc1, ohlc2 = res[0], res[1]

        if ohlc1 is not None and ohlc2 is not None and not ohlc1.empty and not ohlc2.empty:
            # Robust alignment for illiquid legs: union indices and ffill
            combined_index = ohlc1.index.union(ohlc2.index).sort_values()
            s1 = ohlc1['close'].reindex(combined_index).ffill()
            s2 = ohlc2['close'].reindex(combined_index).ffill()
            combined_series = (s1 + s2).dropna()

            if len(combined_series) >= length + 1:
                # Rust Optimized Calculation
                val = RustBridge.calculate_roc(combined_series, length)
                if val is not None:
                    logger.debug(f"[IndicatorManager] ROC({length}) for {key1}/{key2} calculated using {len(combined_series)} historical premium candles.")
                    if self.orchestrator.is_backtest:
                         self._bt_roc_cache[c_key] = val
                         if len(self._bt_roc_cache) > 50: self._bt_roc_cache.pop(next(iter(self._bt_roc_cache)))
                    return val

            logger.warning(f"[IndicatorManager] ROC insufficient premium data: Got {len(combined_series)}/{length+1} candles for {key1}/{key2}.")

        # 2. Index Proxy fallback removed as per user request.

        # 3. Last Resort: Backtest History from CSV (ATP file)
        if self.orchestrator.is_backtest:
            target_key = key1
            roc_hist = getattr(self.state_manager, 'roc_history', {}).get(target_key, {})
            if not roc_hist:
                 target_key = self.orchestrator.index_instrument_key
                 roc_hist = getattr(self.state_manager, 'roc_history', {}).get(target_key, {})

            if roc_hist:
                current_minute = pd.Timestamp(timestamp).replace(second=0, microsecond=0)
                if current_minute.tzinfo is None:
                    current_minute = current_minute.tz_localize('Asia/Kolkata')
                else:
                    current_minute = current_minute.tz_convert('Asia/Kolkata')

                # Check exact match or nearest past value
                if current_minute in roc_hist:
                    return float(roc_hist[current_minute])

                past_vals = {ts: v for ts, v in roc_hist.items() if ts <= current_minute}
                if past_vals:
                    return float(roc_hist[max(past_vals.keys())])

        return None
