import pandas as pd
import numpy as np

class ROCIndicator:
    """
    Implementation of the Rate of Change (ROC) indicator.

    Formula: ROC = 100 * (Source - Source[length]) / Source[length]

    This matches the PineScript indicator:
    roc = 100 * (source - source[length])/source[length]
    """

    @staticmethod
    def calculate(series: pd.Series, length: int) -> pd.Series:
        """
        Calculates ROC for a given data series.

        Args:
            series: pd.Series containing the price/source data.
            length: The period lookback for ROC.

        Returns:
            pd.Series containing ROC values.
        """
        if series is None or series.empty or len(series) <= length:
            return pd.Series(dtype=float)

        # ROC Calculation
        # source[length] refers to the value 'length' periods ago
        prev_source = series.shift(length)

        # 100 * (current - past) / past
        roc = 100 * (series - prev_source) / prev_source

        return roc

    @staticmethod
    def get_latest_value(series: pd.Series, length: int) -> float:
        """Helper to get only the most recent ROC value."""
        roc_series = ROCIndicator.calculate(series, length)
        if roc_series.empty:
            return None

        val = roc_series.iloc[-1]
        return float(val) if not np.isnan(val) else None
