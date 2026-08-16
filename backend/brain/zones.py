"""
Fractal-based support/resistance zone detection.

A zone is built from a 5-bar fractal (2 bars either side confirm the middle
bar as a local high or low) with a small ATR-scaled buffer around it. A
zone stays "active" until price closes decisively through it -- at that
point it's mitigated and dropped from consideration.

IMPORTANT: fractal confirmation has an unavoidable 2-bar lag. You cannot
know bar i is a fractal until bars i+1 and i+2 exist. That's not a bug --
it's the same lag a human reading the chart has. An indicator that
"confirms" a fractal without waiting for those two bars is repainting,
and repainting indicators are how backtests lie to you.

Requires m15_df to have a DatetimeIndex (UTC). If you're feeding this from
MT5, do: df = df.set_index(pd.to_datetime(df['time'], unit='s', utc=True))
before calling build_zones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

from .indicators import atr

ZoneKind = Literal["support", "resistance"]


@dataclass
class Zone:
    kind: ZoneKind
    price: float          # the fractal price itself
    upper: float          # zone upper bound (price + buffer)
    lower: float          # zone lower bound (price - buffer)
    formed_at: pd.Timestamp
    mitigated: bool = False
    touched: bool = False           # set by signals.py on first live-price touch
    first_touch_at: Optional[pd.Timestamp] = None
    db_id: Optional[int] = None     # set once persisted to the zones table

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def mark_touched(self, at: pd.Timestamp) -> None:
        """
        Marks the zone as touched by live M1 price. This is deliberately a
        method called by signals.py the first time price actually trades
        into the zone during live evaluation -- NOT during build_zones(),
        since "touched" describes a live-price event, not a fact knowable
        at M15-formation time. A zone freshly formed this M15 candle has
        touched == False until price is later observed inside it.
        """
        if not self.touched:
            self.touched = True
            self.first_touch_at = at


def find_fractals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns df with two added boolean columns: 'res_fractal' and 'sup_fractal',
    True on the middle bar of a confirmed 5-bar fractal.
    """
    high, low = df["high"], df["low"]

    res = (
        (high > high.shift(1))
        & (high > high.shift(2))
        & (high > high.shift(-1))
        & (high > high.shift(-2))
    )
    sup = (
        (low < low.shift(1))
        & (low < low.shift(2))
        & (low < low.shift(-1))
        & (low < low.shift(-2))
    )

    out = df.copy()
    out["res_fractal"] = res.fillna(False)
    out["sup_fractal"] = sup.fillna(False)
    return out


def build_zones(
    m15_df: pd.DataFrame,
    buffer_mult: float = 0.25,
    max_zones_per_side: int = 5,
    atr_period: int = 14,
) -> list[Zone]:
    """
    Builds the current list of active (unmitigated) zones from M15 data.

    Note on performance: this recomputes zone state from the full history
    on every call. Fine for the data sizes here (a few hundred to a few
    thousand M15 bars). If Kestrel is later re-evaluating every few seconds
    against months of history, switch to maintaining zone state
    incrementally instead of recomputing from scratch each time -- flagging
    this now so it doesn't get discovered the hard way later.
    """
    df = m15_df.copy()
    df["atr"] = atr(df, atr_period)
    df = find_fractals(df)

    zones: list[Zone] = []

    for i in range(len(df)):
        row = df.iloc[i]
        buf = row["atr"] * buffer_mult if pd.notna(row["atr"]) else 0.0

        if row["res_fractal"]:
            zones.append(
                Zone(
                    kind="resistance",
                    price=row["high"],
                    upper=row["high"] + buf,
                    lower=row["high"] - buf,
                    formed_at=row.name,
                )
            )
        if row["sup_fractal"]:
            zones.append(
                Zone(
                    kind="support",
                    price=row["low"],
                    upper=row["low"] + buf,
                    lower=row["low"] - buf,
                    formed_at=row.name,
                )
            )

        # mitigate: a resistance zone dies once price closes above it;
        # a support zone dies once price closes below it
        close = row["close"]
        for z in zones:
            if z.mitigated:
                continue
            if z.kind == "resistance" and close > z.upper:
                z.mitigated = True
            elif z.kind == "support" and close < z.lower:
                z.mitigated = True

    active = [z for z in zones if not z.mitigated]

    active_res = [z for z in active if z.kind == "resistance"][-max_zones_per_side:]
    active_sup = [z for z in active if z.kind == "support"][-max_zones_per_side:]

    return active_res + active_sup
