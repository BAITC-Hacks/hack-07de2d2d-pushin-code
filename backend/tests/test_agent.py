import csv
import json
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pd = pytest.importorskip("pandas")

from windcast import _stubs, agent, backtest, ports, store, timeline, tools

CSV_HEADER = (
    "issue_date,issue_time_local,target_time_local,horizon_h,turbine,"
    "p10,p50,p90,wind_fc_ms,weather_init_max_utc,version"
)
META_KEYS = {"tool", "stage", "status", "issue_date", "version", "source"}


@pytest.fixture(autouse=True)
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDCAST_ROOT", str(tmp_path))
    monkeypatch.setenv("WINDCAST_FORCE_STUBS", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    return tmp_path


def _stub_facts(day: str) -> tuple[float, int]:
    """Mean |Δwind| h 1–24 and new weather risks between the stub runs of a day."""
    previous = tools.normalize_weather(_stubs.fetch_weather(day, "previous"))
    latest = tools.normalize_weather(_stubs.fetch_weather(day, "latest"))
    delta = latest["hourly"]["wind_100m_ms"] - previous["hourly"]["wind_100m_ms"]
    fresh = tools.new_flags(tools.weather_flags(latest), tools.weather_flags(previous))
    return float(delta[:24].abs().mean()), len(fresh)


def _pick_day(recalc: bool) -> str:
    for day in timeline.issue_dates():
        shift, fresh = _stub_facts(day.isoformat())
        if recalc and shift > tools.RECALC_SHIFT_MS and not fresh:
            return day.isoformat()
        if not recalc and shift <= tools.RECALC_SHIFT_MS and not fresh:
            return day.isoformat()
    raise AssertionError("stubs give no such day in February")


RECALC_DAY = _pick_day(recalc=True)
KEEP_DAY = _pick_day(recalc=False)


def _assert_events(events: list[dict], issue_date: str) -> None:
    assert events, "no events"
    for seq, event in enumerate(events, start=1):
        assert set(event) == {"seq", "ts", "type", "title", "body", "meta"}
        assert event["seq"] == seq
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+05:00", event["ts"])
        assert event["type"] in tools.EVENT_TYPES
        assert isinstance(event["title"], str) and event["title"]
        assert isinstance(event["body"], str)
        meta = event["meta"]
        assert META_KEYS <= set(meta)
        assert meta["stage"] in (None, *tools.STAGES)
        assert meta["status"] in tools.STATUSES
        assert meta["issue_date"] == issue_date
    assert events[-1]["type"] == "verdict"


def _tool_calls(events: list[dict]) -> list[str]:
    return [e["meta"]["tool"] for e in events if e["type"] == "tool_call"]


def _decisions(events: list[dict]) -> list[dict]:
    return [e for e in events if e["meta"].get("decision")]


def _csv_rows(path) -> tuple[str, list[dict]]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return lines[0], list(csv.DictReader(lines))


def _utc(text: str) -> datetime:
    return datetime.strptime(text + "+0000", "%Y-%m-%dT%H:%MZ%z")


# ---------- deterministic mode ----------
def test_deterministic_issue_publishes_contract_outputs():
    events: list[dict] = []
    result = agent.run_issue("2026-02-13", mode="deterministic", emit=events.append)
    assert set(result) == {
        "issue_date",
        "version",
        "record_path",
        "csv_path",
        "trace_path",
    }
    assert result["issue_date"] == "2026-02-13" and result["version"] in (1, 2)
    for key in ("record_path", "csv_path", "trace_path"):
        assert Path(result[key]).read_text(encoding="utf-8")

    header, rows = _csv_rows(result["csv_path"])
    assert header == CSV_HEADER
    assert len(rows) == 144
    assert {(int(r["horizon_h"]), r["turbine"]) for r in rows} == {
        (h, t) for h in range(1, 49) for t in ("1", "2", "plant")
    }
    issue = timeline.issue_time_utc("2026-02-13")
    for r in rows:
        p10, p50, p90 = (float(r[q]) for q in ("p10", "p50", "p90"))
        assert 0.0 <= p10 <= p50 <= p90 <= 1.0
        init = _utc(r["weather_init_max_utc"])
        assert init <= datetime(2026, 2, 13, 19, tzinfo=timezone.utc)
        assert timeline.published_before_issue(init, "2026-02-13")
        assert r["issue_time_local"] == "2026-02-14T00:00+05:00"
        assert int(r["version"]) == result["version"]
    first = next(r for r in rows if r["horizon_h"] == "1")
    assert first["target_time_local"] == "2026-02-14T00:00+05:00"

    record = store.load_record("2026-02-13")
    assert record["latest_version"] == result["version"]
    assert record["mode"] == "deterministic"
    assert record["issue_time_utc"] == timeline.iso_utc(issue)
    for entry in record["versions"].values():
        assert all(run["before_issue"] for run in entry["weather_runs"])
        assert entry["summary"].startswith("Пик ")
        assert {f["kind"] for f in entry["flags"]} <= set(tools.FLAG_KINDS)

    trace = store.load_trace("2026-02-13")
    assert trace == events
    _assert_events(trace, "2026-02-13")
    assert any(e["type"] == "action" for e in trace)
    stages = [e["meta"]["stage"] for e in trace if e["type"] == "tool_result"]
    assert stages[:4] == ["weather", "prep", "forecast", "analysis"]
    assert _tool_calls(trace)[:5] == [
        "fetch_weather",
        "check_data",
        "run_model",
        "analyze_forecast",
        "publish_forecast",
    ]
    assert len(_tool_calls(trace)) <= agent.MAX_TOOL_CALLS


def test_big_shift_recalculates_to_v2_with_change_note():
    result = agent.run_issue(RECALC_DAY, mode="deterministic")
    assert result["version"] == 2
    record = store.load_record(RECALC_DAY)
    assert sorted(record["versions"]) == ["1", "2"]
    v1, v2 = record["versions"]["1"], record["versions"]["2"]
    assert v1["change_note"] is None
    assert re.match(r"ветер [+−]\d,\d м/с → .* против v1$", v2["change_note"])
    assert v2["rows"] != v1["rows"]
    later = [run["init_utc"] for run in v2["weather_runs"]]
    assert later > [run["init_utc"] for run in v1["weather_runs"]]
    trace = store.load_trace(RECALC_DAY)
    decision = _decisions(trace)
    assert len(decision) == 1 and decision[0]["meta"]["decision"] == "recalc"
    assert decision[0]["meta"]["status"] == "ok"
    assert "больше порога — пересчитываю" in decision[0]["title"]
    assert _tool_calls(trace)[5:] == [
        "fetch_weather",
        "recalc_forecast",
        "publish_forecast",
    ]
    assert len(_tool_calls(trace)) == agent.MAX_TOOL_CALLS
    assert sum(e["type"] == "action" for e in trace) == 2
    assert trace[-1]["meta"]["version"] == 2


def test_small_shift_keeps_v1_with_skip_decision():
    result = agent.run_issue(KEEP_DAY, mode="deterministic")
    assert result["version"] == 1
    record = store.load_record(KEEP_DAY)
    assert list(record["versions"]) == ["1"]
    trace = store.load_trace(KEEP_DAY)
    decision = _decisions(trace)
    assert len(decision) == 1 and decision[0]["meta"]["decision"] == "keep"
    assert decision[0]["meta"]["status"] == "skip"
    assert decision[0]["meta"]["stage"] == "recalc"
    assert "новых рисков нет — v1 остаётся" in decision[0]["title"]
    assert "recalc_forecast" not in _tool_calls(trace)
    assert trace[-1]["type"] == "verdict" and "пересчёт не нужен" in trace[-1]["title"]


def test_new_weather_run_refuses_on_archive_day_already_on_latest_run():
    agent.run_issue(RECALC_DAY, mode="deterministic")
    before = store.load_record(RECALC_DAY)
    issue_trace = store.load_trace(RECALC_DAY)
    events: list[dict] = []
    result = agent.run_issue(
        RECALC_DAY,
        mode="deterministic",
        trigger="new_weather_run",
        emit=events.append,
    )
    assert result["version"] == 2
    after = store.load_record(RECALC_DAY)
    assert sorted(after["versions"]) == ["1", "2"]
    assert after["versions"] == before["versions"]
    _assert_events(events, RECALC_DAY)
    decision = _decisions(events)
    assert len(decision) == 1 and decision[0]["meta"]["status"] == "skip"
    assert decision[0]["title"].startswith(
        "Более свежего прогона до момента выпуска нет"
    )
    assert events[-1]["meta"]["status"] == "skip"
    assert "v2 остаётся" in events[-1]["title"]
    assert store.load_trace(RECALC_DAY) == issue_trace  # traces keep the full issue


def test_recalc_guardrail_refuses_whatever_the_caller_wants():
    agent.run_issue(RECALC_DAY, mode="deterministic")
    record = store.load_record(RECALC_DAY)
    ctx = tools.RunContext(
        issue_date=RECALC_DAY,
        trigger="new_weather_run",
        issue_time_utc=tools.to_utc(record["issue_time_utc"]),
        record=record,
    )
    ctx.current = tools.version_from_record(record, 2)
    ctx.versions[2] = ctx.current
    events: list[dict] = []
    registry = tools.ToolRegistry(ctx, tools.Tracer(RECALC_DAY, events.append))
    result = registry.call("recalc_forecast", {"reason": "хочу"}, origin="llm")
    assert result["ok"] is False and result["status"] == "refused"
    assert "более свежего прогона до момента выпуска нет" in result["error"]
    assert "опубликован ≈" in result["error"]
    assert events[-1]["meta"]["status"] == "skip"
    assert sorted(store.load_record(RECALC_DAY)["versions"]) == ["1", "2"]


def test_unpublished_run_is_future_data_even_if_started_before_t():
    ctx = tools.RunContext(
        issue_date="2026-02-13",
        issue_time_utc=tools.to_utc(timeline.issue_time_utc("2026-02-13")),
    )
    assert ctx.published(datetime(2026, 2, 13, 6, tzinfo=timezone.utc))
    assert not ctx.published(datetime(2026, 2, 13, 12, tzinfo=timezone.utc))
    assert not ctx.published(datetime(2026, 2, 13, 18, tzinfo=timezone.utc))
    text = tools.late_run_text(
        datetime(2026, 2, 13, 18, tzinfo=timezone.utc), ctx.limit_time()
    )
    assert text.startswith(
        "прогон стартовал в 13.02 18:00 UTC, опубликован ≈ 14.02 02:00 UTC"
    )


def test_check_data_rejects_a_run_published_after_t(monkeypatch):
    real = _stubs.fetch_weather

    def late(issue_date, run="latest"):
        weather = real(issue_date, run)
        weather["hourly"].loc[0, "init_time_utc"] = pd.Timestamp("2026-02-13T18:00Z")
        return weather

    monkeypatch.setattr(_stubs, "fetch_weather", late)
    events: list[dict] = []
    result = agent.run_issue("2026-02-13", mode="deterministic", emit=events.append)
    assert result["version"] is None
    assert store.load_record("2026-02-13") is None
    check = next(
        e
        for e in events
        if e["type"] == "tool_result" and e["meta"]["tool"] == "check_data"
    )
    assert check["meta"]["status"] == "error"
    assert "опубликован ≈ 14.02 02:00 UTC — позже момента выпуска" in check["body"]
    assert events[-1]["type"] == "verdict" and events[-1]["meta"]["status"] == "error"
    assert store.load_trace("2026-02-13")[-1]["type"] == "verdict"


def test_bad_input_raises_value_error():
    with pytest.raises(ValueError, match="ГГГГ-ММ-ДД"):
        agent.run_issue("13.02.2026", mode="deterministic")
    with pytest.raises(ValueError, match="режим"):
        agent.run_issue("2026-02-13", mode="magic")
    with pytest.raises(ValueError, match="запуск"):
        agent.run_issue("2026-02-13", trigger="later")
    with pytest.raises(ValueError, match="ещё не опубликован"):
        agent.run_issue("2026-02-13", mode="deterministic", trigger="new_weather_run")


def test_live_issue_writes_live_outputs():
    result = agent.run_issue("live", mode="deterministic")
    live = store.record_path("live").parent
    assert result["record_path"] == str(live / "latest.json")
    assert result["trace_path"] == str(live / "latest_trace.jsonl")
    record = store.load_record("live")
    assert record["issue_date"] == "live" and record["latest_version"] == 1
    header, rows = _csv_rows(live / "latest.csv")
    assert header == CSV_HEADER and len(rows) == 144
    now = datetime.now(timezone.utc)
    for r in rows:  # a Live snapshot is stamped with its fetch time: taken by T = now
        assert _utc(r["weather_init_max_utc"]) <= now
    trace = store.load_trace("live")
    _assert_events(trace, "live")
    assert any(e["type"] == "action" for e in trace)
    again = agent.run_issue("live", mode="deterministic", trigger="new_weather_run")
    assert again["version"] == record["latest_version"]


def test_live_without_older_snapshot_issues_v1_on_latest_run():
    with pytest.raises(ValueError, match="нет подтверждённого предыдущего снимка"):
        _stubs.fetch_weather("live", "previous")
    events: list[dict] = []
    result = agent.run_issue("live", mode="deterministic", emit=events.append)
    assert result["version"] == 1
    _assert_events(events, "live")
    fallback = [
        e for e in events if e["title"].startswith("Для Live нет более старого")
    ]
    assert len(fallback) == 1
    assert fallback[0]["title"] == (
        "Для Live нет более старого снимка — выпускаю v1 на последнем прогоне"
    )
    assert fallback[0]["meta"]["stage"] == "weather"
    assert fallback[0]["meta"]["status"] == "ok"
    assert _tool_calls(events) == [
        "fetch_weather",
        "check_data",
        "run_model",
        "analyze_forecast",
        "publish_forecast",
    ]
    decision = _decisions(events)
    assert len(decision) == 1
    assert decision[0]["title"] == agent.NO_NEWER_TITLE
    assert decision[0]["meta"]["status"] == "skip"
    assert decision[0]["meta"]["by"] == "rule"
    assert not [e for e in events if e["type"] == "error"]
    assert (
        events[-1]["title"] == "Итог: опубликована v1 — более свежего прогона пока нет"
    )
    entry = store.load_record("live")["versions"]["1"]
    assert entry["source"] == "stub" and all(
        r["before_issue"] for r in entry["weather_runs"]
    )


def test_live_new_weather_run_recalculates_on_a_moved_snapshot(monkeypatch):
    agent.run_issue("live", mode="deterministic")
    real = _stubs.fetch_weather

    def windier(issue_date, run="latest"):
        weather = real(issue_date, run)
        weather["hourly"]["wind_100m_ms"] += 2.0
        return weather

    monkeypatch.setattr(_stubs, "fetch_weather", windier)
    events: list[dict] = []
    result = agent.run_issue(
        "live", mode="deterministic", trigger="new_weather_run", emit=events.append
    )
    assert result["version"] == 2
    facts = next(
        e
        for e in events
        if e["type"] == "tool_result" and e["meta"]["tool"] == "fetch_weather"
    )
    assert facts["title"].startswith("Новый прогон: свежее v1")
    decision = _decisions(events)
    assert decision[0]["meta"]["decision"] == "recalc"
    record = store.load_record("live")
    assert sorted(record["versions"]) == ["1", "2"]
    assert record["versions"]["2"]["change_note"].startswith("ветер +2,0 м/с")


def test_live_llm_path_keeps_v1_without_asking_for_a_recalc():
    client = FakeClient(dispatcher_policy())
    events: list[dict] = []
    result = agent.run_issue("live", mode="agent", emit=events.append, client=client)
    assert result["version"] == 1
    assert _tool_calls(events) == [
        "fetch_weather",
        "check_data",
        "run_model",
        "analyze_forecast",
        "publish_forecast",
    ]
    decision = _decisions(events)
    assert len(decision) == 1 and decision[0]["title"] == agent.NO_NEWER_TITLE
    last_user = [m for m in client.requests[-1]["messages"] if m["role"] == "user"][-1]
    assert last_user["content"] == agent.NO_NEWER_EVENT
    assert not [e for e in events if e["type"] == "error"]
    assert store.load_record("live")["mode"] == "agent"


def test_llm_recalc_after_rule_kept_v1_is_refused():
    def stubborn(messages):
        calls, _ = _history(messages)
        if calls and calls[-1] == "publish_forecast" and messages[-1]["role"] == "user":
            return "Всё равно пересчитаю.", [("recalc_forecast", {"reason": "хочу"})]
        if calls and calls[-1] == "recalc_forecast":
            return "Понял, v1 остаётся.", []
        return dispatcher_policy()(messages)

    events: list[dict] = []
    result = agent.run_issue(
        "live", mode="agent", emit=events.append, client=FakeClient(stubborn)
    )
    assert result["version"] == 1
    refused = next(
        e
        for e in events
        if e["type"] == "tool_result" and e["meta"]["tool"] == "recalc_forecast"
    )
    assert refused["meta"]["status"] == "error"
    assert "Решение по новому прогону уже принято" in refused["body"]
    assert sorted(store.load_record("live")["versions"]) == ["1"]


def test_live_weather_outage_retries_and_marks_reduced_confidence():
    events: list[dict] = []
    result = agent.run_issue(
        "live", mode="deterministic", emit=events.append, scenario="weather_outage"
    )
    assert result["version"] == 1
    titles = [e["title"] for e in events]
    assert (
        titles.count(
            "Для Live нет более старого снимка — выпускаю v1 на последнем прогоне"
        )
        == 1
    )
    assert "Источник погоды недоступен" in titles
    assert "Источник погоды не ответил — повторяю запрос" in titles
    warns = [e["title"] for e in events if e["meta"]["status"] == "warn"]
    assert any("со второй попытки" in t for t in warns)


class WeatherUnavailable(RuntimeError):
    """Like windcast.weather.WeatherUnavailable: a class the agent does not import."""


@pytest.mark.parametrize("issue_date", ["2026-02-13", "live"])
def test_any_weather_port_error_is_reported_never_raised(monkeypatch, issue_date):
    def down(issue_date, run="latest"):
        raise WeatherUnavailable("Нет валидного кэша Open-Meteo")

    monkeypatch.setattr(_stubs, "fetch_weather", down)
    events: list[dict] = []
    result = agent.run_issue(issue_date, mode="deterministic", emit=events.append)
    assert result["version"] is None
    _assert_events(events, issue_date)
    unavailable = [e for e in events if e["title"] == "Источник погоды недоступен"]
    assert len(unavailable) == 2  # the first try and the retry
    assert "WeatherUnavailable: Нет валидного кэша Open-Meteo" in unavailable[0]["body"]
    assert events[-1]["meta"]["status"] == "error"
    assert store.load_record(issue_date) is None


def test_new_weather_run_with_weather_down_keeps_the_version(monkeypatch):
    agent.run_issue(KEEP_DAY, mode="deterministic")

    def down(issue_date, run="latest"):
        raise WeatherUnavailable("сеть недоступна")

    monkeypatch.setattr(_stubs, "fetch_weather", down)
    events: list[dict] = []
    result = agent.run_issue(
        KEEP_DAY, mode="deterministic", trigger="new_weather_run", emit=events.append
    )
    assert result["version"] == 1
    decision = _decisions(events)
    assert len(decision) == 1 and decision[0]["meta"]["status"] == "warn"
    assert events[-1]["type"] == "verdict" and events[-1]["meta"]["status"] == "skip"


def test_policy_recalculates_when_the_window_no_longer_overlaps():
    facts = {
        "fresher_than_current": True,
        "all_inits_before_issue": True,
        "mean_abs_wind_shift_h1_24_ms": 0.0,
        "new_flags_vs_current": [],
        "overlap_hours": 0,
    }
    assert tools.policy_recalc(facts)
    assert not tools.policy_recalc({**facts, "overlap_hours": 30})


def test_weather_outage_is_retried_and_marked():
    events: list[dict] = []
    result = agent.run_issue(
        "2026-02-13",
        mode="deterministic",
        emit=events.append,
        scenario="weather_outage",
    )
    assert result["version"] in (1, 2)
    failed = [
        e
        for e in events
        if e["type"] == "tool_result" and e["meta"]["status"] == "error"
    ]
    assert failed and failed[0]["meta"]["tool"] == "fetch_weather"
    warns = [e["title"] for e in events if e["meta"]["status"] == "warn"]
    assert any("повторяю" in t for t in warns)
    assert any("со второй попытки" in t for t in warns)


# ---------- agent mode: fake OpenAI client ----------
class FakeClient:
    """OpenAI stand-in: a scripted policy answers every chat.completions.create call."""

    def __init__(self, policy):
        self.policy = policy
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        content, calls = self.policy(kwargs["messages"])
        tool_calls = [
            SimpleNamespace(
                id=f"call_{len(self.requests)}_{i}",
                type="function",
                function=SimpleNamespace(
                    name=name, arguments=json.dumps(args, ensure_ascii=False)
                ),
            )
            for i, (name, args) in enumerate(calls)
        ]
        message = SimpleNamespace(content=content, tool_calls=tool_calls or None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _history(messages):
    calls = [
        c["function"]["name"]
        for m in messages
        if m["role"] == "assistant"
        for c in m.get("tool_calls") or []
    ]
    results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
    return calls, results


def _grounded_summary(results) -> str:
    plant = next(r["plant"] for r in reversed(results) if "plant" in r)
    return (
        f"Пик {plant['peak_pct']} % номинала — {plant['peak_time_local']}, минимум "
        f"{plant['min_pct']} % — {plant['min_time_local']}, в среднем {plant['mean_pct']} %."
    )


def dispatcher_policy(summary=None):
    """A well-behaved LLM: the cycle, then a decision on the new run by the policy."""

    def policy(messages):
        calls, results = _history(messages)
        last = calls[-1] if calls else None
        result = results[-1] if results else {}
        if last is None:
            return "Начинаю с погоды на момент выпуска.", [
                ("fetch_weather", {"run": "previous"})
            ]
        if last == "fetch_weather" and "previous" in (
            result.get("run"),
            result.get("requested_run"),
        ):
            return None, [("check_data", {})]
        if last == "check_data":
            return None, [("run_model", {})]
        if last == "run_model":
            return "Модель посчитала — ищу риски.", [("analyze_forecast", {})]
        if last in ("analyze_forecast", "recalc_forecast") and result.get("ok"):
            text = summary or _grounded_summary(results)
            return None, [("publish_forecast", {"summary": text})]
        if last == "publish_forecast" and result.get("version") == 1:
            event = messages[-1]
            if event["role"] == "user" and "вышел новый прогон" in event["content"]:
                return None, [("fetch_weather", {"run": "latest"})]
            return "Готово: v1 опубликована, новых прогонов ждать не нужно.", []
        if last == "fetch_weather":
            if tools.policy_recalc(result):
                return "Сдвиг ветра больше порога — пересчитываю.", [
                    ("recalc_forecast", {"reason": "сдвиг ветра больше порога"})
                ]
            return "Сдвиг ветра мал, новых рисков нет — v1 остаётся.", []
        return "Готово: выпуск опубликован.", []

    return policy


def test_llm_loop_runs_tools_decides_and_publishes_same_numbers():
    agent.run_issue(RECALC_DAY, mode="deterministic")
    reference = store.load_record(RECALC_DAY)["versions"]

    client = FakeClient(dispatcher_policy())
    events: list[dict] = []
    result = agent.run_issue(
        RECALC_DAY, mode="agent", emit=events.append, client=client
    )
    assert result["version"] == 2
    record = store.load_record(RECALC_DAY)
    assert record["mode"] == "agent"
    for key in ("1", "2"):
        assert record["versions"][key]["rows"] == reference[key]["rows"]
    assert record["versions"]["1"]["summary"].startswith("Пик ")
    assert record["versions"]["1"]["summary"] != reference["1"]["summary"]  # LLM's own

    _assert_events(events, RECALC_DAY)
    assert _tool_calls(events) == [
        "fetch_weather",
        "check_data",
        "run_model",
        "analyze_forecast",
        "publish_forecast",
        "fetch_weather",
        "recalc_forecast",
        "publish_forecast",
    ]
    thoughts = [e["title"] for e in events if e["type"] == "thought"]
    assert "Начинаю с погоды на момент выпуска." in thoughts
    assert "Событие: вышел новый прогон погоды" in thoughts
    decision = _decisions(events)
    assert len(decision) == 1 and decision[0]["meta"]["by"] == "llm"
    assert decision[0]["meta"]["decision"] == "recalc"
    assert "сдвиг ветра больше порога" in decision[0]["body"]
    assert not [e for e in events if e["type"] == "error"]
    request = client.requests[0]
    assert request["model"] == "gpt-5-mini" and "temperature" not in request
    assert {t["function"]["name"] for t in request["tools"]} == set(tools.TOOLS)
    assert store.load_trace(RECALC_DAY) == events


def test_llm_keep_decision_is_honoured():
    client = FakeClient(dispatcher_policy())
    events: list[dict] = []
    result = agent.run_issue(KEEP_DAY, mode="agent", emit=events.append, client=client)
    assert result["version"] == 1
    decision = _decisions(events)
    assert len(decision) == 1
    assert (
        decision[0]["meta"]["by"] == "llm" and decision[0]["meta"]["status"] == "skip"
    )
    assert "recalc_forecast" not in _tool_calls(events)


def test_llm_summary_with_invented_numbers_is_replaced():
    client = FakeClient(dispatcher_policy(summary="Пик 12345 % номинала завтра."))
    events: list[dict] = []
    agent.run_issue(KEEP_DAY, mode="agent", emit=events.append, client=client)
    entry = store.load_record(KEEP_DAY)["versions"]["1"]
    assert "12345" not in entry["summary"] and entry["summary"].startswith("Пик ")
    action = next(e for e in events if e["type"] == "action")
    assert action["meta"]["status"] == "warn" and "12345" in action["body"]


def test_llm_failure_falls_back_to_deterministic():
    def broken(messages):
        raise RuntimeError("network down")

    events: list[dict] = []
    result = agent.run_issue(
        RECALC_DAY, mode="agent", emit=events.append, client=FakeClient(broken)
    )
    assert result["version"] == 2
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "network down" in errors[0]["body"]
    _assert_events(events, RECALC_DAY)
    assert store.load_record(RECALC_DAY)["mode"] == "deterministic"


def test_llm_that_stops_early_is_finished_by_the_driver():
    events: list[dict] = []
    client = FakeClient(lambda messages: ("Прогноз готов, всё хорошо.", []))
    result = agent.run_issue(KEEP_DAY, mode="agent", emit=events.append, client=client)
    assert result["version"] == 1
    titles = [e["title"] for e in events]
    assert "LLM закончил раньше цикла — довожу по регламенту" in titles
    assert _tool_calls(events)[:5] == [
        "fetch_weather",
        "check_data",
        "run_model",
        "analyze_forecast",
        "publish_forecast",
    ]
    assert _decisions(events)[0]["meta"]["by"] == "rule"


def test_llm_step_limit_then_driver_publishes():
    loop = FakeClient(lambda messages: (None, [("fetch_weather", {"run": "previous"})]))
    events: list[dict] = []
    result = agent.run_issue(KEEP_DAY, mode="agent", emit=events.append, client=loop)
    assert result["version"] == 1
    limit = [e for e in events if e["type"] == "error" and "Лимит" in e["title"]]
    assert len(limit) == 1
    assert len(loop.requests) == agent.MAX_TOOL_CALLS


def test_llm_insisting_on_recalc_is_refused_by_code():
    agent.run_issue(RECALC_DAY, mode="deterministic")

    def insist(messages):
        calls, _ = _history(messages)
        if not calls:
            return "Всё равно пересчитаю.", [("recalc_forecast", {"reason": "хочу"})]
        return "Понял, пересчёт невозможен.", []

    events: list[dict] = []
    result = agent.run_issue(
        RECALC_DAY,
        mode="agent",
        trigger="new_weather_run",
        emit=events.append,
        client=FakeClient(insist),
    )
    assert result["version"] == 2
    refused = next(
        e
        for e in events
        if e["meta"]["tool"] == "recalc_forecast" and e["type"] == "tool_result"
    )
    assert refused["meta"]["status"] == "skip"
    assert refused["title"].startswith(
        "Отказ: более свежего прогона до момента выпуска нет"
    )
    assert events[-1]["title"].startswith("Итог: отказ")
    assert sorted(store.load_record(RECALC_DAY)["versions"]) == ["1", "2"]


def test_agent_mode_without_key_is_deterministic():
    events: list[dict] = []
    result = agent.run_issue(KEEP_DAY, mode="agent", emit=events.append)
    assert result["version"] == 1
    assert events[1]["title"] == "Нет ключа OpenAI — веду цикл по регламенту"
    assert store.load_record(KEEP_DAY)["mode"] == "deterministic"


# ---------- backtest CLI ----------
def test_backtest_cli_writes_issues_and_combined_csv(capsys):
    code = backtest.main(
        ["--from", "2026-01-31", "--to", "2026-02-01", "--mode", "deterministic"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "2026-01-31  v" in out and "2026-02-01  v" in out and "source=stub" in out
    lines = store.combined_csv_path().read_text(encoding="utf-8").splitlines()
    assert lines[0] == CSV_HEADER and len(lines) == 1 + 2 * 144
    for day in ("2026-01-31", "2026-02-01"):
        assert store.trace_path(day).is_file() and store.csv_path(day).is_file()


def test_backtest_cli_fails_when_an_issue_fails(monkeypatch, capsys):
    def boom(issue_date, weather):
        raise RuntimeError("model broken")

    monkeypatch.setattr(_stubs, "predict", boom)
    code = backtest.main(
        ["--from", "2026-02-01", "--to", "2026-02-01", "--mode", "deterministic"]
    )
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


# ---------- ports ----------
def test_ports_prefer_real_module_and_report_stubs(monkeypatch):
    monkeypatch.delenv("WINDCAST_FORCE_STUBS")
    fake = types.ModuleType("windcast.model")
    fake.predict = lambda issue_date, weather: "real"
    fake.MODEL_VERSION = "real-1"
    monkeypatch.setitem(sys.modules, "windcast.model", fake)
    assert ports.status()["model"] == "real"
    assert ports.model_version() == "real-1"
    assert ports.predict("2026-02-13", {}) == "real"

    incomplete = types.ModuleType("windcast.model")
    incomplete.predict = (
        fake.predict
    )  # no MODEL_VERSION: not the contract, stay on stubs
    monkeypatch.setitem(sys.modules, "windcast.model", incomplete)
    assert ports.status()["model"] == "stub"
    assert ports.model_version() == _stubs.MODEL_VERSION

    monkeypatch.setenv("WINDCAST_FORCE_STUBS", "1")
    assert ports.status() == {"weather": "stub", "data": "stub", "model": "stub"}


def test_ports_missing_module_falls_back_but_other_import_errors_propagate(monkeypatch):
    monkeypatch.delenv("WINDCAST_FORCE_STUBS")

    def missing(name):
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(ports.importlib, "import_module", missing)
    assert ports.status() == {"weather": "stub", "data": "stub", "model": "stub"}
    weather = ports.fetch_weather("2026-02-13", run="previous")
    assert weather["source"] == "stub" and len(weather["hourly"]) == 48

    def broken(name):
        raise ModuleNotFoundError("No module named 'lightgbm'", name="lightgbm")

    monkeypatch.setattr(ports.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError, match="lightgbm"):
        ports.predict("2026-02-13", weather)


def test_llm_recalc_before_v1_is_refused_and_not_taken_as_decision():
    def eager(messages):
        calls, _ = _history(messages)
        if not calls:
            return "Сразу пересчитаю.", [("recalc_forecast", {"reason": "сразу"})]
        return dispatcher_policy()(messages)

    events: list[dict] = []
    result = agent.run_issue(
        KEEP_DAY, mode="agent", emit=events.append, client=FakeClient(eager)
    )
    assert result["version"] == 1  # the early call must not force a v2 later
    first = next(e for e in events if e["type"] == "tool_result")
    assert first["meta"]["tool"] == "recalc_forecast"
    assert first["meta"]["status"] == "error"
    decision = _decisions(events)
    assert len(decision) == 1 and decision[0]["meta"]["decision"] == "keep"


def test_llm_bad_data_stops_with_error_and_verdict(monkeypatch):
    real = _stubs.fetch_weather

    def late(issue_date, run="latest"):
        weather = real(issue_date, run)
        weather["hourly"].loc[0, "init_time_utc"] = pd.Timestamp("2026-02-13T18:00Z")
        return weather

    monkeypatch.setattr(_stubs, "fetch_weather", late)

    def stop_after_check(messages):
        calls, _ = _history(messages)
        if not calls:
            return None, [("fetch_weather", {"run": "previous"})]
        if calls == ["fetch_weather"]:
            return None, [("check_data", {})]
        return "Данные плохие, останавливаюсь.", []

    events: list[dict] = []
    result = agent.run_issue(
        "2026-02-13",
        mode="agent",
        emit=events.append,
        client=FakeClient(stop_after_check),
    )
    assert result["version"] is None
    assert any(e["type"] == "error" and "непригодны" in e["title"] for e in events)
    assert events[-1]["type"] == "verdict" and events[-1]["meta"]["status"] == "error"
