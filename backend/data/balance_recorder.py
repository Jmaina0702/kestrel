"""
backend/data/balance_recorder.py

A background recorder that snapshots account balance/equity/floating
P&L into balance_snapshots on a fixed interval, independent of whether
the trading brain (Instance B) exists yet or is even running. Per
Section 8.3: this starts immediately after a successful MT5 connect,
deliberately before the brain -- the Accounts tab's growth-over-time
chart needs real history by the time Joy first looks at it, and a
recorder that only starts once the brain exists would leave that chart
empty for however long the brain takes to build.

This module defines two entry points for Instance B to consume:

  1. run(stop_event: threading.Event) -> None
     The loop itself. Instance B can thread this however it wants,
     consistent with however it threads the brain loop.

  2. start() -> tuple[threading.Thread, threading.Event]
     A convenience wrapper that creates the event, starts a daemon
     thread, and hands both back. If Instance B just wants "start it
     and get a way to stop it later," this is the one-liner.

Error handling: a background recorder's whole job is to keep recording
across whatever transient failures MT5 throws at it. A balance history
with an occasional gap beats a balance history that silently dies
forever the first time MT5 hiccups. Every exception caught here --
expected (NotConnectedError) or not -- is logged, never swallowed
silently, which is what Section 5 actually asks for. The one deliberate
broad `except Exception` is called out explicitly in this docstring and
in the run() function rather than left to look like an oversight.
"""

from __future__ import annotations

import threading

from . import db, mt5_client
from .. import config


def _record_once() -> None:
    """
    Fetches current account state and open positions, sums floating P&L
    across every open position, and inserts one row into
    balance_snapshots. Raises whatever mt5_client raises -- the caller
    (run(), below) is responsible for catching and logging, so this
    stays a pure "do the thing or raise" unit that's simple to reason
    about and simple to test in isolation from the loop/threading logic.
    """
    account = mt5_client.get_account_info()
    positions = mt5_client.get_open_positions()
    open_pnl = sum(p["pnl"] for p in positions)

    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO balance_snapshots (timestamp_utc, balance, equity, open_pnl) "
            "VALUES (?, ?, ?, ?)",
            (db.utc_now_iso(), account["balance"], account["equity"], open_pnl),
        )
        conn.commit()
    finally:
        conn.close()


def run(stop_event: threading.Event) -> None:
    """
    The recorder loop itself -- the intended target of a
    threading.Thread started from main.py immediately after a
    successful MT5 connect. Runs until stop_event is set, snapshotting
    every config.BALANCE_SNAPSHOT_INTERVAL_SEC seconds.

    A single failed snapshot (MT5 hiccups, a momentary disconnect) is
    caught, logged, and skipped rather than killing the loop -- a
    balance history with an occasional gap is far more useful than a
    balance history that silently stops recording forever the first
    time MT5 blips. This is the one deliberate broad `except Exception`
    in this module: not a bare `except:` (Section 5 forbids those
    outright), but a specific, documented choice to keep a background
    recorder thread alive across failure modes that can't all be
    enumerated in advance -- with every failure still logged, never
    swallowed silently, satisfying Section 5's "handle failure
    explicitly" rule rather than working around it.
    """
    db.log_event("INFO", "Balance recorder started.")

    while not stop_event.is_set():
        try:
            _record_once()
        except mt5_client.NotConnectedError as exc:
            db.log_event("WARNING", f"Balance recorder skipped a snapshot: {exc}")
        except Exception as exc:  # noqa: BLE001 -- see docstring above
            db.log_event("ERROR", f"Balance recorder snapshot failed: {exc}")

        stop_event.wait(config.BALANCE_SNAPSHOT_INTERVAL_SEC)

    db.log_event("INFO", "Balance recorder stopped.")


def start() -> tuple[threading.Thread, threading.Event]:
    """
    Convenience for main.py: creates the stop_event, starts run() as a
    daemon thread (so it never blocks process shutdown on its own), and
    hands back both so the caller can request a clean stop later via
    stop_event.set() followed by thread.join().

    main.py is free to ignore this and call run() directly inside its
    own threading.Thread(...) instead, if it wants more control over
    thread naming or lifecycle -- this is a convenience wrapper, not the
    only valid entry point into this module.
    """
    stop_event = threading.Event()
    thread = threading.Thread(target=run, args=(stop_event,), daemon=True, name="balance-recorder")
    thread.start()
    return thread, stop_event
