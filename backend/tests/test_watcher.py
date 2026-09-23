"""Live watcher and journal (contract §6.5) — a fake RUNNER, a temporary WINDCAST_ROOT."""

import json
import sys
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from windcast import api, runs, watcher


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDCAST_ROOT", str(tmp_path))
    monkeypatch.setenv("LIVE_WATCH_MINUTES", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runs.reset()
    watcher.reset()
    yield tmp_path
    watcher.reset()
    runs.reset()


@pytest.fixture()
def client(root):
    return TestClient(api.app)


def _ev(type_, title, **meta):
    return {"type": type_, "title": title, "meta": meta}


# what the agent emits for each outcome (titles as in windcast.agent / windcast.tools)
PUBLISHED_V1 = [
    _ev("tool_call", "Беру погоду", tool="fetch_weather", stage="weather"),
    _ev("action", "Опубликована v1", tool="publish_forecast", version=1),
    _ev(
        "verdict",
        "Итог: опубликована v1 — новый прогон ещё не вышел",
        status="ok",
        version=1,
    ),
]
KEPT = [
    _ev(
        "thought",
        "Новый прогон ещё не вышел — v1 остаётся",
        stage="recalc",
        status="skip",
        decision="keep",
        by="rule",
        version=1,
    ),
    _ev(
        "verdict",
        "Итог: v1 остаётся — новый прогон ещё не вышел",
        status="skip",
        version=1,
    ),
]
REFUSED = [
    _ev("thought", "Сдвиг 1,1 м/с больше порога — пересчитываю", decision="recalc"),
    _ev(
        "tool_result",
        "Отказ: прогон 06 UTC вышел бы после момента выпуска",
        tool="recalc_forecast",
        status="skip",
        version=1,
    ),
    _ev("verdict", "Итог: отказ — v1 остаётся", status="skip", version=1),
]
PUBLISHED_V2 = [
    _ev("thought", "Сдвиг 1,1 м/с больше порога — пересчитываю", decision="recalc"),
    _ev("action", "Опубликована v2", tool="publish_forecast", version=2),
    _ev("verdict", "Итог: опубликована v2 на новом прогоне", status="ok", version=2),
]
CRASHED = [
    _ev("error", "Сбой агента", status="error"),
    _ev("verdict", "Итог: выпуск прерван ошибкой", status="error"),
]


def _runner(calls, events, version=1, gate=None, raises=None):
    def runner(issue_date, *, mode, trigger, emit, **kwargs):
        calls.append((issue_date, mode, trigger))
        if gate is not None:
            assert gate.wait(5)
        for event in events:
            emit(event)
        if raises is not None:
            raise raises
        return {"issue_date": issue_date, "version": version}

    return runner


def _done(run, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not run.done:
        assert time.monotonic() < deadline, f"run {run.id} did not finish"
        time.sleep(0.01)
    return run


def _journal(root):
    path = root / "outputs" / "live" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def _publish_live(root):
    live = root / "outputs" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "latest.json").write_text(
        json.dumps({"issue_date": "live", "latest_version": 1, "versions": {"1": {}}}),
        encoding="utf-8",
    )


# --- tick -------------------------------------------------------------------------------------


def test_tick_issues_first_then_checks_new_runs(root, monkeypatch):
    calls = []
    monkeypatch.setattr(runs, "RUNNER", _runner(calls, PUBLISHED_V1))
    assert watcher.status()["live_last_check_local"] is None
    first = _done(watcher.tick())
    assert first.initiator == "agent" and first.trigger == "issue"
    assert watcher.status()["live_last_check_local"].endswith("+05:00")
    _publish_live(root)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key")
    second = _done(watcher.tick())
    assert second.trigger == "new_weather_run"
    assert calls == [
        ("live", "deterministic", "issue"),
        ("live", "agent", "new_weather_run"),
    ]
    lines = _journal(root)
    assert [(e["initiator"], e["trigger"], e["run_id"]) for e in lines] == [
        ("agent", "issue", first.id),
        ("agent", "new_weather_run", second.id),
    ]
    assert set(lines[0]) == {
        "ts",
        "initiator",
        "trigger",
        "run_id",
        "outcome",
        "version",
        "title",
    }
    assert lines[0]["ts"].endswith("+05:00")


def test_tick_is_skipped_while_a_live_run_is_running(root, monkeypatch):
    gate = threading.Event()
    calls = []
    monkeypatch.setattr(runs, "RUNNER", _runner(calls, PUBLISHED_V1, gate=gate))
    try:
        busy = runs.start_issue("live", mode="deterministic", trigger="issue")
        assert watcher.tick() is None
        assert watcher.status()["live_last_check_local"] is not None
    finally:
        gate.set()
    _done(busy)
    assert len(calls) == 1
    assert [e["initiator"] for e in _journal(root)] == ["user"]


@pytest.mark.parametrize(
    ("events", "version", "raises", "outcome", "title"),
    [
        (
            PUBLISHED_V1,
            1,
            None,
            "published",
            "Опубликована v1 — новый прогон ещё не вышел",
        ),
        (KEPT, 1, None, "kept", "Новый прогон ещё не вышел — v1 остаётся"),
        (
            REFUSED,
            1,
            None,
            "refused",
            "Отказ: прогон 06 UTC вышел бы после момента выпуска",
        ),
        (PUBLISHED_V2, 2, None, "published", "Опубликована v2 на новом прогоне"),
        (CRASHED, None, RuntimeError("сеть"), "error", "Выпуск прерван ошибкой"),
    ],
)
def test_journal_outcomes(root, monkeypatch, events, version, raises, outcome, title):
    monkeypatch.setattr(
        runs, "RUNNER", _runner([], events, version=version, raises=raises)
    )
    run = _done(watcher.tick())
    (line,) = _journal(root)
    assert line["outcome"] == outcome and line["title"] == title
    assert line["version"] == version and line["run_id"] == run.id


def test_journal_when_the_agent_is_not_connected(root, monkeypatch):
    monkeypatch.setitem(sys.modules, "windcast.agent", None)
    _done(watcher.tick())
    (line,) = _journal(root)
    assert line["outcome"] == "error" and line["title"] == "Агент ещё не подключён"


def test_user_live_runs_are_journaled_and_dated_runs_are_not(client, root, monkeypatch):
    monkeypatch.setattr(runs, "RUNNER", _runner([], KEPT))
    rid = client.post(
        "/api/runs", json={"issue_date": "live", "trigger": "new_weather_run"}
    ).json()["id"]
    _done(runs.get(rid))
    dated = client.post("/api/runs", json={"issue_date": "2026-02-13"}).json()["id"]
    _done(runs.get(dated))
    lines = _journal(root)
    assert [(e["initiator"], e["run_id"], e["outcome"]) for e in lines] == [
        ("user", rid, "kept")
    ]
    assert client.get(f"/api/runs/{rid}").json()["initiator"] == "user"


# --- endpoint and /health --------------------------------------------------------------------


def test_journal_endpoint_newest_first(client, root, monkeypatch):
    assert client.get("/api/live/journal").json() == []
    monkeypatch.setattr(runs, "RUNNER", _runner([], PUBLISHED_V1))
    ids = [_done(watcher.tick()).id for _ in range(3)]
    body = client.get("/api/live/journal").json()
    assert [e["run_id"] for e in body] == ids[::-1]
    assert [e["run_id"] for e in client.get("/api/live/journal?limit=1").json()] == [
        ids[-1]
    ]
    with (root / "outputs" / "live" / "journal.jsonl").open("a") as handle:
        handle.write('{"ts": "half')
    assert len(client.get("/api/live/journal").json()) == 3  # a torn line is skipped
    for bad in ("0", "201", "abc", "-1"):
        resp = client.get(f"/api/live/journal?limit={bad}")
        assert resp.status_code == 400 and "limit" in resp.json()["error"]


@pytest.mark.parametrize(
    ("value", "minutes"),
    [("0", 0), ("15", 15), ("abc", 0), ("-5", 0), ("0.5", 0.5), (None, 30)],
)
def test_health_reports_the_watch_interval(client, monkeypatch, value, minutes):
    monkeypatch.setitem(sys.modules, "windcast.model", None)
    if value is None:
        monkeypatch.delenv("LIVE_WATCH_MINUTES", raising=False)
    else:
        monkeypatch.setenv("LIVE_WATCH_MINUTES", value)
    body = client.get("/health").json()
    assert body["live_watch_minutes"] == minutes
    assert body["live_last_check_local"] is None


# --- the thread and the app lifespan ---------------------------------------------------------


def test_lifespan_leaves_the_watcher_off_at_zero(root):
    with TestClient(api.app) as c:
        assert c.get("/health").json()["live_watch_minutes"] == 0
        assert not watcher.running()


def test_lifespan_runs_the_watcher_loop(root, monkeypatch):
    monkeypatch.setenv("LIVE_WATCH_MINUTES", "0.002")  # every ~0.12 s
    monkeypatch.setattr(watcher, "FIRST_DELAY_S", 0.05)
    monkeypatch.setattr(runs, "RUNNER", _runner([], PUBLISHED_V1))
    journal = root / "outputs" / "live" / "journal.jsonl"
    with TestClient(api.app) as c:
        assert watcher.running()
        deadline = time.monotonic() + 5
        while len(c.get("/api/live/journal").json()) < 2:
            assert time.monotonic() < deadline, "the watcher did not tick twice"
            time.sleep(0.02)
    assert not watcher.running()  # stopped on shutdown
    assert {e["initiator"] for e in _journal(root)} == {"agent"}
    assert journal.exists()


def test_a_failing_tick_does_not_stop_the_loop(root, monkeypatch):
    ticks = []

    def flaky_tick():
        ticks.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(watcher, "tick", flaky_tick)
    assert watcher.start(minutes=0.001, first_delay=0.01)
    deadline = time.monotonic() + 5
    while len(ticks) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert watcher.running()
    watcher.stop()
    assert not watcher.running()
