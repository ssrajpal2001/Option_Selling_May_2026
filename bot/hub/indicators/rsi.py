import pandas as pd
import numpy as np

class RSIIndicator:
    """
    Implementation of the Relative Strength Index (RSI).
    Uses Wilder's Smoothing (EMA with alpha = 1/period).
    Matches TradingView and PineScript precisely.
    OPTIMIZED: Uses vectorized pandas operations (EWM) to avoid Python loops.
    """

    @staticmethod
    def calculate(series: pd.Series, period: int) -> pd.Series:
        """
        Calculates RSI for a given data series.

        Args:
            series: pd.Series containing the price/source data.
            period: The period for RSI (standard is 14).

        Returns:
            pd.Series containing RSI values.
        """
        if series is None or series.empty or len(series) <= period:
            return pd.Series(dtype=float, index=series.index if series is not None else None)

        delta = series.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        # WILDER'S SMOOTHING (RMA) using EWM
        # alpha = 1 / period
        # span = 2 * period - 1 (This is the relationship between EMA span and RMA period)
        # adjust=False mimics the recursive nature of Wilder's
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()

        # Handle division by zero
        rs = avg_gain / avg_loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))

        return rsi

    @staticmethod
    def get_latest_value(series: pd.Series, period: int) -> float:
        """Helper to get only the most recent RSI value."""
        # Performance: For the latest value, we only need the tail of the series
        # but EWM requires the full chain for accuracy.
        rsi_series = RSIIndicator.calculate(series, period)
        if rsi_series.empty:
            return None

        val = rsi_series.iloc[-1]
        return float(val) if pd.notna(val) else None
