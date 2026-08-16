"""
backend/config.py

Central configuration for Kestrel. Every other module imports constants
from here rather than hardcoding values -- this is the single place a
parameter gets tuned, instead of hunting through the codebase.

Ownership: Instance A (Section 7 of KESTREL_MASTER_PRD.md). Instance B
and Instance C both import from this module; nothing here should be
renamed or restructured without flagging the change in PROGRESS.md, since
that would break the contract both other instances build against.

Per Section 5 (Coding Standards): this file contains no secrets. The MT5
account number, password, and server are never stored here or anywhere
on disk -- they live in backend process memory only, for the duration of
a session, passed in through the /connect endpoint (Section 10.1).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Instrument & position sizing
# ---------------------------------------------------------------------------

SYMBOL: str = "XAUUSD"
# VERIFY against your broker's exact symbol name in MT5 Market Watch before
# running -- some brokers suffix this (e.g. "XAUUSD.s"). mt5_client.connect()
# must validate this symbol exists and fail loudly if it doesn't, rather
# than silently returning no data later.

LOT_SIZE: float = 0.01
# Fixed size for the entire v1 build. Per Section 13, position sizing and
# risk-percentage logic are explicitly deferred until the brain is
# validated on demo -- do not scale this anywhere in this build.

# ---------------------------------------------------------------------------
# Indicator parameters
# ---------------------------------------------------------------------------

EMA_PERIOD: int = 50
ATR_PERIOD: int = 14
ZONE_BUFFER_MULT: float = 0.25

STOCH_K_PERIOD: int = 5
STOCH_SLOWING: int = 3
STOCH_D_PERIOD: int = 3
STOCH_BASE_OVERSOLD: int = 20        # base trigger threshold
STOCH_BASE_OVERBOUGHT: int = 80
STOCH_DEEP_OVERSOLD: int = 15        # "perfect" scoring threshold -- stricter than base
STOCH_DEEP_OVERBOUGHT: int = 85

# ---------------------------------------------------------------------------
# Bias (H1 directional gate)
# ---------------------------------------------------------------------------

BIAS_LOOKBACK_BARS: int = 5

# ---------------------------------------------------------------------------
# Zones (fractal + ATR-buffered support/resistance)
# ---------------------------------------------------------------------------

MAX_ZONES_PER_SIDE: int = 5

# ---------------------------------------------------------------------------
# Scoring (normal vs. perfect signal classification)
# ---------------------------------------------------------------------------

PERFECT_SCORE_THRESHOLD: int = 3     # out of 3 -- see Section 9.5

# ---------------------------------------------------------------------------
# Order structure
# ---------------------------------------------------------------------------

TP_RR: float = 1.5                   # fixed R:R for v1, single TP, no tiering
PERFECT_ORDER_COUNT: int = 3         # stacked orders for a "perfect" signal

# ---------------------------------------------------------------------------
# Sessions (UTC) -- see Section 9.4 for the offset-conversion mechanism.
# Windows are approximate, based on observed timing clustering in the
# reference trader's data, not verified ground truth. If live results
# suggest they're off, that's a config change here, not a code change.
# ---------------------------------------------------------------------------

SESSION_WINDOWS_UTC: list[tuple[str, str]] = [
    ("08:00", "11:00"),   # London open
    ("13:00", "17:00"),   # London/NY overlap
]

# ---------------------------------------------------------------------------
# News blackout -- manual entries for v1. Real economic-calendar API
# integration is out of scope until v2 (Section 13).
# ---------------------------------------------------------------------------

NEWS_BLACKOUT_WINDOWS: list[tuple[str, str]] = []
# List of (start_utc_iso, end_utc_iso) tuples, e.g.
# ("2026-08-15T12:25:00", "2026-08-15T12:35:00"). Empty by default.

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

ENGINE_LOOP_INTERVAL_SEC: int = 5
# How often the engine checks for a new closed M1 candle -- NOT how often
# it trades. Candle-close discipline (Section 9.6) is what prevents this
# from re-evaluating the same candle multiple times before it closes.

BALANCE_SNAPSHOT_INTERVAL_SEC: int = 60
TIME_OFFSET_REFRESH_INTERVAL_SEC: int = 86400   # recompute broker offset daily

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DB_PATH: str = "kestrel.db"
