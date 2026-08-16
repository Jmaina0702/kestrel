"""
Core technical indicators for Kestrel.

All functions take a pandas DataFrame with at least ['high', 'low', 'close']
columns and return a pandas Series/DataFrame aligned to that DataFrame's
index. No lookahead: every value at row i uses only data available up to
and including row i (the fractal detector in zones.py is the one deliberate
exception, and it's documented there).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range, Wilder-smoothed.

    This matches MT4/MT5's built-in ATR indicator (which uses Wilder's
    smoothing, not a plain simple-moving-average of true range). If you
    compare this against the ATR value shown on a live MT5 chart, they
    should track closely once warmed up.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder's smoothing is mathematically an EMA with alpha = 1/period
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def stochastic(
    df: pd.DataFrame,
    k_period: int = 5,
    slowing: int = 3,
    d_period: int = 3,
) -> pd.DataFrame:
    """
    Slow stochastic oscillator, matching MT4/MT5's Stochastic(5,3,3) default.

    Returns a DataFrame with columns ['k', 'd'] aligned to df's index.
    'k' here is the *slowed* %K (raw %K smoothed by `slowing`), and 'd'
    is the signal line -- this is exactly what MT5 plots as %K/%D by
    default, not the raw unsmoothed %K.
    """
    high, low, close = df["high"], df["low"], df["close"]

    lowest_low = low.rolling(k_period).min()
    highest_high = high.rolling(k_period).max()
    # dead-flat candle ranges (highest == lowest) would otherwise divide by
    # zero -- replace with NaN so it propagates cleanly instead of crashing
    rng = (highest_high - lowest_low).replace(0, np.nan)

    raw_k = 100 * (close - lowest_low) / rng
    k = raw_k.rolling(slowing).mean()
    d = k.rolling(d_period).mean()

    return pd.DataFrame({"k": k, "d": d}, index=df.index)
