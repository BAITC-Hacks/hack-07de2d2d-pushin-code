import csv
import json
import os

import pytest

pd = pytest.importorskip("pandas")

from windcast import _stubs, paths, store, timeline, tools

CSV_HEADER = (
    "issue_date,issue_time_local,target_time_local,horizon_h,turbine,"
    "p10,p50,p90,wind_fc_ms,weather_init_max_utc,version"
)


@pytest.fixture(autouse=True)
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDCAST_ROOT", str(tmp_path))
    monkeypatch.setenv("WINDCAST_FORCE_STUBS", "1")
    return tmp_path


def _publish(issue_date: str, version: int = 1) -> dict:
    weather = tools.normalize_weather(_stubs.fetch_weather(issue_date, "previous"))
    frame, _ = tools.sanitize_forecast(_stubs.predict(issue_date, weather))
    record = store.load_record(issue_date) or store.new_record(
        issue_date, timeline.issue_time_utc(issue_date), "deterministic"
    )
    entry = {
        "version": version,
        "created_at": store.now_local(),
        "weather_runs": tools.weather_runs(
            weather,
            lambda init: timeline.published_before_issue(
                init.to_pydatetime(), issue_date
            ),
        ),
        "source": weather["source"],
        "change_note": None,
        "summary": "сводка",
        "flags": [],
        "rows": store.forecast_rows(frame),
    }
    store.save_version(record, entry, tools.init_by_h(weather))
    return record


def test_atomic_write_replaces_and_leaves_no_temp_files(tmp_root):
    target = tmp_root / "outputs" / "x" / "a.json"
    store.atomic_write_text(target, "one")
    store.atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert os.listdir(target.parent) == ["a.json"]


def test_record_and_csv_match_contract(tmp_root):
    record = _publish("2026-02-13")
    saved = store.load_record("2026-02-13")
    assert saved == json.loads(json.dumps(record))
    assert saved["issue_time_local"] == "2026-02-14T00:00+05:00"
    assert saved["issue_time_utc"] == "2026-02-13T19:00Z"
    assert saved["latest_version"] == 1 and list(saved["versions"]) == ["1"]
    row = saved["versions"]["1"]["rows"][0]
    assert set(row) == {
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
    assert row["actual"] is None

    text = store.csv_path("2026-02-13").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == CSV_HEADER
    rows = list(csv.DictReader(lines))
    assert len(rows) == 144
    first = rows[0]
    assert first["issue_date"] == "2026-02-13"
    assert first["issue_time_local"] == "2026-02-14T00:00+05:00"
    assert first["target_time_local"] == "2026-02-14T00:00+05:00"
    assert first["horizon_h"] == "1"
    # h = 1 of v1 (previous_day2): target 13.02 19:00Z − 2 days, floored to 00/06/12/18
    assert first["weather_init_max_utc"] == "2026-02-11T18:00Z"
    assert rows[-1]["target_time_local"] == "2026-02-15T23:00+05:00"
    assert {r["turbine"] for r in rows} == {"1", "2", "plant"}
    for r in rows:
        for q in ("p10", "p50", "p90"):
            assert len(r[q].split(".")[1]) == 4


def test_new_version_rewrites_csv_with_latest_only(tmp_root):
    _publish("2026-02-13", 1)
    _publish("2026-02-13", 2)
    record = store.load_record("2026-02-13")
    assert record["latest_version"] == 2 and sorted(record["versions"]) == ["1", "2"]
    rows = list(csv.DictReader(store.csv_path("2026-02-13").open(encoding="utf-8")))
    assert {r["version"] for r in rows} == {"2"}
    assert store.version_entry(record, 1)["version"] == 1
    assert store.version_entry(record)["version"] == 2


def test_combined_csv_holds_test_range_issues(tmp_root):
    _publish("2026-01-31")
    _publish("2026-02-01")
    _publish("2026-01-10")  # backtest day: outside the February file
    path, count = store.write_combined_csv()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert path == paths.outputs_dir() / "forecast_feb2026.csv"
    assert count == 2
    assert lines[0] == CSV_HEADER and len(lines) == 1 + 2 * 144
    assert {line.split(",")[0] for line in lines[1:]} == {"2026-01-31", "2026-02-01"}


def test_actuals_come_from_processed_hourly(tmp_root):
    pytest.importorskip("pyarrow")
    targets = timeline.target_times_utc("2026-01-10")[:3]
    frame = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(targets * 2, utc=True),
            "turbine": [1, 1, 1, 2, 2, 2],
            "wind_ms": 8.0,
            "power": [0.2, 0.4, 0.6, 0.4, 0.6, 0.8],
            "temp_c": -5.0,
            "valid": [True, True, True, True, False, True],
        }
    )
    paths.processed_dir().mkdir(parents=True, exist_ok=True)
    frame.to_parquet(paths.processed_dir() / "hourly.parquet")
    _publish("2026-01-10")
    rows = store.load_record("2026-01-10")["versions"]["1"]["rows"]
    got = {(r["h"], r["turbine"]): r["actual"] for r in rows}
    assert got[(1, "1")] == 0.2 and got[(1, "2")] == 0.4 and got[(1, "plant")] == 0.3
    assert got[(2, "2")] is None and got[(2, "plant")] is None  # invalid hour
    assert got[(3, "plant")] == 0.7
    assert got[(4, "1")] is None  # not covered


def test_live_paths_and_trace_round_trip(tmp_root):
    assert store.record_path("live") == paths.live_dir() / "latest.json"
    assert store.csv_path("live") == paths.live_dir() / "latest.csv"
    assert store.trace_path("live") == paths.live_dir() / "latest_trace.jsonl"
    events = [{"seq": 1, "type": "verdict", "title": "Итог"}]
    store.write_trace("2026-02-13", events)
    assert store.load_trace("2026-02-13") == events
    assert store.load_trace("2026-02-14") == []
    with pytest.raises(ValueError, match="ГГГГ-ММ-ДД"):
        store.record_path("13.02.2026")
