"""Run registry for the API: background execution of the agent and event fan-out for SSE.

A run calls RUNNER(issue_date, mode=..., trigger=..., emit=...) in a daemon thread; every event
the runner emits is numbered, stored in memory and pushed to every SSE subscriber. The last
event of a run is always a verdict (added here if the runner did not emit one), so the stream
can close after it. A backtest is one run that calls RUNNER for each date in turn.

Runs live in memory only: serve the API with ONE uvicorn worker. Published results survive in
outputs/ (the agent writes them); a restart only forgets in-flight streams.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from windcast import timeline

log = logging.getLogger("windcast.runs")

Emit = Callable[[dict], None]

PING_INTERVAL_S = 15.0  # SSE comment while idle, keeps proxies from dropping the stream
RETRY_MS = 3000  # EventSource reconnect delay
MAX_RUNS = 200  # finished runs beyond this are forgotten, oldest first

AGENT_MISSING = "Агент ещё не подключён"


class AgentUnavailable(RuntimeError):
    """windcast.agent cannot be imported yet (it is developed in parallel)."""


def _default_runner(issue_date: str, *, mode: str, trigger: str, emit: Emit) -> Any:
    try:
        from windcast.agent import run_issue
    except ImportError as exc:
        raise AgentUnavailable(str(exc)) from exc
    return run_issue(issue_date, mode=mode, trigger=trigger, emit=emit)


# Tests replace this with a fake; looked up at call time.
RUNNER: Callable[..., Any] = _default_runner


def jsonable(obj: Any) -> Any:
    """Plain JSON value: non-finite floats -> None, numpy/pandas scalars -> Python, dates -> ISO."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    for attr in ("item", "tolist"):  # numpy / pandas scalars and arrays
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                value = method()
            except (TypeError, ValueError):
                continue
            if value is not obj:
                return jsonable(value)
    return str(obj)


def now_local_iso() -> str:
    return datetime.now(timeline.LOCAL_TZ).isoformat(timespec="seconds")


def plural_issues(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} выпуск"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} выпуска"
    return f"{n} выпусков"


@dataclass(eq=False)
class Run:
    id: str
    kind: str  # "issue" | "backtest"
    issue_date: str | None  # "YYYY-MM-DD" | "live"; None for a backtest
    trigger: str
    mode: str
    date_from: str | None = None
    date_to: str | None = None
    created_at: str = field(default_factory=now_local_iso)
    current_issue: str | None = None
    version: int | None = None
    done: bool = False
    events: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _waiters: list = field(default_factory=list, repr=False)

    # --- producer side (runner thread) ---
    def emit(self, event: Any, *, issue_date: str | None = None) -> dict:
        """Store one event and wake the subscribers. issue_date forces meta.issue_date."""
        raw = jsonable(event) if isinstance(event, dict) else {"title": str(event)}
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        if issue_date:
            meta["issue_date"] = issue_date
        elif not meta.get("issue_date") and self.issue_date:
            meta["issue_date"] = self.issue_date
        ev = {  # key order of docs/references/trace-event-schema.md
            "seq": 0,
            "ts": raw.get("ts") or now_local_iso(),
            "type": raw.get("type") or "thought",
            "title": raw.get("title") if raw.get("title") is not None else "",
            "body": raw.get("body") if raw.get("body") is not None else "",
            "meta": meta,
        }
        for key, value in raw.items():
            ev.setdefault(key, value)
        with self._lock:
            ev["seq"] = len(self.events) + 1
            self.events.append(ev)
        self._notify()
        return ev

    def finish(self, version: int | None = None) -> None:
        with self._lock:
            self.version = version
            self.done = True
            self.current_issue = None
        self._notify()

    def _notify(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for loop, flag in waiters:
            try:
                loop.call_soon_threadsafe(flag.set)
            except RuntimeError:  # the subscriber's loop is already closed
                pass

    # --- consumer side (SSE handlers) ---
    def snapshot(self, cursor: int) -> tuple[list[dict], bool]:
        """Events after position `cursor` and whether the run is done, read atomically."""
        with self._lock:
            return self.events[cursor:], self.done

    def event_count(self) -> int:
        with self._lock:
            return len(self.events)

    async def wait(self, cursor: int, timeout: float) -> bool:
        """Wait for an event past `cursor` or the end of the run; False on timeout."""
        waiter = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            if len(self.events) > cursor or self.done:
                return True
            self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter[1].wait(), timeout)
            return True
        except TimeoutError:
            return False
        finally:
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    def to_dict(self) -> dict:
        with self._lock:
            data = {
                "id": self.id,
                "issue_date": self.issue_date,
                "trigger": self.trigger,
                "mode": self.mode,
                "done": self.done,
                "version": self.version,
                "events": list(self.events),
                "kind": self.kind,
                "created_at": self.created_at,
            }
            if self.kind == "backtest":
                data.update(
                    {
                        "from": self.date_from,
                        "to": self.date_to,
                        "current_issue": self.current_issue,
                    }
                )
        return data


