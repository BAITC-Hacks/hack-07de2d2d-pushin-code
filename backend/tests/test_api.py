"""API contract tests (docs/CONTRACT.md §6) against a temporary WINDCAST_ROOT — no network."""

import asyncio
import csv
import io
import json
import sys
import threading
import time
import types
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from windcast import api, runs, timeline

ISSUE = "2026-02-13"


# --- synthetic files the agent would write ---------------------------------------------------


def _rows(version: int) -> list[dict]:
    rows = []
    targets = timeline.target_times_utc(ISSUE)
    for turbine in ("1", "2", "plant"):
        for h in range(1, 49):
            p50 = round(0.2 + 0.01 * h + (0.05 if version == 2 else 0.0), 4)
            rows.append(
                {
                    "h": h,
                    "target_time_local": timeline.iso_local(targets[h - 1]),
                    "turbine": turbine,
                    "p10": round(p50 - 0.1, 4),
                    "p50": p50,
                    "p90": round(p50 + 0.1, 4),
                    "wind_fc_ms": 6.4,
                    "temp_fc_c": -4.1,
                    "actual": None,
                }
            )
    return rows


def _record() -> dict:
    v2_rows = _rows(2)
    v2_rows[0]["temp_fc_c"] = float("nan")  # must come back as null, not break the JSON
    return {
        "issue_date": ISSUE,
        "issue_time_local": "2026-02-14T00:00+05:00",
        "issue_time_utc": "2026-02-13T19:00Z",
        "latest_version": 2,
        "mode": "deterministic",
        "recorded_at": "2026-09-23T15:40+05:00",
        "versions": {
            "1": {
                "version": 1,
                "created_at": "2026-09-23T15:39+05:00",
                "weather_runs": [
                    {
                        "hours": "1-48",
                        "model": "ecmwf_ifs025",
                        "init_utc": "2026-02-12T00:00Z",
                        "before_issue": True,
                    }
                ],
                "source": "cache",
                "change_note": None,
                "summary": "v1 на прогоне 12.02",
                "flags": [],
                "rows": _rows(1),
            },
            "2": {
                "version": 2,
                "created_at": "2026-09-23T15:40+05:00",
                "weather_runs": [
                    {  # no before_issue on purpose: the API fills it in
                        "hours": "1-24",
                        "model": "ecmwf_ifs025",
                        "init_utc": "2026-02-13T00:00Z",
                    },
                    {
                        "hours": "25-48",
                        "model": "ecmwf_ifs025",
                        "init_utc": "2026-02-12T00:00Z",
                        "before_issue": True,
                    },
                ],
                "source": "api",
                "change_note": "ветер +0,9 м/с → пик +6 п.п. против v1",
                "summary": "Пик 73 % номинала",
                "flags": [
                    {"kind": "ramp", "from_h": 15, "to_h": 17, "text": "спад"},
                    {"kind": "ice", "from_h": 1, "to_h": 5, "text": "обледенение"},
                    {"kind": "ramp", "from_h": 30, "to_h": 32, "text": "рост"},
                ],
                "rows": v2_rows,
            },
        },
    }


CSV_FILE = (
    "issue_date,issue_time_local,target_time_local,horizon_h,turbine,p10,p50,p90,"
    "wind_fc_ms,weather_init_max_utc,version\n"
    "2026-02-13,2026-02-14T00:00+05:00,2026-02-14T00:00+05:00,1,plant,"
    "0.1600,0.2600,0.3600,6.40,2026-02-13T00:00Z,2\n"
)

TRACE = [
    {"seq": 1, "type": "tool_call", "title": "fetch_weather", "meta": {"version": 1}},
    {"seq": 2, "type": "action", "title": "publish v1", "meta": {"version": 1}},
    {"seq": 3, "type": "tool_call", "title": "recalc", "meta": {"version": 2}},
    {"seq": 4, "type": "action", "title": "publish v2", "meta": {"version": 2}},
    {"seq": 5, "type": "verdict", "title": "Итог", "meta": {}},
]

