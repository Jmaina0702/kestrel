"""
backend/data/mt5_client.py

The single point of contact with the MetaTrader5 Python package. No
other module (Instance B's engine, Instance C's frontend queries) should
ever `import MetaTrader5` directly (Section 8.2) -- every broker-API
quirk gets handled in exactly one place instead of scattered across the
codebase, and everything this module returns is already in Kestrel's
own types (float, dict, pandas DataFrame) with correct true-UTC
timestamps, never a raw MT5 struct.

The one deliberate exception is time_sync.py (Section 8.4): it computes
the broker-offset itself by reading a raw, unconverted tick directly
from MetaTrader5, because every other function in *this* module needs
that offset already known before it can convert anything -- asking
time_sync to go through mt5_client.get_current_tick() to discover the
offset would be circular (you'd need the offset to get a tick whose
whole purpose is computing the offset). time_sync.py is Instance A's own
sibling module for exactly this reason, not a third party reaching into
MT5 -- Section 5 names it explicitly as "the boundary where broker-server
time enters the system."

Concurrency: the engine loop and the balance recorder run on separate
threads inside the same backend process, and both call into this module.
The MetaTrader5 Python API is not documented as safe under concurrent
calls from multiple threads, so every MT5 call here is serialized
through one module-level lock. That removes an entire category of
race-condition bugs before they can happen, at the cost of one thread
occasionally waiting a few milliseconds behind another -- a trivial
price for a bot polling every few seconds, not a real-time system.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Literal

import pandas as pd

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

Direction = Literal["BUY", "SELL"]
Timeframe = Literal["M1", "M15", "H1"]

_lock = threading.Lock()
_connected = False

# Kestrel's own magic number, used to tag every order this bot places so
# it's unambiguous in the MT5 terminal (and in any manual audit) which
# positions are Kestrel's versus a human clicking around in the same
# account.
_MAGIC_NUMBER = 20260813

# Slippage tolerance for market orders, in points. XAUUSD moves fast
# enough around session opens that zero deviation tolerance would reject
# valid fills constantly; this is a small, deliberate allowance to absorb
# normal spread noise without accepting a materially worse fill than the
# signal intended.
_DEVIATION_POINTS = 20


class NotConnectedError(RuntimeError):
    """Raised when an MT5-dependent call is made before connect() has succeeded."""


def _require_connected() -> None:
    if not _connected:
        raise NotConnectedError(
            "mt5_client is not connected -- call connect() successfully first."
        )


def _timeframe_map() -> dict[str, int]:
    # Built lazily (not at module import time) so that importing this
    # module never fails just because MT5 constants aren't resolvable
    # yet in some test/mock context -- only actually calling get_ohlc()
    # requires them.
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
    }


def connect(account: int, password: str, server: str) -> tuple[bool, str]:
    """
    Establishes the MT5 terminal connection and validates that
    config.SYMBOL exists on this broker. Returns (success, message)
    where message is always human-readable -- Section 10.1's /connect
    endpoint shows it directly to Joy on the login page, never a raw
    exception string.

    The password is used here and only here: passed straight into
    mt5.initialize() and never stored, logged, or written to disk
    anywhere in this function or any caller (Section 5's no-secrets rule).
    """
    global _connected

    with _lock:
        ok = mt5.initialize(login=account, password=password, server=server)
        if not ok:
            code, description = mt5.last_error()
            message = f"MT5 connection failed: {description} (code {code})"
            db.log_event("ERROR", message)
            _connected = False
            return False, message

        symbol_info = mt5.symbol_info(config.SYMBOL)
        if symbol_info is None:
            mt5.shutdown()
            message = (
                f"Symbol '{config.SYMBOL}' not found on this broker -- check "
                f"the exact name in MT5's Market Watch panel (some brokers "
                f"suffix it, e.g. 'XAUUSD.s') and update config.SYMBOL."
            )
            db.log_event("ERROR", message)
            _connected = False
            return False, message

        _connected = True
        message = f"Connected to account {account} on {server}."
        db.log_event("INFO", message)
        return True, message


def disconnect() -> None:
    """
    Calls mt5.shutdown(). Safe to call even when not currently connected
    -- mt5.shutdown() is itself a no-op with nothing initialized, so this
    never raises, and only logs a disconnect event if there was actually
    a live connection to end.
    """
    global _connected

    with _lock:
        mt5.shutdown()
        was_connected = _connected
        _connected = False

    if was_connected:
        db.log_event("INFO", "Disconnected from MT5.")


def get_account_info() -> dict:
    """
    Returns {'balance': float, 'equity': float, 'margin': float,
    'currency': str} from mt5.account_info(). Raises NotConnectedError if
    called before connect() has succeeded -- a loud exception here
    surfaces a real ordering bug in the caller rather than returning
    silently wrong data.
    """
    _require_connected()

    with _lock:
        info = mt5.account_info()

    if info is None:
        code, description = mt5.last_error()
        message = f"account_info() returned None: {description} (code {code})"
        db.log_event("ERROR", message)
        raise RuntimeError(message)

    return {
        "balance": float(info.balance),
        "equity": float(info.equity),
        "margin": float(info.margin),
        "currency": str(info.currency),
    }


def get_ohlc(timeframe: Timeframe, count: int, start_pos: int = 0) -> pd.DataFrame:
    """
    Fetches `count` bars of config.SYMBOL at the given timeframe,
    starting `start_pos` bars back from the most recent closed bar
    (start_pos=0 = the most recent `count` bars). Used by the frontend's
    lazy scroll-back chart loading -- increasing start_pos fetches older
    history without ever loading the full dataset up front.

    Returns a DataFrame with a UTC-aware DatetimeIndex named 'time_utc'
    and columns ['open', 'high', 'low', 'close', 'tick_volume']. The
    broker-offset correction from time_sync.py is applied here, so every
    DataFrame this function returns is already true UTC -- no downstream
    module should ever need to do timezone math again.
    """
    _require_connected()

    tf_map = _timeframe_map()
    if timeframe not in tf_map:
        raise ValueError(f"Unknown timeframe {timeframe!r} -- expected one of {list(tf_map)}")

    with _lock:
        rates = mt5.copy_rates_from_pos(config.SYMBOL, tf_map[timeframe], start_pos, count)

    if rates is None or len(rates) == 0:
        code, description = mt5.last_error()
        message = f"get_ohlc({timeframe}) returned no data: {description} (code {code})"
        db.log_event("WARNING", message)
        return pd.DataFrame(columns=["open", "high", "low", "close", "tick_volume"])

    from .time_sync import get_offset_hours  # local import: avoids a
    # circular import at module load time (time_sync doesn't import this
    # module, but importing it at the top would still force it to exist
    # and fully initialize before mt5_client can, which is unnecessary
    # coupling for a function that isn't called until runtime anyway).

    offset = get_offset_hours()

    df = pd.DataFrame(rates)
    server_time = pd.to_datetime(df["time"], unit="s")  # naive broker wall-clock
    true_utc_time = server_time - timedelta(hours=offset)
    df.index = pd.DatetimeIndex(true_utc_time).tz_localize("UTC")
    df.index.name = "time_utc"

    return df[["open", "high", "low", "close", "tick_volume"]]


def get_current_tick() -> dict:
    """
    Returns {'bid': float, 'ask': float, 'time_utc': datetime} for
    config.SYMBOL, with true-UTC timestamp correction already applied.
    Used for live price streaming on the chart and live floating-P&L
    calculation on the Live Trades tab.
    """
    _require_connected()

    with _lock:
        tick = mt5.symbol_info_tick(config.SYMBOL)

    if tick is None:
        code, description = mt5.last_error()
        message = f"get_current_tick() returned None: {description} (code {code})"
        db.log_event("WARNING", message)
        raise RuntimeError(message)

    from .time_sync import get_offset_hours

    offset = get_offset_hours()
    server_time = datetime.utcfromtimestamp(tick.time)
    true_utc = (server_time - timedelta(hours=offset)).replace(tzinfo=timezone.utc)

    return {"bid": float(tick.bid), "ask": float(tick.ask), "time_utc": true_utc}


def place_order(direction: Direction, lot: float, sl: float, tp: float) -> tuple[bool, int, str]:
    """
    Places a market order on config.SYMBOL via mt5.order_send() with
    action=TRADE_ACTION_DEAL. Returns (success, mt5_ticket, message). On
    failure, mt5_ticket is 0 and message includes the broker's own
    rejection reason (result.comment) -- a generic "order failed" message
    is useless for diagnosing whether it was invalid stops, insufficient
    margin, or a closed market, so the real reason is always surfaced.

    Every attempt, successful or not, is logged to system_events.
    """
    _require_connected()

    with _lock:
        tick = mt5.symbol_info_tick(config.SYMBOL)
        if tick is None:
            code, description = mt5.last_error()
            message = f"place_order failed: no tick data ({description}, code {code})"
            db.log_event("ERROR", message)
            return False, 0, message

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if direction == "BUY" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.SYMBOL,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": _DEVIATION_POINTS,
            "magic": _MAGIC_NUMBER,
            "comment": "kestrel",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)

    if result is None:
        code, description = mt5.last_error()
        message = f"place_order failed: order_send returned None ({description}, code {code})"
        db.log_event("ERROR", message)
        return False, 0, message

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        message = f"place_order rejected: {result.comment} (retcode {result.retcode})"
        db.log_event("ERROR", message)
        return False, 0, message

    message = (
        f"Order placed: {direction} {lot} lot(s) {config.SYMBOL} @ {price}, "
        f"ticket {result.order}"
    )
    db.log_event("INFO", message)
    return True, int(result.order), message


def close_order(ticket: int) -> tuple[bool, str]:
    """
    Closes a specific open position by ticket, via an opposing market
    order referencing the position. Used both for kill-switch force-closes
    (Section 11) and for reconciling a position the database still shows
    open but MT5 no longer has.
    """
    _require_connected()

    with _lock:
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            message = f"close_order({ticket}) failed: position not found (already closed?)"
            db.log_event("WARNING", message)
            return False, message

        position = positions[0]
        tick = mt5.symbol_info_tick(config.SYMBOL)
        if tick is None:
            code, description = mt5.last_error()
            message = f"close_order({ticket}) failed: no tick data ({description}, code {code})"
            db.log_event("ERROR", message)
            return False, message

        closing_type = (
            mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        )
        price = tick.bid if closing_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.SYMBOL,
            "volume": position.volume,
            "type": closing_type,
            "position": ticket,
            "price": price,
            "deviation": _DEVIATION_POINTS,
            "magic": _MAGIC_NUMBER,
            "comment": "kestrel-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)

    if result is None:
        code, description = mt5.last_error()
        message = f"close_order({ticket}) failed: order_send returned None ({description}, code {code})"
        db.log_event("ERROR", message)
        return False, message

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        message = f"close_order({ticket}) rejected: {result.comment} (retcode {result.retcode})"
        db.log_event("ERROR", message)
        return False, message

    message = f"Position {ticket} closed @ {price}."
    db.log_event("INFO", message)
    return True, message


def modify_sl(ticket: int, new_sl: float) -> tuple[bool, str]:
    """
    Modifies the stop-loss on an open position via TRADE_ACTION_SLTP.
    Not called anywhere in v1 yet -- reserved for the v2 breakeven-cascade
    (Section 13) -- but implemented now since it's cheap to build
    correctly alongside the rest of this module while the request-dict
    conventions are already fresh, rather than revisiting this file later.
    """
    _require_connected()

    with _lock:
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            message = f"modify_sl({ticket}) failed: position not found"
            db.log_event("WARNING", message)
            return False, message

        position = positions[0]

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": config.SYMBOL,
            "position": ticket,
            "sl": new_sl,
            "tp": position.tp,
        }

        result = mt5.order_send(request)

    if result is None:
        code, description = mt5.last_error()
        message = f"modify_sl({ticket}) failed: order_send returned None ({description}, code {code})"
        db.log_event("ERROR", message)
        return False, message

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        message = f"modify_sl({ticket}) rejected: {result.comment} (retcode {result.retcode})"
        db.log_event("ERROR", message)
        return False, message

    message = f"Position {ticket} SL modified to {new_sl}."
    db.log_event("INFO", message)
    return True, message


def get_open_positions() -> list[dict]:
    """
    Returns every open position on config.SYMBOL as a list of dicts:
    {'ticket', 'direction', 'lot', 'entry', 'sl', 'tp', 'pnl', 'opened_at'}
    (opened_at is a true-UTC datetime).

    Used by the engine's reconciliation step (compare this ground truth
    against what the database thinks is open) and by the kill switch
    (iterate every position found here and close it). The frontend reads
    from SQLite per Section 3's architecture rather than calling this
    directly -- MT5 calls stay inside the backend process only.
    """
    _require_connected()

    with _lock:
        positions = mt5.positions_get(symbol=config.SYMBOL)

    if positions is None:
        return []

    from .time_sync import get_offset_hours

    offset = get_offset_hours()
    offset_delta = timedelta(hours=offset)

    result: list[dict] = []
    for p in positions:
        server_time = datetime.utcfromtimestamp(p.time)
        true_utc = (server_time - offset_delta).replace(tzinfo=timezone.utc)
        result.append(
            {
                "ticket": int(p.ticket),
                "direction": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "lot": float(p.volume),
                "entry": float(p.price_open),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "pnl": float(p.profit),
                "opened_at": true_utc,
            }
        )
    return result
