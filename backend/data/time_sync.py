"""
backend/data/time_sync.py

Instance A's timezone boundary (Section 5): the one place broker-server
time gets converted to true UTC. Every other module in the system --
including the rest of mt5_client.py -- trusts that any timestamp it
receives has already crossed this boundary and is already true UTC. If
a module downstream of this file is ever doing its own timezone math,
that's a sign the boundary was crossed incorrectly somewhere and needs
fixing here, not there.

This module reads MetaTrader5 directly, which Section 8.2 otherwise
reserves for mt5_client.py alone. That's a deliberate, narrow exception,
documented in mt5_client.py's own module docstring too: every function
that needs broker time normally goes through mt5_client.get_current_tick()
or get_ohlc(), which apply this module's offset correction -- but this
module is what computes that offset in the first place, so it needs one
raw, unconverted tick read to do it. Routing that raw read back through
mt5_client's already-corrected wrapper would be circular.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The MetaTrader5 package is Windows-only and requires a running "
        "MT5 terminal on the same machine. Install it with "
        "`pip install MetaTrader5` on the Windows machine that will "
        "actually run the backend -- it cannot be installed or tested "
        "on Linux/macOS."
    ) from exc

from . import db
from .. import config

_cached_offset_hours: float | None = None
_last_computed_monotonic: float | None = None


def get_offset_hours() -> float:
    """
    Returns broker_server_time - true_utc_time, in hours.

    MT5 QUIRK this function exists to correct: mt5.symbol_info_tick(symbol).time
    is a Unix timestamp representing the broker server's WALL-CLOCK
    reading, naively interpreted as if it were UTC seconds-since-epoch --
    it is NOT true UTC unless the broker's server happens to be set to
    UTC (many run EET/EEST instead). Left uncorrected, every timestamp
    in this system would silently be off by however many hours separate
    the broker's clock from UTC, which would corrupt session-window
    gating (Section 9.4) without ever throwing an error -- it would just
    quietly trade at the wrong times.

    Rounds to the nearest 0.5 hour: real broker offsets are whole or
    half hours in practice, and rounding absorbs the few seconds of
    latency between the two clock reads below without producing a
    nonsense fractional offset like 2.0138.

    Caching model: the PRD describes this as "called once at backend
    startup and re-called every TIME_OFFSET_REFRESH_INTERVAL_SEC
    thereafter." Rather than requiring some external scheduler to
    enforce that cadence, this function enforces it internally -- every
    call is a cheap cached-value return except the very first call ever,
    and any call made more than TIME_OFFSET_REFRESH_INTERVAL_SEC after
    the last real computation. This is what makes it safe for
    mt5_client.py to call this on every single tick and OHLC conversion
    (which it does) without spamming system_events or hammering MT5 with
    redundant tick reads -- the overwhelming majority of those calls are
    cache hits, and only a self-throttled minority ever touch MT5 again.

    Logs the detected offset to system_events every time it's actually
    (re)computed -- not on cache-hit calls -- so Joy can cross-check it
    by eye against the MT5 terminal's own candle timestamps without the
    log being flooded by every price fetch.
    """
    global _cached_offset_hours, _last_computed_monotonic

    now_monotonic = _time.monotonic()
    is_stale = (
        _cached_offset_hours is None
        or _last_computed_monotonic is None
        or (now_monotonic - _last_computed_monotonic) >= config.TIME_OFFSET_REFRESH_INTERVAL_SEC
    )

    if not is_stale:
        return _cached_offset_hours

    tick = mt5.symbol_info_tick(config.SYMBOL)
    if tick is None:
        code, description = mt5.last_error()
        message = f"get_offset_hours() could not read a tick: {description} (code {code})"
        db.log_event("ERROR", message)
        if _cached_offset_hours is not None:
            # A transient read failure shouldn't blow up every downstream
            # timestamp conversion in mt5_client.py -- fall back to the
            # last known-good offset rather than raising. A stale-but-
            # recently-correct offset is far less damaging than every
            # caller suddenly crashing mid-session.
            return _cached_offset_hours
        raise RuntimeError(message)

    server_wall_clock = datetime.utcfromtimestamp(tick.time)
    true_utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    raw_offset = (server_wall_clock - true_utc_now).total_seconds() / 3600

    offset = round(raw_offset * 2) / 2  # nearest 0.5 hour

    db.log_event(
        "INFO",
        f"Broker time offset detected: {offset:+.1f}h (raw: {raw_offset:+.4f}h)",
    )

    _cached_offset_hours = offset
    _last_computed_monotonic = now_monotonic
    return offset