LIVE_RECORD = {
    "issue_date": "live",
    "issue_time_local": "2026-09-23T15:00+05:00",
    "issue_time_utc": "2026-09-23T10:00Z",
    "latest_version": 1,
    "mode": "deterministic",
    "recorded_at": "2026-09-23T15:05+05:00",
    "versions": {
        "1": {
            "version": 1,
            "created_at": "2026-09-23T10:05:00Z",
            "weather_runs": [
                {
                    "hours": "1-48",
                    "model": "ecmwf_ifs025",
                    "init_utc": "2026-09-23T00:00Z",
                }
            ],
            "source": "api",
            "change_note": None,
            "summary": "live",
            "flags": [],
            "rows": [
                {
                    "h": 1,
                    "target_time_local": "2026-09-23T16:00+05:00",
                    "turbine": "plant",
                    "p10": 0.1,
                    "p50": 0.2,
                    "p90": 0.3,
                    "wind_fc_ms": 5.0,
                    "temp_fc_c": 12.0,
                }
            ],
        }
    },
}

METRICS = {
    "period": "2025-12-31 … 2026-01-29",
    "issues_count": 30,
    "coverage_p10_p90": 0.81,
    "methods": [
        {"key": "model", "label": "Модель", "nmae": 0.11, "nrmse": 0.16},
        {"key": "persistence", "label": "Персистентность", "nmae": 0.2, "nrmse": 0.27},
    ],
    "by_horizon": [{"h": 1, "model": 0.1, "power_curve": 0.14}],
    "series": [
        {
            "target_time_local": t,
            "turbine": turbine,
            "p10": 0.1,
            "p50": 0.2,
            "p90": 0.3,
            "actual": 0.25,
        }
        for t in (
            "2026-01-22T00:00+05:00",
            "2026-01-14T23:00+05:00",
            "2026-01-15T00:00+05:00",
            "2026-01-21T23:00+05:00",
        )
        for turbine in ("plant", "1")
    ],
}


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDCAST_ROOT", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    forecasts = tmp_path / "outputs" / "forecasts"
    traces = tmp_path / "outputs" / "traces"
    live = tmp_path / "outputs" / "live"
    for d in (forecasts, traces, live):
        d.mkdir(parents=True)
    (forecasts / f"{ISSUE}.json").write_text(
        json.dumps(_record(), ensure_ascii=False), encoding="utf-8"
    )
    (forecasts / f"{ISSUE}.csv").write_text(CSV_FILE, encoding="utf-8")
    (forecasts / "2026-02-14.json").write_text('{"issue_date": "2026-02-14", "vers')
    lines = [json.dumps(e, ensure_ascii=False) for e in TRACE]
    (traces / f"{ISSUE}.jsonl").write_text(
        "\n".join(lines) + '\n{"seq": 6, "type": "thou', encoding="utf-8"
    )
    (live / "latest.json").write_text(json.dumps(LIVE_RECORD), encoding="utf-8")
    (tmp_path / "outputs" / "metrics_jan.json").write_text(
        json.dumps(METRICS, ensure_ascii=False), encoding="utf-8"
    )
    runs.reset()
    yield tmp_path
    runs.reset()


@pytest.fixture()
def client(root):
    return TestClient(api.app)


def _error(resp, status):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"} and body["error"], body
    return body["error"]


# --- §6.7 health ----------------------------------------------------------------------------


def test_health_counts_records_and_reads_model_version(client, monkeypatch):
    fake = types.ModuleType("windcast.model")
    fake.MODEL_VERSION = "lgbm-q-test"
    monkeypatch.setitem(sys.modules, "windcast.model", fake)
    body = client.get("/health").json()
    assert body == {
        "ok": True,
        "mode": "deterministic",
        "model_version": "lgbm-q-test",
        "issues_ready": 1,  # 13.02 only; the half-written 14.02 does not count
    }


