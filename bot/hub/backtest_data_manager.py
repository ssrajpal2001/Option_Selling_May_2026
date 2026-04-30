import pandas as pd
from utils.logger import logger

class BacktestDataManager:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.data_manager = orchestrator.data_manager
        self._index_ohlc_cache = None
        self._futures_ohlc_cache = None

    async def pre_fetch_underlying_data(self, date_str):
        """Pre-fetches macro data day-by-day descending, stopping once any data is found."""
        idx_key = self.orchestrator.index_instrument_key
        fut_key = self.orchestrator.futures_instrument_key

        # User Requirement: Check one day before, if found, don't check further back.
        # We check up to 4 days back to handle long weekends/holidays.
        target_dt = pd.to_datetime(date_str)

        async def fetch_day_by_day(key):
            if not key: return None
            for i in range(1, 5):
                check_date = (target_dt - pd.Timedelta(days=i)).strftime('%Y-%m-%d')
                try:
                    df = await self.data_manager._fetch_and_prepare_api_data(key, check_date, check_date, '1minute')
                    if df is not None and not df.empty:
                        logger.info(f"[BacktestDataManager] Primed macro data for {key} using history from {check_date}")
                        return df
                except Exception as e:
                    logger.debug(f"Pre-fetch attempt failed for {key} on {check_date}: {e}")
            return None

        if idx_key:
            self._index_ohlc_cache = await fetch_day_by_day(idx_key)
        if fut_key:
            self._futures_ohlc_cache = await fetch_day_by_day(fut_key)

    def get_index_price(self, timestamp):
        if self._index_ohlc_cache is not None and not self._index_ohlc_cache.empty:
            try:
                # Ensure matching timezone awareness
                idx_tz = getattr(self._index_ohlc_cache.index, 'tzinfo', getattr(self._index_ohlc_cache.index, 'tz', None))
                if idx_tz is not None and timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=idx_tz)
                elif idx_tz is None and timestamp.tzinfo is not None:
                    timestamp = timestamp.replace(tzinfo=None)

                relevant = self._index_ohlc_cache[self._index_ohlc_cache.index <= timestamp]
                if not relevant.empty: return relevant.iloc[-1]['close']
                if not self._index_ohlc_cache.empty: return self._index_ohlc_cache.iloc[0]['open']
            except Exception as e:
                logger.error(f"[BacktestDataManager] Index price lookup error at {timestamp}: {e}")
        return None

    def get_futures_price(self, timestamp):
        if self._futures_ohlc_cache is not None and not self._futures_ohlc_cache.empty:
            try:
                # Ensure matching timezone awareness
                idx_tz = getattr(self._futures_ohlc_cache.index, 'tzinfo', getattr(self._futures_ohlc_cache.index, 'tz', None))
                if idx_tz is not None and timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=idx_tz)
                elif idx_tz is None and timestamp.tzinfo is not None:
                    timestamp = timestamp.replace(tzinfo=None)

                relevant = self._futures_ohlc_cache[self._futures_ohlc_cache.index <= timestamp]
                if not relevant.empty: return relevant.iloc[-1]['close']
                if not self._futures_ohlc_cache.empty: return self._futures_ohlc_cache.iloc[0]['open']
            except Exception as e:
                logger.error(f"[BacktestDataManager] Futures price lookup error at {timestamp}: {e}")
        return None
