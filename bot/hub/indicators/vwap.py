import pandas as pd
import numpy as np

class VWAPIndicator:
    """
    Implementation of Volume Weighted Average Price (VWAP).
    Formula: VWAP = sum(Price * Volume) / sum(Volume)
    """

    @staticmethod
    def calculate(df: pd.DataFrame) -> pd.Series:
        """
        Calculates VWAP for a given OHLCV dataframe.
        Expects columns: 'high', 'low', 'close', 'volume'.

        Returns:
            pd.Series containing intraday VWAP values.
        """
        if df is None or df.empty:
            return pd.Series(dtype=float)

        tp = (df['high'] + df['low'] + df['close']) / 3
        vol = df.get('volume', pd.Series(1.0, index=df.index))

        # Intraday cumulative sums
        # Note: This logic assumes the dataframe has already been filtered for the current day.
        cum_pv = (tp * vol).cumsum()
        cum_vol = vol.cumsum()

        vwap = cum_pv / cum_vol
        return vwap

    @staticmethod
    def get_latest_value(df: pd.DataFrame) -> float:
        """Helper to get only the most recent VWAP value."""
        vwap_series = VWAPIndicator.calculate(df)
        if vwap_series.empty:
            return None

        val = vwap_series.iloc[-1]
        return float(val) if not np.isnan(val) else None