def test_health_stub_model_and_agent_mode(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "windcast.model", None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    body = client.get("/health").json()
    assert body["model_version"] == "stub" and body["mode"] == "agent"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-...")  # the .env.example placeholder
    assert client.get("/health").json()["mode"] == "deterministic"


# --- §6.1 calendar ----------------------------------------------------------------------------


def test_issues_default_range_and_entries(client):
    issues = client.get("/api/issues").json()
    assert len(issues) == 29
    assert issues[0]["issue_date"] == "2026-01-31"
    assert issues[-1]["issue_date"] == "2026-02-28"
    by_date = {i["issue_date"]: i for i in issues}
    published = by_date[ISSUE]
    plant_p50 = [r["p50"] for r in _rows(2) if r["turbine"] == "plant"]
    assert published == {
        "issue_date": ISSUE,
        "issue_time_local": "2026-02-14T00:00+05:00",
        "status": "published",
        "version": 2,
        "versions": [1, 2],
        "mean_p50": round(sum(plant_p50) / 48, 4),
        "peak_p50": max(plant_p50),
        "flags": {"ramp": 2, "ice": 1, "wind_gt20": 0, "models_diverge": 0},
        "source": "api",
    }
    for day in ("2026-02-12", "2026-02-14"):  # none / half-written
        missing = by_date[day]
        assert missing["status"] == "missing"
        assert missing["version"] is None and missing["mean_p50"] is None
        assert missing["peak_p50"] is None and missing["source"] is None
        assert missing["versions"] == []
    assert by_date["2026-02-12"]["issue_time_local"] == "2026-02-13T00:00+05:00"


def test_issues_range_and_bad_params(client):
    issues = client.get("/api/issues?from=2026-02-12&to=2026-02-14").json()
    assert [i["issue_date"] for i in issues] == ["2026-02-12", ISSUE, "2026-02-14"]
    assert "ГГГГ-ММ-ДД" in _error(client.get("/api/issues?from=13.02.2026"), 400)
    _error(client.get("/api/issues?from=2026-02-14&to=2026-02-12"), 400)
    _error(client.get("/api/issues?from=2020-01-01&to=2026-02-12"), 400)


# --- §6.2 issue -------------------------------------------------------------------------------


def test_forecast_latest_shape(client):
    body = client.get(f"/api/forecasts/{ISSUE}").json()
    for key in (
        "issue_date",
        "issue_time_local",
        "issue_time_utc",
        "version",
        "versions",
        "weather_runs",
        "change_note",
        "summary",
        "flags",
        "rows",
    ):
        assert key in body
    assert body["issue_date"] == ISSUE and body["issue_time_utc"] == "2026-02-13T19:00Z"
    assert body["version"] == 2 and body["versions"] == [1, 2]
    assert body["change_note"].startswith("ветер")
    assert [w["before_issue"] for w in body["weather_runs"]] == [True, True]
    assert len(body["flags"]) == 3 and body["flags"][0]["kind"] == "ramp"
    assert len(body["rows"]) == 144
    first = body["rows"][0]
    assert set(first) >= {
        "h",
        "target_time_local",
        "turbine",
        "p10",
        "p50",
        "p90",
        "wind_fc_ms",
        "temp_fc_c",
        "actual",
    }
    assert first["temp_fc_c"] is None  # NaN in the file -> null
    assert first["actual"] is None


def test_forecast_filters(client):
    plant = client.get(f"/api/forecasts/{ISSUE}?turbine=plant").json()["rows"]
    assert len(plant) == 48 and {r["turbine"] for r in plant} == {"plant"}
    body = client.get(f"/api/forecasts/{ISSUE}?version=1&turbine=1").json()
    assert body["version"] == 1 and body["versions"] == [1, 2]
    assert body["change_note"] is None and body["source"] == "cache"
    assert len(body["rows"]) == 48 and {r["turbine"] for r in body["rows"]} == {"1"}
    assert body["rows"][0]["p50"] == pytest.approx(0.21)


def test_forecast_errors(client):
    assert "3" in _error(client.get(f"/api/forecasts/{ISSUE}?version=3"), 404)
    _error(client.get(f"/api/forecasts/{ISSUE}?version=abc"), 400)
    _error(client.get(f"/api/forecasts/{ISSUE}?turbine=3"), 400)
    _error(client.get("/api/forecasts/2026-02-12"), 404)
    _error(client.get("/api/forecasts/2026-02-14"), 404)  # half-written record
    assert "ГГГГ-ММ-ДД" in _error(client.get("/api/forecasts/13-02-2026"), 400)


def _csv_rows(text):
    return list(csv.reader(io.StringIO(text)))


def test_csv_latest_is_the_file(client):
    resp = client.get(f"/api/forecasts/{ISSUE}.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.text == CSV_FILE


def test_csv_older_version_is_built_from_record(client):
    resp = client.get(f"/api/forecasts/{ISSUE}.csv?version=1")
    assert resp.status_code == 200
    rows = _csv_rows(resp.text)
    assert tuple(rows[0]) == api.CSV_COLUMNS
    assert len(rows) == 1 + 144
    first = dict(zip(rows[0], rows[1], strict=True))
    assert first["issue_date"] == ISSUE
    assert first["issue_time_local"] == "2026-02-14T00:00+05:00"
    assert first["target_time_local"] == "2026-02-14T00:00+05:00"
    assert first["horizon_h"] == "1" and first["turbine"] == "1"
    assert first["p50"] == "0.2100" and first["wind_fc_ms"] == "6.40"
    assert first["weather_init_max_utc"] == "2026-02-12T00:00Z"
    assert first["version"] == "1"
    assert "v1" in resp.headers["content-disposition"]


def test_csv_latest_built_when_file_missing(client, root):
    (root / "outputs" / "forecasts" / f"{ISSUE}.csv").unlink()
    rows = _csv_rows(client.get(f"/api/forecasts/{ISSUE}.csv").text)
    by_h = {int(r[3]): r for r in rows[1:] if r[4] == "plant"}
    assert by_h[1][9] == "2026-02-13T00:00Z"  # hours 1-24: the fresher run
    assert by_h[25][9] == "2026-02-12T00:00Z"  # hours 25-48
    assert {r[10] for r in rows[1:]} == {"2"}


def test_csv_errors_and_february(client, root):
    _error(client.get("/api/forecasts/2026-02-12.csv"), 404)
    _error(client.get(f"/api/forecasts/{ISSUE}.csv?version=5"), 404)
    _error(client.get("/api/forecasts/bad.csv"), 400)
    _error(client.get("/api/forecasts/february.csv"), 404)
    (root / "outputs" / "forecast_feb2026.csv").write_text(CSV_FILE, encoding="utf-8")
    resp = client.get("/api/forecasts/february.csv")
    assert resp.status_code == 200 and resp.text == CSV_FILE


# --- §6.3 traces ------------------------------------------------------------------------------


def test_trace_latest(client):
    body = client.get(f"/api/traces/{ISSUE}").json()
    assert body["issue_date"] == ISSUE and body["version"] == 2
    assert body["recorded_at"] == "2026-09-23T15:40+05:00"
    assert [e["seq"] for e in body["events"]] == [1, 2, 3, 4, 5]  # half line skipped


def test_trace_version_and_errors(client, root):
    body = client.get(f"/api/traces/{ISSUE}?version=1").json()
    assert body["version"] == 1 and [e["seq"] for e in body["events"]] == [1, 2, 5]
    _error(client.get(f"/api/traces/{ISSUE}?version=3"), 404)
    _error(client.get("/api/traces/2026-02-12"), 404)
    _error(client.get("/api/traces/2026-13-45"), 400)
    # a trace without a record: version from the events, recorded_at from the file time
    (root / "outputs" / "forecasts" / f"{ISSUE}.json").unlink()
    body = client.get(f"/api/traces/{ISSUE}").json()
    assert body["version"] == 2 and body["recorded_at"].endswith("+05:00")


# --- §6.5 live ------------------------------------------------------------------------------


def test_live_status(client, root, monkeypatch):
    now = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(api, "_now", lambda: now)
    body = client.get("/api/live/status").json()
    assert body == {
        "now_local": "2026-09-23T13:00+05:00",
        "latest_run_utc": "2026-09-23T00:00Z",
        "next_run_utc": "2026-09-23T06:00Z",
        "next_run_available_local": "2026-09-23T18:00+05:00",
        "current": {
            "version": 1,
            "issued_at_local": "2026-09-23T15:05+05:00",
            "weather_run_utc": "2026-09-23T00:00Z",
        },
    }
    (root / "outputs" / "live" / "latest.json").unlink()
    assert client.get("/api/live/status").json()["current"] is None


def test_live_forecast(client, root):
    body = client.get("/api/forecasts/live").json()
    assert body["version"] == 1 and body["versions"] == [1]
    assert body["issue_time_local"] == "2026-09-23T15:00+05:00"
    assert body["weather_runs"][0]["before_issue"] is True
    assert body["rows"][0]["actual"] is None
    assert client.get("/api/forecasts/live.csv").status_code == 200
    (root / "outputs" / "live" / "latest.json").unlink()
    _error(client.get("/api/forecasts/live"), 404)


# --- §6.6 metrics -----------------------------------------------------------------------------


def test_metrics(client):
    body = client.get("/api/metrics?from=2025-12-31&to=2026-01-29").json()
    assert "series" not in body
    assert body["issues_count"] == 30 and body["methods"][0]["key"] == "model"
    _error(client.get("/api/metrics?from=nope"), 400)


def test_metrics_series(client):
    rows = client.get("/api/metrics/series?from=2026-01-15&to=2026-01-21").json()
    assert [r["target_time_local"] for r in rows] == [
        "2026-01-15T00:00+05:00",
        "2026-01-21T23:00+05:00",
    ]
    assert {r["turbine"] for r in rows} == {"plant"}
    assert set(rows[0]) >= {"target_time_local", "p10", "p50", "p90", "actual"}
    t1 = client.get("/api/metrics/series?turbine=1").json()
    assert len(t1) == 4 and {r["turbine"] for r in t1} == {"1"}
    assert len(client.get("/api/metrics/series?turbine=all").json()) == 8
    _error(client.get("/api/metrics/series?turbine=x"), 400)
    _error(client.get("/api/metrics/series?from=2026-01-21&to=2026-01-15"), 400)


def test_metrics_missing(client, root):
    (root / "outputs" / "metrics_jan.json").unlink()
    assert _error(client.get("/api/metrics"), 404) == "Метрики ещё не посчитаны"
    _error(client.get("/api/metrics/series"), 404)


# --- errors -----------------------------------------------------------------------------------


def test_unknown_route_and_method_are_json(client):
    assert "/api/nope" in _error(client.get("/api/nope"), 404)
    _error(client.post("/api/issues"), 405)


def test_validation_errors_map_to_400():
    exc = RequestValidationError(
        [{"type": "missing", "loc": ("query", "x"), "msg": "Field required"}]
    )
    resp = asyncio.run(api._validation_error(None, exc))
    assert resp.status_code == 400
    assert json.loads(resp.body) == {
        "error": "Неверный запрос — query.x: обязательное поле"
    }


def test_unexpected_error_is_500_json(root, monkeypatch):
    def boom(issue):
        raise RuntimeError("диск")

    monkeypatch.setattr(api, "_load_record", boom)
    client = TestClient(api.app, raise_server_exceptions=False)
    resp = client.get(f"/api/forecasts/{ISSUE}")
    assert resp.status_code == 500
    assert resp.json()["error"].startswith("Внутренняя ошибка")


# --- §6.3 runs + SSE --------------------------------------------------------------------------


def _sse(text):
    return [
        json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")
    ]


def _wait_done(client, run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()
        if body["done"]:
            return body
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


def _fake_runner(calls, pause=0.0, gate=None):
    def runner(issue_date, *, mode, trigger, emit):
        calls.append((issue_date, mode, trigger))
        emit(
            {
                "type": "thought",
                "title": f"Начинаю {issue_date}",
                "meta": {"stage": "weather"},
            }
        )
        if gate is not None:
            assert gate.wait(5)
        time.sleep(pause)
        emit(
            {
                "type": "tool_call",
                "title": "fetch_weather",
                "meta": {"tool": "fetch_weather", "stage": "weather", "source": "api"},
            }
        )
        emit({"type": "action", "title": "publish_forecast", "meta": {"version": 2}})
        emit({"type": "verdict", "title": "Опубликована v2", "meta": {"status": "ok"}})
        return {"issue_date": issue_date, "version": 2}

    return runner


def test_run_streams_events_in_order_and_closes(client, monkeypatch):
    calls = []
    monkeypatch.setattr(runs, "RUNNER", _fake_runner(calls, pause=0.3))
    resp = client.post("/api/runs", json={"issue_date": ISSUE})
    assert resp.status_code == 200
    run_id = resp.json()["id"]
    assert run_id.startswith("r_")

    stream = client.get(f"/api/runs/{run_id}/events")  # returns once the stream closed
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "no-cache" in stream.headers["cache-control"]
    assert stream.headers["x-accel-buffering"] == "no"
    events = _sse(stream.text)
    assert [e["type"] for e in events] == ["thought", "tool_call", "action", "verdict"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert all(e["meta"]["issue_date"] == ISSUE and e["ts"] for e in events)
    assert events[1]["meta"]["source"] == "api"
    assert calls == [(ISSUE, "deterministic", "issue")]

    body = client.get(f"/api/runs/{run_id}").json()
    assert {
        k: body[k] for k in ("id", "issue_date", "trigger", "mode", "done", "version")
    } == {
        "id": run_id,
        "issue_date": ISSUE,
        "trigger": "issue",
        "mode": "deterministic",
        "done": True,
        "version": 2,
    }
    assert [e["seq"] for e in body["events"]] == [1, 2, 3, 4]

    # replay after the end, and resume from Last-Event-ID
    assert [e["seq"] for e in _sse(client.get(f"/api/runs/{run_id}/events").text)] == [
        1,
        2,
        3,
        4,
    ]
    resumed = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "2"})
    assert [e["seq"] for e in _sse(resumed.text)] == [3, 4]
    finished = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "4"})
    assert finished.status_code == 204


