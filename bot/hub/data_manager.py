import os
from pathlib import Path
from utils.logger import logger
from hub.event_bus import event_bus
import datetime
from upstox_client.rest import ApiException
import asyncio
import pandas as pd
from hub.contract_manager import ContractManager, OptionContract
from hub.futures_manager import FuturesManager

class DataManager:
    def __init__(self, rest_client, instrument_key, config_manager, atm_manager=None, is_backtest=False):
        self.rest_client = rest_client
        self.instrument_key = instrument_key
        self.config_manager = config_manager
        self.atm_manager = atm_manager
        self.is_backtest = is_backtest

        self.contract_manager = ContractManager(rest_client, config_manager, atm_manager)
        self.futures_manager = FuturesManager(rest_client, config_manager, atm_manager)

        self.market_data = {}
        self.daily_ohlc_cache = {}
        self.backtest_ohlc_data = {}
        self.api_ohlc_cache = {}
        self.live_ohlc_cache = {}
        self._ohlc_lock = asyncio.Lock()

    @property
    def all_options(self): return self.contract_manager.all_options
    @all_options.setter
    def all_options(self, val): self.contract_manager.all_options = val

    @property
    def near_expiry_date(self): return self.contract_manager.near_expiry_date
    @near_expiry_date.setter
    def near_expiry_date(self, val): self.contract_manager.near_expiry_date = val

    @property
    def monthly_expiries(self): return self.contract_manager.monthly_expiries

    @property
    def backtest_df(self):
        # We need to access backtest_df for some historical queries
        # For simplicity, I'll keep it here and let ContractManager set it if needed,
        # or just load it here since it's used for historical data too.
        if not hasattr(self, '_backtest_df'):
            self._backtest_df = None
        return self._backtest_df
    @backtest_df.setter
    def backtest_df(self, val): self._backtest_df = val

    async def load_contracts(self):
        success, df = await self.contract_manager.load_contracts(self.instrument_key, self.discover_futures_key)
        if df is not None:
            self.backtest_df = df
        return success

    async def discover_futures_key(self):
        await self.futures_manager.discover_futures_key(self.instrument_key, self._update_orch_futures_key)

    def _update_orch_futures_key(self, new_key):
        if self.atm_manager and self.atm_manager.orchestrator:
            orch = self.atm_manager.orchestrator
            orch.futures_instrument_key = new_key
            self.atm_manager.spot_instrument_key = new_key
            if hasattr(orch, 'price_feed_handler'):
                orch.price_feed_handler.futures_instrument_key = new_key

    def get_trading_instruments(self):
        return self.all_options, self.near_expiry_date

    def _is_mcx(self):
        """Helper to identify MCX instruments for timing and pricing logic."""
        key = str(self.instrument_key).upper()
        # Check both the key (e.g. MCX_INDEX|CRUDE OIL) and the instrument name
        return "MCX" in key or any(x in key for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])

    async def get_historical_index_price_at_timestamp(self, timestamp: datetime.datetime) -> float:
        try:
            if self.backtest_df is not None and not self.backtest_df.empty:
                relevant = self.backtest_df[self.backtest_df.index < timestamp]
                if not relevant.empty:
                    for col in ['spot_price', 'atm', 'index_price']:
                        if col in relevant.columns: return float(relevant.iloc[-1][col])

            df = await self._fetch_and_prepare_api_data(self.instrument_key, timestamp.date(), timestamp.date(), "1minute")
            if df.empty: return None

            if timestamp.tzinfo is None:
                import pytz
                timestamp = pytz.timezone('Asia/Kolkata').localize(timestamp)

            is_backtest = self.config_manager.get_boolean('settings', 'backtest_enabled', fallback=False)
            relevant_data = df[df.index < timestamp] if is_backtest else df[df.index <= timestamp]
            return relevant_data.iloc[-1]['close'] if not relevant_data.empty else None
        except Exception as e:
            logger.error(f"Error fetching historical index price: {e}")
            return None

    async def _fetch_and_prepare_api_data(self, instrument_key, from_date, to_date, interval="1minute"):
        if not hasattr(self, '_api_failure_cache'): self._api_failure_cache = set()
        if instrument_key in self._api_failure_cache:
            return pd.DataFrame()
        try:
            # For Crude Oil, try to use the futures key if the index symbol fails
            if 'CRUDE' in instrument_key.upper() and self.atm_manager and self.atm_manager.orchestrator:
                f_key = self.atm_manager.orchestrator.futures_instrument_key
                if f_key and f_key != instrument_key:
                    # Try futures key first for commodities as they often don't have historical index data on Upstox
                    df = await self.rest_client.get_historical_candle_data(f_key, interval, to_date, from_date)
                    if not df.empty: return df

            df = await self.rest_client.get_historical_candle_data(instrument_key, interval, to_date, from_date)
            if df is None or df.empty: return pd.DataFrame()

            if 'timestamp' in df.columns:
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                df.set_index('timestamp', inplace=True)

            if df.index.tz is None:
                df.index = df.index.tz_localize('Asia/Kolkata')
            else:
                df.index = df.index.tz_convert('Asia/Kolkata')
            return df
        except Exception as e:
            _e_str = str(e)
            if "401" in _e_str or "403" in _e_str:
                # Auth token expired — all future calls for any key will fail the same way.
                # Cache the failure so on_tick stops trying on every tick.
                if not hasattr(self, '_api_failure_cache'): self._api_failure_cache = set()
                self._api_failure_cache.add(instrument_key)
                logger.debug(
                    f"[DataManager] REST auth error ({_e_str[:60]}) for {instrument_key}. "
                    "Skipping future historical fetches for this key."
                )
            else:
                logger.error(f"DATA_FETCH: Error for '{instrument_key}': {e}")
            return pd.DataFrame()

    async def get_historical_ohlc(self, instrument_key: str, timeframe_minutes, current_timestamp: datetime.datetime = None, num_minutes_back: int = None, from_date: datetime.datetime = None, for_full_day: bool = False, include_current: bool = False, min_candles: int = None) -> pd.DataFrame:
        import re
        parsed_minutes = 1
        if isinstance(timeframe_minutes, int):
            parsed_minutes = timeframe_minutes
        elif isinstance(timeframe_minutes, str):
            match = re.search(r'\d+', timeframe_minutes)
            if match: parsed_minutes = int(match.group())

        if parsed_minutes > 1:
            # PERFORMANCE: Cache resampled dataframes for backtests
            if self.is_backtest:
                if not hasattr(self, '_bt_resample_cache'): self._bt_resample_cache = {}
                res_key = (instrument_key, parsed_minutes, current_timestamp.date())
                if res_key in self._bt_resample_cache:
                    df_full = self._bt_resample_cache[res_key]
                    ts_limit = pd.Timestamp(current_timestamp)
                    if df_full.index.tz is not None and ts_limit.tzinfo is None:
                        ts_limit = ts_limit.tz_localize('Asia/Kolkata')
                    return df_full[df_full.index <= ts_limit] if include_current else df_full[df_full.index < ts_limit]

            # OPTIMIZATION: When resampling in backtest, use unsliced base data to populate full day cache
            if self.is_backtest:
                # Internal call to get base data without slicing by current simulation time
                # We reuse the 1m logic but bypass the current_timestamp limit for resampling source
                one_minute_df = await self._get_unsliced_1m_data(instrument_key, current_timestamp)
            else:
                one_minute_df = await self.get_historical_ohlc(instrument_key, 1, current_timestamp, num_minutes_back, from_date, for_full_day, include_current, min_candles)

            if one_minute_df.empty: return pd.DataFrame()

            resampling_logic = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}
            if 'volume' in one_minute_df.columns: resampling_logic['volume'] = 'sum'

            if self.is_backtest:
                resampled_buckets = []
                is_mcx = self._is_mcx()

                # Get preferred market open time
                start_str = self.config_manager.get(self.instrument_key, 'start_time', '09:00:00' if is_mcx else '09:15:00')
                try:
                    open_time = datetime.datetime.strptime(start_str.replace('.', ':'), '%H:%M:%S').time()
                    market_close_time = datetime.time(23, 30) if is_mcx else datetime.time(15, 30)
                except:
                    open_time = datetime.time(9, 0) if is_mcx else datetime.time(9, 15)
                    market_close_time = datetime.time(23, 30) if is_mcx else datetime.time(15, 30)

                # Process day-by-day to maintain session-aligned resampling across historical gaps
                for date, day_data in one_minute_df.groupby(one_minute_df.index.date):
                    # 1. Fill gaps in 1m data for this day to ensure consistent candle lengths
                    day_start = datetime.datetime.combine(date, open_time)
                    day_end = datetime.datetime.combine(date, market_close_time)
                    if day_data.index.tz is not None:
                         # Use pd.Timestamp for robust localization across pytz and datetime.timezone
                         day_start = pd.Timestamp(day_start).tz_localize(day_data.index.tz)
                         day_end = pd.Timestamp(day_end).tz_localize(day_data.index.tz)

                         # Ensure current_timestamp is aligned
                         cur_ts = pd.Timestamp(current_timestamp)
                         if cur_ts.tzinfo is None:
                             cur_ts = cur_ts.tz_localize(day_data.index.tz)
                         else:
                             cur_ts = cur_ts.tz_convert(day_data.index.tz)
                         limit_ts = min(day_end, cur_ts)
                    else:
                        limit_ts = min(pd.Timestamp(day_end), pd.Timestamp(current_timestamp)) if current_timestamp else pd.Timestamp(day_end)

                    full_range = pd.date_range(start=day_start, end=limit_ts, freq='1min')
                    df_filled = day_data.reindex(full_range).ffill().dropna(subset=['close'])

                    if df_filled.empty: continue

                    # 2. Custom windowing to align with market open anchor
                    for start_idx in range(0, len(df_filled), parsed_minutes):
                        group = df_filled.iloc[start_idx : start_idx + parsed_minutes]

                        # We only take full buckets to ensure indicator stability
                        if len(group) == parsed_minutes:
                            bucket_start = group.index[0]
                            bucket_end = bucket_start + pd.Timedelta(minutes=parsed_minutes)

                            # Respect current timestamp limit in backtest
                            if bucket_end > current_timestamp and not include_current:
                                continue

                            bucket = {
                                'timestamp': bucket_start,
                                'open': group.iloc[0]['open'],
                                'high': group['high'].max(),
                                'low': group['low'].min(),
                                'close': group.iloc[-1]['close']
                            }
                            if 'volume' in group.columns:
                                bucket['volume'] = group['volume'].sum()
                            resampled_buckets.append(bucket)

                resampled_df = pd.DataFrame(resampled_buckets).set_index('timestamp') if resampled_buckets else pd.DataFrame()

                # Store in resample cache (full day)
                if not resampled_df.empty:
                    self._bt_resample_cache[res_key] = resampled_df
                    # Trim to current time for return
                    ts_limit = pd.Timestamp(current_timestamp)
                    if resampled_df.index.tz is not None and ts_limit.tzinfo is None:
                        ts_limit = ts_limit.tz_localize('Asia/Kolkata')
                    return resampled_df[resampled_df.index <= ts_limit] if include_current else resampled_df[resampled_df.index < ts_limit]
            else:
                resampled_df = one_minute_df.resample(f"{parsed_minutes}min").agg(resampling_logic).dropna()
            return resampled_df

        if not self.is_backtest:
            async with self._ohlc_lock:
                now = current_timestamp or datetime.datetime.now()
                to_date = now.date()

                # UNIFIED: Always use the day-by-day optimized fetcher for history
                df_cached = await self.fetch_and_cache_api_ohlc(instrument_key, to_date)
                if not df_cached.empty:
                    # Filter for now, but handle timezone awareness
                    ts_limit = pd.Timestamp(now)
                    if df_cached.index.tz is not None and ts_limit.tzinfo is None:
                        ts_limit = ts_limit.tz_localize('Asia/Kolkata')
                    elif df_cached.index.tz is None and ts_limit.tzinfo is not None:
                        ts_limit = ts_limit.replace(tzinfo=None)
                    return df_cached[df_cached.index <= ts_limit]

                return pd.DataFrame()

        # Backtest Logic
        if current_timestamp is None: return pd.DataFrame()
        backtest_date = current_timestamp.date()
        api_cache_key = (instrument_key, backtest_date, "1minute")

        # PERFORMANCE: Backtest Simulation Minute Cache
        if not hasattr(self, '_bt_sim_cache'): self._bt_sim_cache = {}
        # Clear cache if date changes
        if getattr(self, '_bt_last_date', None) != backtest_date:
            self._bt_sim_cache.clear()
            self._bt_last_date = backtest_date

        sim_key = (instrument_key, timeframe_minutes, current_timestamp.replace(second=0, microsecond=0), include_current)
        if sim_key in self._bt_sim_cache:
            return self._bt_sim_cache[sim_key]

        # Prioritize high-fidelity synthetic data injected by Orchestrator
        res_df = pd.DataFrame()
        if instrument_key in self.backtest_ohlc_data:
            df = self.backtest_ohlc_data[instrument_key]
            if not df.empty:
                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
                ts = pd.Timestamp(current_timestamp)
                idx_tz = getattr(df.index, 'tzinfo', getattr(df.index, 'tz', None))
                if idx_tz is not None and ts.tzinfo is None:
                    ts = ts.tz_localize('Asia/Kolkata')
                elif idx_tz is None and ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                res_df = df[df.index <= ts].copy() if include_current else df[df.index < ts].copy()

        # PROACTIVE LOCAL FALLBACK for Index/Spot instruments in Backtest
        if res_df.empty and ("INDEX" in instrument_key or instrument_key == self.instrument_key):
            res_df = await self._get_backtest_ohlc_csv_fallback(instrument_key, backtest_date, current_timestamp, include_current, for_full_day)

        # MANDATORY: If we requested historical lookback, ensure we fulfill it even by calling API
        # PERFORMANCE: For Options (NSE_FO), skip API hunt if CSV data already provides buffer_count
        is_option = "NSE_FO" in instrument_key
        if (from_date or num_minutes_back or for_full_day or min_candles):
            if is_option and min_candles and len(res_df) >= min_candles:
                 # Buffer satisfied by local high-fidelity data, skip API hunt
                 return res_df

            ts = pd.Timestamp(current_timestamp)
            if ts.tzinfo is None: ts = ts.tz_localize('Asia/Kolkata')

            earliest_needed = pd.to_datetime(from_date) if from_date else (ts - pd.Timedelta(minutes=num_minutes_back or 0))
            if earliest_needed.tzinfo is None: earliest_needed = earliest_needed.tz_localize('Asia/Kolkata')

            if res_df.empty or res_df.index[0] > earliest_needed or (min_candles and len(res_df) < min_candles):
                # Attempt to fetch history from API to warm up technicals
                hist_api = self.api_ohlc_cache.get(api_cache_key)
                if hist_api is None:
                    hist_api = await self.fetch_and_cache_api_ohlc(instrument_key, ts.date())

                if hist_api is not None and not hist_api.empty:
                    if not isinstance(hist_api.index, pd.DatetimeIndex):
                        hist_api.index = pd.to_datetime(hist_api.index)
                    if hist_api.index.tz is None: hist_api.index = hist_api.index.tz_localize('Asia/Kolkata')

                    if res_df.empty:
                        res_df = hist_api[hist_api.index <= ts].copy() if include_current else hist_api[hist_api.index < ts].copy()
                    else:
                        # Stitch: Take API data before CSV data starts
                        api_part = hist_api[hist_api.index < res_df.index[0]]
                        res_df = pd.concat([api_part, res_df]).sort_index()

                    # Trim to timestamp limit
                    res_df = res_df[res_df.index <= ts] if include_current else res_df[res_df.index < ts]

        if not res_df.empty:
            # Store in simulation cache before returning
            self._bt_sim_cache[sim_key] = res_df
            return res_df

        if api_cache_key in self.api_ohlc_cache:
            df = self.api_ohlc_cache[api_cache_key]
            if df is not None and not df.empty:
                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
                if df.index.tz is None:
                    df.index = df.index.tz_localize('Asia/Kolkata')
                ts = pd.Timestamp(current_timestamp)
                if ts.tzinfo is None:
                    ts = ts.tz_localize('Asia/Kolkata')
                return df[df.index <= ts].copy() if include_current else df[df.index < ts].copy()

        # CSV Resampling fallback... (rest of the complex logic simplified or preserved)
        # For brevity, I'll keep the core structure but it's now much smaller.
        return await self._get_backtest_ohlc_csv_fallback(instrument_key, backtest_date, current_timestamp, include_current, for_full_day)

    async def _get_backtest_ohlc_csv_fallback(self, instrument_key, backtest_date, current_timestamp, include_current, for_full_day):
        cache_key = (instrument_key, backtest_date, "1minute")
        if cache_key not in self.daily_ohlc_cache:
            if self.backtest_df is not None and not self.backtest_df.empty:
                df = self.backtest_df[self.backtest_df.index.date == backtest_date].copy()
                if not df.empty:
                    # 1. Check if it's the Index/Spot instrument itself
                    is_index = "INDEX" in instrument_key or instrument_key == self.instrument_key
                    if is_index:
                        # Try all common spot price columns in high-fidelity CSV
                        for col in ['spot_price', 'index_price', 'atm']:
                            if col in df.columns:
                                res = df.resample('1min')[col].ohlc().dropna()
                                if not res.empty:
                                    self.daily_ohlc_cache[cache_key] = res
                                    break

                    # 2. Check if it's an option contract
                    if cache_key not in self.daily_ohlc_cache:
                        contract = self.atm_manager.get_contract_by_instrument_key(instrument_key)
                        if contract:
                            side = contract.instrument_type.lower()
                            price_col = f'{side}_ltp'
                            sym_col = f'{side}_symbol'

                            # Filter by specific instrument key to avoid mixing data from different strikes
                            if sym_col in df.columns:
                                df = df[df[sym_col] == instrument_key]

                            if not df.empty and price_col in df.columns:
                                res = df.resample('1min')[price_col].ohlc().dropna()
                                self.daily_ohlc_cache[cache_key] = res

            if cache_key not in self.daily_ohlc_cache:
                df = await self.fetch_and_cache_api_ohlc(instrument_key, backtest_date)
                if not df.empty:
                    self.daily_ohlc_cache[cache_key] = df

        df = self.daily_ohlc_cache.get(cache_key, pd.DataFrame())
        if df.empty: return df
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize('Asia/Kolkata')
        ts = pd.Timestamp(current_timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize('Asia/Kolkata')
        return df[df.index <= ts].copy() if include_current else df[df.index < ts].copy()

    async def fetch_and_cache_api_ohlc(self, instrument_key: str, date: datetime.date, interval: str = "1minute", days_back: int = 10):
        """
        UNIVERSAL LOCAL HISTORY CACHE:
        Prioritizes local discovery to ensure 'Zero API' repeat backtests.
        Saves all successful API fetches as standardized broker-keyed CSVs.
        """
        if not hasattr(self, '_api_failure_cache'): self._api_failure_cache = set()
        if instrument_key in self._api_failure_cache: return pd.DataFrame()

        if not hasattr(self, 'local_history_cache'): self.local_history_cache = {}

        hist_dir = Path("data_history")
        hist_dir.mkdir(parents=True, exist_ok=True)
        external_hist = Path("Selling_Using_C")

        # Resolve broker name for standardized naming
        broker_name = 'upstox'
        if self.atm_manager and self.atm_manager.orchestrator:
             broker_name = getattr(self.atm_manager.orchestrator, 'broker_name', 'upstox').lower()

        local_frames = []
        for i in range(days_back + 1):
            check_date = date - datetime.timedelta(days=i)
            date_str = check_date.strftime('%Y-%m-%d')
            safe_key = instrument_key.replace('|', '_').replace(':', '_').replace(' ', '_')

            # Standard Naming: {broker}_{key}_{date}.csv
            cache_key = f"{broker_name}_{safe_key}_{date_str}"

            if cache_key in self.local_history_cache:
                local_frames.append(self.local_history_cache[cache_key])
                continue

            file_paths = [
                hist_dir / f"{cache_key}.csv",
                # Fallback to old format or external captures
                hist_dir / f"{safe_key}_{date_str}.csv",
                external_hist / f"atp_data_{self.instrument_key}_{date_str}.csv",
                Path(os.path.dirname(os.path.abspath(__file__))).parent / "backtest_data" / f"atp_data_{self.instrument_key}_{date_str}.csv",
                Path(os.getcwd()) / "backtest_data" / f"atp_data_{self.instrument_key}_{date_str}.csv",
                Path(os.getcwd()) / f"atp_data_{self.instrument_key}_{date_str}.csv"
            ]

            file_path = next((p for p in file_paths if p.exists()), None)

            if file_path:
                try:
                    # Offload blocking IO to thread
                    df_local = await asyncio.to_thread(pd.read_csv, file_path, low_memory=True)
                    if 'timestamp' in df_local.columns:
                        df_local['timestamp'] = pd.to_datetime(df_local['timestamp'])
                        df_local.set_index('timestamp', inplace=True)
                        if df_local.index.tz is None:
                            df_local.index = df_local.index.tz_localize('Asia/Kolkata')

                        # Store in RAM cache
                        self.local_history_cache[cache_key] = df_local
                        local_frames.append(df_local)
                except Exception as e:
                    logger.debug(f"Failed to read local history {file_path}: {e}")

        # Optimization: If it's an ATP file, we might need to filter by instrument_key
        # because those files are often combined session captures.
        processed_local_frames = []
        for df in local_frames:
            if 'instrument_key' in df.columns:
                sub = df[df['instrument_key'] == instrument_key]
                if not sub.empty:
                    # Rename columns to standard OHLC if it's an ATP file
                    if 'atp' in sub.columns and 'ltp' in sub.columns:
                         sub = sub.rename(columns={'atp': 'close', 'minute_ts': 'timestamp'})
                         sub['open'] = sub['high'] = sub['low'] = sub['close']
                    processed_local_frames.append(sub)
            else:
                processed_local_frames.append(df)

        if processed_local_frames:
            df_combined = pd.concat(processed_local_frames).sort_index()
            # If local data is sufficient (e.g. at least 15 candles), return it
            # (Note: min_candles check happens in get_historical_ohlc, but we cache this anyway)
            self.api_ohlc_cache[(instrument_key, date, interval)] = df_combined
            # If we have something, we still might need more, but let's see if it's enough for the caller
            if len(df_combined) >= 15:
                return df_combined

        real_key = instrument_key
        if self.config_manager.get_boolean('settings', 'backtest_enabled'):
            parts = instrument_key.split()
            if len(parts) >= 6:
                contract = await self.get_live_contract_details(int(parts[1]), datetime.datetime.strptime(f"{parts[3]} {parts[4]} {parts[5]}", "%d %b %Y").date(), parts[2])
                if contract: real_key = contract.instrument_key
                else: return pd.DataFrame()

        # Cache key for this specific instrument and backtest session date
        cache_key = (instrument_key, date, interval)
        cached_df = self.api_ohlc_cache.get(cache_key)

        # Determine if we actually need to hit the API or if cache is sufficient
        fetch_needed = True
        if cached_df is not None and not cached_df.empty:
            earliest_in_cache = cached_df.index[0].date()
            if earliest_in_cache <= (date - datetime.timedelta(days=days_back)):
                fetch_needed = False

        if fetch_needed:
            # Step 8 & 9: Optimized Descending hunt (Day-by-Day)
            # User requirement: Check one day before, if found, don't check further back.
            combined_df = pd.DataFrame()
            for i in range(1, days_back + 1):
                check_date = date - datetime.timedelta(days=i)
                try:
                    df = await self._fetch_and_prepare_api_data(real_key, check_date, check_date, interval)

                    # Empty 200 means a holiday or weekend — continue to an earlier day.
                    # Truly unlisted/expired options return HTTP 400, caught by the except
                    # block below, which adds them to _api_failure_cache immediately.
                    if df is None or df.empty:
                        continue

                    combined_df = pd.concat([df, combined_df]).sort_index()

                    # Standardize and save to local cache
                    hist_dir = Path("data_history")
                    safe_key = instrument_key.replace('|', '_').replace(':', '_').replace(' ', '_')
                    day_file = hist_dir / f"{broker_name}_{safe_key}_{check_date.strftime('%Y-%m-%d')}.csv"
                    if not day_file.exists():
                        # Save with standard headers: timestamp,open,high,low,close,volume
                        # Ensure the index is named 'timestamp' for consistency
                        df.index.name = 'timestamp'
                        df.to_csv(day_file)
                        logger.info(f"[DataManager] Saved technical history: {day_file.name}")

                    # Stop as soon as we find any data for a day
                    break
                except Exception as e:
                    _err = str(e)
                    if "400" in _err:
                        # Contract is unlisted or expired — no point retrying further dates
                        self._api_failure_cache.add(instrument_key)
                        return combined_df
                    if "401" in _err or "403" in _err:
                        # Auth token expired for this REST client — all dates will fail the same way.
                        # Add to failure cache so subsequent on_tick calls skip the fetch entirely.
                        self._api_failure_cache.add(instrument_key)
                        logger.debug(
                            f"[DataManager] Historical fetch for {instrument_key} blocked by auth error "
                            f"({_err[:60]}). Adding to failure cache to prevent retry loop."
                        )
                        return combined_df
                    logger.debug(f"API fetch failed for {instrument_key} on {check_date}: {e}")
                    # Other errors (network, 5xx): might be a transient/holiday — continue to next day

            if not combined_df.empty:
                self.api_ohlc_cache[cache_key] = combined_df
                return combined_df

        return cached_df if cached_df is not None else pd.DataFrame()

    def clear_api_ohlc_cache_for_strike(self, old_strike: int, expiry_date: datetime.date):
        keys_to_remove = []
        for k in self.api_ohlc_cache:
            contract = self.atm_manager.get_contract_by_instrument_key(k[0])
            if contract and contract.strike_price == old_strike:
                keys_to_remove.append(k)
        for k in keys_to_remove: del self.api_ohlc_cache[k]

    async def _get_unsliced_1m_data(self, instrument_key, current_timestamp):
        """Helper to get full unsliced 1m data for resampling cache."""
        # 1. Check local high-fidelity data
        if instrument_key in self.backtest_ohlc_data:
            return self.backtest_ohlc_data[instrument_key]

        # 2. Check API cache (full day)
        date = current_timestamp.date()
        cache_key = (instrument_key, date, "1minute")
        if cache_key in self.api_ohlc_cache:
            return self.api_ohlc_cache[cache_key]

        # 3. Trigger a fetch if missing
        await self.fetch_and_cache_api_ohlc(instrument_key, date)
        return self.api_ohlc_cache.get(cache_key, pd.DataFrame())

    def clear_caches(self):
        self.daily_ohlc_cache.clear()
        self.api_ohlc_cache.clear()
        self.live_ohlc_cache.clear()
        if hasattr(self, 'local_history_cache'):
            self.local_history_cache.clear()

    async def get_live_contract_details(self, strike, expiry, type):
        contracts = await self.contract_manager.get_live_option_contracts(self.instrument_key)
        for c in contracts:
            if c.strike_price == strike and c.expiry.date() == expiry and c.instrument_type == type: return c
        return None

    async def prime_aggregator(self, aggregator, instrument_key, timestamp):
        if not instrument_key:
            logger.warning(f"prime_aggregator called with None instrument_key — skipping.")
            return
        df = await self.get_historical_ohlc(instrument_key, aggregator.interval_minutes, timestamp, for_full_day=True, include_current=True)
        if not df.empty:
            aggregator.prime_with_history(instrument_key, df[df.index < timestamp])