# --- registry -----------------------------------------------------------------------------

_REGISTRY: OrderedDict[str, Run] = OrderedDict()
_REGISTRY_LOCK = threading.Lock()
_ISSUE_LOCKS: dict[str, threading.Lock] = {}


def reset() -> None:
    """Forget every run (tests)."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def get(run_id: str) -> Run | None:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(run_id)


def running_issue_dates() -> set[str]:
    """Issue dates a run is working on right now (for status "running" in /api/issues)."""
    with _REGISTRY_LOCK:
        active = [run for run in _REGISTRY.values() if not run.done]
    dates = set()
    for run in active:
        current = run.current_issue or (run.issue_date if run.kind == "issue" else None)
        if current:
            dates.add(current)
    return dates


def _register(run: Run) -> None:
    # caller holds _REGISTRY_LOCK
    _REGISTRY[run.id] = run
    if len(_REGISTRY) > MAX_RUNS:
        for old_id in [rid for rid, old in _REGISTRY.items() if old.done]:
            if len(_REGISTRY) <= MAX_RUNS:
                break
            del _REGISTRY[old_id]


def _new_id() -> str:
    return f"r_{uuid.uuid4().hex[:12]}"


def _start(run: Run, target: Callable[..., None], *args: Any) -> None:
    thread = threading.Thread(
        target=target, args=(run, *args), name=f"windcast-{run.id}", daemon=True
    )
    thread.start()


def start_issue(issue_date: str, *, mode: str, trigger: str) -> Run:
    """Start (or join, if the same one is already running) a run for one issue."""
    with _REGISTRY_LOCK:
        for run in _REGISTRY.values():
            if (
                run.kind == "issue"
                and not run.done
                and (run.issue_date, run.trigger, run.mode)
                == (issue_date, trigger, mode)
            ):
                return run
        run = Run(
            id=_new_id(),
            kind="issue",
            issue_date=issue_date,
            trigger=trigger,
            mode=mode,
            current_issue=issue_date,
        )
        _register(run)
    _start(run, _execute_issue)
    return run


def start_backtest(dates: Iterable[date], *, mode: str) -> Run:
    """One run over several issue dates, in order (trigger "issue")."""
    days = [d.isoformat() for d in dates]
    if not days:
        raise ValueError("Пустой период бэктеста")
    with _REGISTRY_LOCK:
        for run in _REGISTRY.values():
            if (
                run.kind == "backtest"
                and not run.done
                and (run.date_from, run.date_to, run.mode) == (days[0], days[-1], mode)
            ):
                return run
        run = Run(
            id=_new_id(),
            kind="backtest",
            issue_date=None,
            trigger="issue",
            mode=mode,
            date_from=days[0],
            date_to=days[-1],
        )
        _register(run)
    _start(run, _execute_backtest, days)
    return run


# --- execution ----------------------------------------------------------------------------


@contextmanager
def _issue_lock(run: Run, issue_date: str, emit: Emit) -> Iterator[None]:
    """One run per issue date at a time: the agent rewrites that date's files."""
    with _REGISTRY_LOCK:
        lock = _ISSUE_LOCKS.setdefault(issue_date, threading.Lock())
    run.current_issue = issue_date
    if not lock.acquire(blocking=False):
        emit(
            {
                "type": "thought",
                "title": f"Жду: по выпуску {issue_date} уже идёт другой прогон",
                "meta": {"status": "skip"},
            }
        )
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _version_of(result: Any) -> int | None:
    value = result.get("version") if isinstance(result, dict) else result
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _call_runner(run: Run, issue_date: str, trigger: str, emit: Emit) -> int | None:
    with _issue_lock(run, issue_date, emit):
        return _version_of(
            RUNNER(issue_date, mode=run.mode, trigger=trigger, emit=emit)
        )