def test_run_sends_pings_while_idle(client, monkeypatch):
    monkeypatch.setattr(runs, "PING_INTERVAL_S", 0.05)
    monkeypatch.setattr(runs, "RUNNER", _fake_runner([], pause=0.4))
    run_id = client.post("/api/runs", json={"issue_date": ISSUE}).json()["id"]
    text = client.get(f"/api/runs/{run_id}/events").text
    assert ": ping" in text
    assert _sse(text)[-1]["type"] == "verdict"


def test_run_modes_triggers_and_live(client, monkeypatch):
    calls = []
    monkeypatch.setattr(runs, "RUNNER", _fake_runner(calls))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    rid = client.post(
        "/api/runs", json={"issue_date": ISSUE, "trigger": "new_weather_run"}
    ).json()["id"]
    _wait_done(client, rid)
    rid = client.post(
        "/api/runs", json={"issue_date": "live", "mode": "deterministic"}
    ).json()["id"]
    assert _wait_done(client, rid)["issue_date"] == "live"
    monkeypatch.delenv("OPENAI_API_KEY")
    rid = client.post(
        "/api/runs", json={"issue_date": "2025-12-31", "mode": "agent"}
    ).json()["id"]
    _wait_done(client, rid)
    assert calls == [
        (ISSUE, "agent", "new_weather_run"),
        ("live", "deterministic", "issue"),
        ("2025-12-31", "deterministic", "issue"),  # no key: agent mode is not possible
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"issue_date": "13.02.2026"},
        {"issue_date": "2026-03-01"},
        {"issue_date": "2025-12-30"},
        {"issue_date": ISSUE, "mode": "magic"},
        {"issue_date": ISSUE, "trigger": "now"},
        {},
    ],
)
def test_run_validation(client, payload):
    _error(client.post("/api/runs", json=payload), 400)


