"""Autonomous live watcher — contract §6.5: the agent checks the weather by itself.

Every LIVE_WATCH_MINUTES minutes (default 30; 0 or an invalid value turns it off; the first
check comes ~10 s after start) a daemon thread starts a live run through the same registry as
POST /api/runs, so it shows up in /api/runs/{id} and over SSE: no live issue yet → trigger
"issue", otherwise "new_weather_run" and the agent decides whether a recalculation is worth it.
A tick is skipped while a live run is still in progress. Every finished live run, the
watcher's (initiator "agent") and the button's ("user"), is journaled by windcast.runs.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading

from windcast import paths, runs

log = logging.getLogger("windcast.watcher")

ENV = "LIVE_WATCH_MINUTES"
DEFAULT_MINUTES = 30
FIRST_DELAY_S = 10.0

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_last_check_local: str | None = None


def configured_minutes() -> float:
    """LIVE_WATCH_MINUTES as a positive number of minutes; 0 means the watcher is off."""
    raw = os.environ.get(ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MINUTES
    try:
        value = float(raw)
    except ValueError:
        return 0
    return value if math.isfinite(value) and value > 0 else 0


def live_published() -> bool:
    """A live issue with at least one version exists (outputs/live/latest.json)."""
    try:
        record = json.loads((paths.live_dir() / "latest.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and bool(record.get("versions"))


def tick() -> runs.Run | None:
    """One check: start a live run unless one is already going. Returns the new run."""
    global _last_check_local
    _last_check_local = runs.now_local_iso()
    trigger = "new_weather_run" if live_published() else "issue"
    run = runs.start_issue(
        runs.LIVE,
        mode=runs.default_mode(),
        trigger=trigger,
        initiator="agent",
        only_if_idle=True,
    )
    if run is None:
        log.info("live watcher: a live run is in progress, tick skipped")
    else:
        log.info("live watcher: started %s (%s)", run.id, trigger)
    return run


def _loop(minutes: float, first_delay: float) -> None:
    if _stop.wait(first_delay):
        return
    while True:
        try:
            tick()
        except Exception:  # never take the process down
            log.exception("live watcher tick failed")
        if _stop.wait(minutes * 60):
            return


def start(minutes: float | None = None, first_delay: float | None = None) -> bool:
    """Start the watcher thread (once). False when LIVE_WATCH_MINUTES turns it off."""
    global _thread
    minutes = configured_minutes() if minutes is None else minutes
    if minutes <= 0:
        log.info("live watcher is off (%s=0)", ENV)
        return False
    delay = FIRST_DELAY_S if first_delay is None else first_delay
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(
            target=_loop,
            args=(minutes, delay),
            name="windcast-live-watcher",
            daemon=True,
        )
        _thread.start()
    log.info("live watcher: every %s min, first check in %.0f s", minutes, delay)
    return True


def stop(timeout: float = 2.0) -> None:
    global _thread
    _stop.set()
    with _lock:
        thread, _thread = _thread, None
    if thread is not None:
        thread.join(timeout)


def running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


def reset() -> None:
    """Stop the thread and forget the last check (tests)."""
    global _last_check_local
    stop()
    _last_check_local = None


def status() -> dict:
    minutes = configured_minutes()
    return {
        "live_watch_minutes": int(minutes) if float(minutes).is_integer() else minutes,
        "live_last_check_local": _last_check_local,
    }
