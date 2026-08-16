"""
H1 directional bias -- the top-level gate deciding which trade directions
are even allowed on this pass.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from .indicators import ema

Bias = Literal["bullish", "bearish", "range"]


def compute_h1_bias(h1_df: pd.DataFrame, ema_period: int = 50, lookback: int = 5) -> Bias:
    """
    Bullish: EMA50 is higher now than `lookback` H1 bars ago (rising) AND
             current close is above current EMA50.
    Bearish: EMA50 is lower now than `lookback` bars ago (falling) AND
             current close is below current EMA50.
    Range:   anything else -- flat EMA, or price whipsawing across it.

    Requires at least ema_period + lookback bars of H1 history; returns
    'range' if there isn't enough history yet, since a slope read from too
    little data is not trustworthy and 'range' is the conservative default
    (permits both directions rather than wrongly locking one out).
    """
    df = h1_df.copy()
    df["ema50"] = ema(df["close"], ema_period)

    if len(df) < ema_period + lookback:
        return "range"

    ema_now = df["ema50"].iloc[-1]
    ema_then = df["ema50"].iloc[-1 - lookback]
    price_now = df["close"].iloc[-1]

    rising = ema_now > ema_then
    falling = ema_now < ema_then

    if rising and price_now > ema_now:
        return "bullish"
    if falling and price_now < ema_now:
        return "bearish"
    return "range"