def test_run_bad_bodies(client):
    _error(client.post("/api/runs", content=b"{not json"), 400)
    _error(client.post("/api/runs", json=[ISSUE]), 400)


def test_unknown_run_is_404(client):
    _error(client.get("/api/runs/r_missing"), 404)
    _error(client.get("/api/runs/r_missing/events"), 404)


def test_runner_failure_becomes_error_and_verdict(client, monkeypatch):
    def broken(issue_date, *, mode, trigger, emit):
        emit({"type": "thought", "title": "Начинаю"})
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(runs, "RUNNER", broken)
    run_id = client.post("/api/runs", json={"issue_date": ISSUE}).json()["id"]
    events = _sse(client.get(f"/api/runs/{run_id}/events").text)
    assert [e["type"] for e in events] == ["thought", "error", "verdict"]
    assert "сеть недоступна" in events[1]["body"]
    body = _wait_done(client, run_id)
    assert body["version"] is None
    assert client.get("/api/live/status").status_code == 200  # server still fine


def test_default_runner_without_agent_module(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "windcast.agent", None)
    run_id = client.post("/api/runs", json={"issue_date": ISSUE}).json()["id"]
    events = _sse(client.get(f"/api/runs/{run_id}/events").text)
    assert [e["type"] for e in events] == ["error", "verdict"]
    assert events[0]["title"] == "Агент ещё не подключён"
    assert _wait_done(client, run_id)["done"] is True


