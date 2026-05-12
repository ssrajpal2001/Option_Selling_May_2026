"""
Baseline tests for VWAP, RSI, and ROC indicators.

On first run with --update-baseline: records golden values to tests/baseline.json.
On subsequent runs: asserts indicator values match baseline (drift = bug).

Run:
    cd bot && python -m pytest ../tests/test_strategy_baseline.py -v
    cd bot && python -m pytest ../tests/test_strategy_baseline.py -v --update-baseline
"""

import sys
import os
import json
import math
import argparse

import pandas as pd
import pytest

# Make bot/ importable when running from the repo root or from bot/
BOT_DIR = os.path.join(os.path.dirname(__file__), '..', 'bot')
if BOT_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(BOT_DIR))

from hub.indicators.vwap import VWAPIndicator
from hub.indicators.rsi import RSIIndicator
from hub.indicators.roc import ROCIndicator

BASELINE_FILE = os.path.join(os.path.dirname(__file__), 'baseline.json')
ATP_FILE = os.path.join(os.path.dirname(__file__), '..', 'bot', 'backtest_data',
                        'atp_data_NIFTY_2026-04-02.csv')


@pytest.fixture(scope='session')
def update_baseline(pytestconfig):
    return pytestconfig.getoption('--update-baseline')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_atp_data(filepath: str) -> pd.DataFrame:
    """Load atp_data CSV. Returns DataFrame sorted by minute_ts."""
    df = pd.read_csv(filepath, parse_dates=['minute_ts'])
    df.sort_values('minute_ts', inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _build_ohlcv_from_spot(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build a synthetic per-minute OHLCV frame from spot_price.
    Uses first occurrence per minute_ts (de-dup by minute).
    VWAP needs high/low/close/volume — we use spot_price as all three
    and unit volume, which is deterministic and reproducible.
    """
    minute_df = df.drop_duplicates('minute_ts').copy()
    minute_df = minute_df.set_index('minute_ts')
    ohlcv = pd.DataFrame({
        'high': minute_df['spot_price'],
        'low': minute_df['spot_price'],
        'close': minute_df['spot_price'],
        'volume': 1.0,
    })
    return ohlcv


def _approx_equal(a: float, b: float, rel_tol: float = 1e-4) -> bool:
    """True if a and b are equal within relative tolerance."""
    if a is None or b is None:
        return a == b
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if a == 0.0 and b == 0.0:
        return True
    return abs(a - b) / max(abs(a), abs(b)) <= rel_tol


# ---------------------------------------------------------------------------
# Compute current indicator values on known checkpoints
# ---------------------------------------------------------------------------

CHECKPOINTS = [20, 50, 100, -1]   # candle indices to record


def _compute_indicator_values(atp_df: pd.DataFrame) -> dict:
    ohlcv = _build_ohlcv_from_spot(atp_df)
    spot_series = ohlcv['close']

    vwap_series = VWAPIndicator.calculate(ohlcv)
    rsi_series = RSIIndicator.calculate(spot_series, period=14)
    roc_series = ROCIndicator.calculate(spot_series, length=9)

    result = {}
    for idx in CHECKPOINTS:
        key = str(idx)
        # Guard against short CSVs
        if abs(idx) > len(ohlcv):
            result[key] = {'vwap': None, 'rsi': None, 'roc': None}
            continue

        def _val(series, i):
            try:
                v = float(series.iloc[i])
                return None if math.isnan(v) else round(v, 6)
            except Exception:
                return None

        result[key] = {
            'vwap': _val(vwap_series, idx),
            'rsi': _val(rsi_series, idx),
            'roc': _val(roc_series, idx),
        }

    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def atp_data():
    if not os.path.exists(ATP_FILE):
        pytest.skip(f'ATP data file not found: {ATP_FILE}')
    return _load_atp_data(ATP_FILE)


@pytest.fixture(scope='module')
def current_values(atp_data):
    return _compute_indicator_values(atp_data)


def test_baseline_vwap(update_baseline, atp_data, current_values):
    """VWAP values at CHECKPOINTS must match baseline.json."""
    baseline = _load_or_update_baseline('vwap', update_baseline, current_values)
    for idx_key, vals in current_values.items():
        expected = baseline.get(idx_key, {}).get('vwap')
        actual = vals['vwap']
        assert _approx_equal(actual, expected), (
            f'VWAP drift at candle {idx_key}: expected={expected}, actual={actual}. '
            f'Run with --update-baseline to record new baseline.'
        )


def test_baseline_rsi(update_baseline, atp_data, current_values):
    """RSI values at CHECKPOINTS must match baseline.json."""
    baseline = _load_or_update_baseline('rsi', update_baseline, current_values)
    for idx_key, vals in current_values.items():
        expected = baseline.get(idx_key, {}).get('rsi')
        actual = vals['rsi']
        assert _approx_equal(actual, expected), (
            f'RSI drift at candle {idx_key}: expected={expected}, actual={actual}. '
            f'Run with --update-baseline to record new baseline.'
        )


def test_baseline_roc(update_baseline, atp_data, current_values):
    """ROC values at CHECKPOINTS must match baseline.json."""
    baseline = _load_or_update_baseline('roc', update_baseline, current_values)
    for idx_key, vals in current_values.items():
        expected = baseline.get(idx_key, {}).get('roc')
        actual = vals['roc']
        assert _approx_equal(actual, expected), (
            f'ROC drift at candle {idx_key}: expected={expected}, actual={actual}. '
            f'Run with --update-baseline to record new baseline.'
        )


# ---------------------------------------------------------------------------
# Sanity checks (always run, never need update-baseline)
# ---------------------------------------------------------------------------

def test_vwap_is_positive(atp_data):
    ohlcv = _build_ohlcv_from_spot(atp_data)
    vwap = VWAPIndicator.calculate(ohlcv)
    assert not vwap.empty, 'VWAP returned empty series'
    assert (vwap.dropna() > 0).all(), 'VWAP must always be positive'


def test_rsi_bounds(atp_data):
    spot = _build_ohlcv_from_spot(atp_data)['close']
    rsi = RSIIndicator.calculate(spot, period=14)
    valid = rsi.dropna()
    assert not valid.empty, 'RSI returned all-NaN series'
    assert (valid >= 0).all() and (valid <= 100).all(), 'RSI must be in [0, 100]'


def test_roc_returns_same_length(atp_data):
    spot = _build_ohlcv_from_spot(atp_data)['close']
    roc = ROCIndicator.calculate(spot, length=9)
    assert len(roc) == len(spot), 'ROC must return same length as input'


def test_vwap_is_monotonically_weighted(atp_data):
    """VWAP should be between session low and session high of spot prices."""
    ohlcv = _build_ohlcv_from_spot(atp_data)
    vwap = VWAPIndicator.calculate(ohlcv).dropna()
    spot_min = ohlcv['close'].min()
    spot_max = ohlcv['close'].max()
    assert vwap.min() >= spot_min * 0.99, 'VWAP fell below session low'
    assert vwap.max() <= spot_max * 1.01, 'VWAP exceeded session high'


# ---------------------------------------------------------------------------
# Baseline persistence helpers
# ---------------------------------------------------------------------------

def _load_or_update_baseline(indicator: str, update: bool, current_values: dict) -> dict:
    """
    Load baseline.json. If update=True or file missing, write current values first.
    Returns the per-checkpoint dict for this indicator.
    """
    if update or not os.path.exists(BASELINE_FILE):
        _write_baseline(current_values)

    with open(BASELINE_FILE, 'r') as f:
        data = json.load(f)

    # Reformat: baseline stores {idx_key: {vwap, rsi, roc}}
    # Return only the indicator slice: {idx_key: <value>}
    return {k: v for k, v in data.items()}


def _write_baseline(current_values: dict):
    with open(BASELINE_FILE, 'w') as f:
        json.dump(current_values, f, indent=2)
    print(f'\nBaseline written to {BASELINE_FILE}')