def _error_event(exc: BaseException) -> dict:
    if isinstance(exc, AgentUnavailable):
        return {
            "type": "error",
            "title": AGENT_MISSING,
            "body": f"Модуль windcast.agent недоступен: {exc}",
            "meta": {"status": "error"},
        }
    title = "Запрос отклонён" if isinstance(exc, ValueError) else "Ошибка агента"
    return {
        "type": "error",
        "title": title,
        "body": f"{type(exc).__name__}: {exc}",
        "meta": {"status": "error"},
    }


def _close_issue(
    emit: Emit, run: Run, start: int, version: int | None, failed: bool
) -> None:
    """Make sure the events of one issue (those after position `start`) end with a verdict."""
    emitted, _ = run.snapshot(start)
    if not failed and emitted and emitted[-1].get("type") == "verdict":
        return
    if failed:
        emit(
            {
                "type": "verdict",
                "title": "Выпуск не выполнен",
                "body": "Подробности — в событии об ошибке выше.",
                "meta": {"status": "error"},
            }
        )
    else:
        emit(
            {
                "type": "verdict",
                "title": "Прогон завершён",
                "body": f"Версия {version}" if version is not None else "",
                "meta": {"status": "ok", "version": version},
            }
        )


def _execute_issue(run: Run) -> None:
    version, failed = None, False
    try:
        version = _call_runner(run, run.issue_date or "", run.trigger, run.emit)
    except AgentUnavailable as exc:
        log.warning("run %s: %s: %s", run.id, AGENT_MISSING, exc)
        failed = True
        run.emit(_error_event(exc))
    except Exception as exc:  # never let a runner take the server down
        log.exception("run %s failed", run.id)
        failed = True
        run.emit(_error_event(exc))
    finally:
        try:
            _close_issue(run.emit, run, 0, version, failed)
        finally:
            run.finish(version)


def _execute_backtest(run: Run, days: list[str]) -> None:
    published: list[str] = []
    failed: list[str] = []
    aborted = False
    try:
        for day in days:

            def emit(event: Any, _day: str = day) -> None:
                run.emit(event, issue_date=_day)

            version, bad, start = None, False, run.event_count()
            try:
                version = _call_runner(run, day, "issue", emit)
                published.append(day)
            except AgentUnavailable as exc:
                log.warning("backtest %s: %s: %s", run.id, AGENT_MISSING, exc)
                bad = aborted = True
                failed.append(day)
                emit(_error_event(exc))
            except Exception as exc:
                log.exception("backtest %s: issue %s failed", run.id, day)
                bad = True
                failed.append(day)
                emit(_error_event(exc))
            _close_issue(emit, run, start, version, bad)
            if aborted:
                break
    finally:
        body = f"Период {days[0]} … {days[-1]}"
        if failed:
            body += f" · ошибок: {len(failed)} ({', '.join(failed)})"
        if aborted:
            body += f" · остановлено: {AGENT_MISSING.lower()}"
        run.emit(
            {
                "type": "verdict",
                "title": f"Готово: {plural_issues(len(published))}",
                "body": body,
                "meta": {
                    "status": "error" if aborted else ("warn" if failed else "ok"),
                    "from": days[0],
                    "to": days[-1],
                    "published": published,
                    "failed": failed,
                },
            }
        )
        run.finish(None)


# --- SSE ----------------------------------------------------------------------------------


def sse_message(event: dict) -> str:
    return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def stream(run: Run, after: int = 0) -> AsyncIterator[str]:
    """Replay events after `after`, then follow the run; ends after its last event (a verdict)."""
    cursor = max(0, after)
    yield f"retry: {RETRY_MS}\n\n"
    while True:
        batch, done = run.snapshot(cursor)
        for event in batch:
            yield sse_message(event)
        cursor += len(batch)
        if done:
            return
        if not batch and not await run.wait(cursor, PING_INTERVAL_S):
            yield ": ping\n\n"