def test_runner_without_verdict_gets_one(client, monkeypatch):
    def quiet(issue_date, *, mode, trigger, emit):
        emit({"type": "action", "title": "publish_forecast"})
        return {"version": 1}

    monkeypatch.setattr(runs, "RUNNER", quiet)
    run_id = client.post("/api/runs", json={"issue_date": ISSUE}).json()["id"]
    events = _sse(client.get(f"/api/runs/{run_id}/events").text)
    assert [e["type"] for e in events] == ["action", "verdict"]


def test_running_status_dedupe_and_issue_lock(client, monkeypatch):
    gate = threading.Event()
    calls = []
    monkeypatch.setattr(runs, "RUNNER", _fake_runner(calls, gate=gate))
    try:
        first = client.post("/api/runs", json={"issue_date": ISSUE}).json()["id"]
        again = client.post("/api/runs", json={"issue_date": ISSUE}).json()["id"]
        assert again == first  # same issue + trigger + mode while running: same run
        deadline = time.monotonic() + 5
        while not client.get(f"/api/runs/{first}").json()["events"]:
            assert time.monotonic() < deadline  # then the first run holds the lock
            time.sleep(0.02)
        other = client.post(
            "/api/runs", json={"issue_date": ISSUE, "trigger": "new_weather_run"}
        ).json()["id"]
        assert other != first
        issues = client.get("/api/issues?from=2026-02-12&to=2026-02-13").json()
        assert [i["status"] for i in issues] == ["missing", "running"]
        assert issues[1]["version"] == 2  # the published data stays visible
        deadline = time.monotonic() + 5
        while not client.get(f"/api/runs/{other}").json()["events"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        waiting = client.get(f"/api/runs/{other}").json()["events"][0]
        assert waiting["type"] == "thought" and "Жду" in waiting["title"]
    finally:
        gate.set()
    _wait_done(client, first)
    _wait_done(client, other)
    assert [c[2] for c in calls] == ["issue", "new_weather_run"]  # one after the other
    statuses = {i["status"] for i in client.get("/api/issues").json()}
    assert "running" not in statuses


# --- §6.4 backtest ----------------------------------------------------------------------------


def test_backtest_two_dates(client, monkeypatch):
    calls = []
    monkeypatch.setattr(runs, "RUNNER", _fake_runner(calls, pause=0.05))
    resp = client.post(
        "/api/backtest",
        json={"from": "2026-02-12", "to": ISSUE, "mode": "deterministic"},
    )
    assert resp.status_code == 200
    run_id = resp.json()["id"]
    events = _sse(client.get(f"/api/runs/{run_id}/events").text)
    assert calls == [
        ("2026-02-12", "deterministic", "issue"),
        (ISSUE, "deterministic", "issue"),
    ]
    assert [e["seq"] for e in events] == list(range(1, 10))
    per_issue = events[:-1]
    assert [e["meta"]["issue_date"] for e in per_issue] == ["2026-02-12"] * 4 + [
        ISSUE
    ] * 4
    verdicts = [e for e in events if e["type"] == "verdict"]
    assert len(verdicts) == 3  # one per issue, then the final one
    assert events[-1]["type"] == "verdict"
    assert events[-1]["title"] == "Готово: 2 выпуска"
    body = _wait_done(client, run_id)
    assert body["issue_date"] is None and body["kind"] == "backtest"
    assert (body["from"], body["to"]) == ("2026-02-12", ISSUE)


def test_backtest_continues_after_a_failed_issue(client, monkeypatch):
    def flaky(issue_date, *, mode, trigger, emit):
        if issue_date == "2026-02-12":
            raise ValueError("нет погоды")
        emit({"type": "verdict", "title": "ok"})
        return {"version": 1}

    monkeypatch.setattr(runs, "RUNNER", flaky)
    run_id = client.post(
        "/api/backtest", json={"from": "2026-02-12", "to": ISSUE}
    ).json()["id"]
    events = _sse(client.get(f"/api/runs/{run_id}/events").text)
    assert [(e["type"], e["meta"].get("issue_date")) for e in events[:-1]] == [
        ("error", "2026-02-12"),
        ("verdict", "2026-02-12"),
        ("verdict", ISSUE),
    ]
    assert events[-1]["title"] == "Готово: 1 выпуск"
    assert events[-1]["meta"]["failed"] == ["2026-02-12"]


@pytest.mark.parametrize(
    "payload",
    [
        {"from": "2026-02-13", "to": "2026-02-12"},
        {"from": "2026-01-31", "to": "2026-03-01"},
        {"from": "2025-12-01", "to": "2026-01-02"},
        {"from": "31.01.2026", "to": "2026-02-01"},
        {"from": "2026-01-31", "to": "2026-02-01", "mode": "x"},
    ],
)
def test_backtest_validation(client, payload):
    _error(client.post("/api/backtest", json=payload), 400)


def test_backtest_stops_when_agent_is_missing(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "windcast.agent", None)
    run_id = client.post(
        "/api/backtest", json={"from": "2026-02-12", "to": ISSUE}
    ).json()["id"]
    events = _sse(client.get(f"/api/runs/{run_id}/events").text)
    assert [e["type"] for e in events] == ["error", "verdict", "verdict"]
    assert events[0]["title"] == "Агент ещё не подключён"
    assert events[-1]["title"] == "Готово: 0 выпусков"
    assert events[-1]["meta"]["status"] == "error"


def test_events_are_plain_json_in_schema_order(client, monkeypatch):
    np = pytest.importorskip("numpy")

    def runner(issue_date, *, mode, trigger, emit):
        emit(
            {
                "title": "числа",
                "extra": 1,
                "meta": {
                    "args": {
                        "n": np.int64(3),
                        "x": np.float64("nan"),
                        "when": datetime(2026, 2, 13, 19, tzinfo=timezone.utc),
                    }
                },
            }
        )
        emit({"type": "verdict", "title": "ok", "seq": 99})

    monkeypatch.setattr(runs, "RUNNER", runner)
    run_id = client.post("/api/runs", json={"issue_date": ISSUE}).json()["id"]
    events = _sse(client.get(f"/api/runs/{run_id}/events").text)
    assert list(events[0])[:6] == ["seq", "ts", "type", "title", "body", "meta"]
    assert events[0]["type"] == "thought" and events[0]["extra"] == 1
    assert events[0]["meta"]["args"] == {
        "n": 3,
        "x": None,
        "when": "2026-02-13T19:00:00+00:00",
    }
    assert [e["seq"] for e in events] == [1, 2]  # the stream numbers events itself
