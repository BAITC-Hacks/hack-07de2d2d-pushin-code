"""scripts/verify.py against a tiny synthetic outputs tree: two issues, checked via --from/--to.

The script runs as the jury runs it, in a subprocess, and with site-packages disabled (-S):
that also proves it needs nothing beyond the standard library.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "verify.py"
ISSUES = ("2026-02-01", "2026-02-02")
RANGE = ("--from", ISSUES[0], "--to", ISSUES[-1])
COLUMNS = [  # docs/CONTRACT.md §7, in order
    "issue_date",
    "issue_time_local",
    "target_time_local",
    "horizon_h",
    "turbine",
    "p10",
    "p50",
    "p90",
    "wind_fc_ms",
    "weather_init_max_utc",
    "version",
]
LOCAL = timezone(timedelta(hours=5))
METRICS = {
    "period": "2025-12-31 … 2026-01-29",
    "issues_count": 30,
    "coverage_p10_p90": 0.81,
    "methods": [
        {"key": "model", "label": "Модель", "nmae": 0.118, "nrmse": 0.17},
        {"key": "power_curve", "label": "Кривая мощности", "nmae": 0.142, "nrmse": 0.2},
        {"key": "climatology", "label": "Климатология", "nmae": 0.201, "nrmse": 0.26},
        {"key": "persistence", "label": "Персистентность", "nmae": 0.236, "nrmse": 0.3},
    ],
    "by_horizon": [],
}


def moment(issue: str) -> datetime:
    d = date.fromisoformat(issue)
    return datetime(d.year, d.month, d.day, 19, tzinfo=timezone.utc)  # T = D 19:00Z


def local(when: datetime) -> str:
    return when.astimezone(LOCAL).strftime("%Y-%m-%dT%H:%M+05:00")


def utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


BUCKETS = ((1, 17), (18, 41), (42, 48))  # §2 v0.4: h -> previous_day1, day2, day3


def run_start(t: datetime, h: int) -> datetime:
    """§2 v0.4: the latest run for hour h — its target minus N days, floored to 00/06/12/18Z.

    With these buckets the latest start of any hour is D 06:00Z, published ≈ D 14:00Z < T.
    """
    days = next(n for n, (lo, hi) in enumerate(BUCKETS, start=1) if lo <= h <= hi)
    start = t + timedelta(hours=h - 1) - timedelta(days=days)
    return start.replace(hour=start.hour - start.hour % 6)


def issue_rows(issue: str, version: int) -> list[dict[str, str]]:
    t = moment(issue)
    rows = []
    for turbine in ("1", "2", "plant"):
        for h in range(1, 49):
            p50 = 0.2 + h / 200
            rows.append(
                {
                    "issue_date": issue,
                    "issue_time_local": local(t),
                    "target_time_local": local(t + timedelta(hours=h - 1)),
                    "horizon_h": str(h),
                    "turbine": turbine,
                    "p10": f"{p50 - 0.1:.4f}",
                    "p50": f"{p50:.4f}",
                    "p90": f"{p50 + 0.15:.4f}",
                    "wind_fc_ms": f"{5 + h / 10:.1f}",
                    "weather_init_max_utc": utc(run_start(t, h)),
                    "version": str(version),
                }
            )
    return rows


def issue_record(issue: str, version: int) -> dict:
    t = moment(issue)
    runs = [
        {
            "hours": f"{lo}-{hi}",
            "model": "ecmwf_ifs025",
            "init_utc": utc(max(run_start(t, h) for h in range(lo, hi + 1))),
        }
        for lo, hi in BUCKETS
    ]
    return {
        "issue_date": issue,
        "issue_time_local": local(t),
        "issue_time_utc": utc(t),
        "latest_version": version,
        "mode": "deterministic",
        "recorded_at": "2026-09-23T15:40+05:00",
        "versions": {
            str(v): {
                "version": v,
                "created_at": "2026-09-23T15:40+05:00",
                "weather_runs": [dict(run, before_issue=True) for run in runs],
                "source": "cache",
                "change_note": None if v == 1 else "ветер +0,9 м/с",
                "summary": "Пик 44 % номинала",
                "flags": [],
                "rows": [],
            }
            for v in range(1, version + 1)
        },
    }


def issue_trace(issue: str) -> list[dict]:
    meta = {"issue_date": issue, "version": 2}
    steps = [
        ("thought", "Беру погоду, доступную на момент выпуска", {}),
        ("tool_call", "fetch_weather", {"tool": "fetch_weather", "stage": "weather"}),
        (
            "tool_result",
            "Погода получена",
            {"tool": "fetch_weather", "source": "cache"},
        ),
        ("action", "Публикую выпуск v2", {"tool": "publish_forecast"}),
        ("verdict", "Выпуск опубликован", {}),
    ]
    return [
        {
            "seq": seq,
            "ts": "2026-09-23T15:40+05:00",
            "type": kind,
            "title": title,
            "body": "",
            "meta": {**meta, **extra},
        }
        for seq, (kind, title, extra) in enumerate(steps, start=1)
    ]


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build(root: Path, *, rows=None, record=None, trace=None) -> Path:
    """A valid outputs tree for ISSUES; the hooks edit it in memory before it is written."""
    outputs = root / "outputs"
    (outputs / "forecasts").mkdir(parents=True)
    (outputs / "traces").mkdir(parents=True)
    combined = []
    for issue in ISSUES:
        issue_csv = issue_rows(issue, version=2)
        rec = issue_record(issue, version=2)
        events = issue_trace(issue)
        for hook, data in ((rows, issue_csv), (record, rec), (trace, events)):
            if hook:
                hook(issue, data)
        write_csv(outputs / "forecasts" / f"{issue}.csv", issue_csv)
        (outputs / "forecasts" / f"{issue}.json").write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8"
        )
        (outputs / "traces" / f"{issue}.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
            encoding="utf-8",
        )
        combined += issue_csv
    write_csv(outputs / "forecast_feb2026.csv", combined)
    (outputs / "metrics_jan.json").write_text(
        json.dumps(METRICS, ensure_ascii=False), encoding="utf-8"
    )
    return root


def verify(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def report(root: Path) -> tuple[int, dict]:
    done = verify(root, *RANGE, "--json")
    return done.returncode, json.loads(done.stdout)


def check(result: dict, key: str) -> dict:
    return next(c for c in result["checks"] if c["id"] == key)


def messages(result: dict, key: str) -> str:
    return " | ".join(p["message"] for p in check(result, key)["problems"])


def only_issue(index: int, edit):
    """A hook that edits only ISSUES[index]."""

    def hook(issue, data):
        if issue == ISSUES[index]:
            edit(data)

    return hook


def test_valid_tree_passes(tmp_path):
    root = build(tmp_path)
    done = verify(root, *RANGE)
    assert done.returncode == 0, done.stdout
    assert done.stdout.strip().splitlines()[-1] == "PASS"
    assert "✗" not in done.stdout

    code, result = report(root)
    assert code == 0 and result["result"] == "PASS" and result["failed_checks"] == []
    assert {c["id"]: c["status"] for c in result["checks"]} == {
        c["id"]: "pass" for c in result["checks"]
    }
    assert check(result, "issues")["checked"] == 2
    assert check(result, "no_future")["checked"] == 2 * 144
    runs = check(result, "no_future_runs")["checked"]
    assert runs == 2 * 2 * len(BUCKETS)  # issues x versions x runs
    assert check(result, "combined")["checked"] == 2 * 144


@pytest.mark.parametrize(
    "start, published",
    [
        ("06:00", True),  # published ≈ 14:00Z, before T = 19:00Z
        ("11:00", True),  # published ≈ 19:00Z = T: still in time, init + 8 h ≤ T
        ("12:00", False),  # starts before T, but is published ≈ 20:00Z, after it
        ("18:00", False),  # the example from contract §2
        ("20:00", False),  # starts after T
    ],
)
def test_run_counts_only_once_published_by_the_issue_moment(tmp_path, start, published):
    def edit(rows):
        rows[5]["weather_init_max_utc"] = f"{ISSUES[1]}T{start}Z"

    code, result = report(build(tmp_path, rows=only_issue(1, edit)))
    expected = (0, []) if published else (1, ["no_future"])
    assert (code, result["failed_checks"]) == expected


def test_run_published_after_the_issue_moment_is_named(tmp_path):
    def edit(rows):
        rows[5]["weather_init_max_utc"] = f"{ISSUES[1]}T12:00Z"  # before T = 19:00Z

    root = build(tmp_path, rows=only_issue(1, edit))
    code, result = report(root)
    assert code == 1 and result["failed_checks"] == ["no_future"]
    [problem] = check(result, "no_future")["problems"]
    assert problem["file"] == f"outputs/forecasts/{ISSUES[1]}.csv"
    assert problem["line"] == 7  # the header is line 1, rows[5] is line 7
    assert problem["message"] == (
        "turbine=1 h=6 · weather_init_max_utc: прогон 2026-02-02T12:00Z опубликован "
        "≈ 2026-02-02T20:00Z, позже момента выпуска T=2026-02-02T19:00Z"
    )

    text = verify(root, *RANGE).stdout
    assert "✗ Без будущего: прогон доступен к T (старт + 8 ч ≤ T, CSV)" in text
    assert text.strip().splitlines()[-1] == "FAIL: 1 проверка не пройдена"


@pytest.mark.parametrize("value", ["2026-02-02 18:00", "вчера", ""])
def test_weather_init_without_zone_or_unparseable_fails(tmp_path, value):
    def edit(rows):
        rows[0]["weather_init_max_utc"] = value

    code, result = report(build(tmp_path, rows=only_issue(1, edit)))
    assert code == 1 and result["failed_checks"] == ["no_future"]
    assert "публикацию к T подтвердить нельзя" in messages(result, "no_future")


@pytest.mark.parametrize(
    "start, published",
    [("06:00", True), ("11:00", True), ("12:00", False), ("18:00", False)],
)
def test_record_runs_count_only_once_published(tmp_path, start, published):
    def edit(record):
        record["versions"]["1"]["weather_runs"][0]["init_utc"] = f"{ISSUES[0]}T{start}Z"

    code, result = report(build(tmp_path, record=only_issue(0, edit)))
    if published:
        assert (code, result["failed_checks"]) == (0, [])
    else:
        assert (code, result["failed_checks"]) == (1, ["no_future_runs"])
        init = datetime.fromisoformat(f"{ISSUES[0]}T{start}:00+00:00")
        assert messages(result, "no_future_runs") == (
            f"v1 weather_runs[0] (часы 1-17): прогон {utc(init)} опубликован "
            f"≈ {utc(init + timedelta(hours=8))}, позже момента выпуска "
            f"T={utc(moment(ISSUES[0]))}"
        )


def test_missing_trace_fails(tmp_path):
    root = build(tmp_path)
    (root / "outputs" / "traces" / f"{ISSUES[1]}.jsonl").unlink()
    code, result = report(root)
    assert code == 1 and result["failed_checks"] == ["traces"]
    [problem] = check(result, "traces")["problems"]
    assert problem["file"] == f"outputs/traces/{ISSUES[1]}.jsonl"
    assert "нет ленты" in problem["message"]


def without(kind: str):
    def edit(events):
        events[:] = [event for event in events if event["type"] != kind]

    return edit


@pytest.mark.parametrize(
    "edit, expected",
    [
        (without("action"), "нет события action"),
        (without("verdict"), "а нужно verdict"),
        (lambda events: events[1].update(type="answer"), "не из схемы"),
        (lambda events: events[2].pop("meta"), "нет ключей meta"),
    ],
)
def test_trace_follows_the_event_schema(tmp_path, edit, expected):
    code, result = report(build(tmp_path, trace=only_issue(0, edit)))
    assert code == 1 and result["failed_checks"] == ["traces"]
    assert expected in messages(result, "traces")


def test_p10_above_p50_fails(tmp_path):
    root = build(
        tmp_path, rows=only_issue(0, lambda rows: rows[10].update(p10="0.9000"))
    )
    code, result = report(root)
    assert code == 1 and result["failed_checks"] == ["quantiles"]
    assert "p10=0.9000 > p50=0.2550" in messages(result, "quantiles")


@pytest.mark.parametrize(
    "hooks",
    [
        {"record": only_issue(0, lambda r: r["versions"]["1"].update(source="stub"))},
        {
            "trace": only_issue(
                1, lambda events: events[2]["meta"].update(source="stub")
            )
        },
    ],
    ids=["record", "trace"],
)
def test_stub_source_fails(tmp_path, hooks):
    code, result = report(build(tmp_path, **hooks))
    assert code == 1 and result["failed_checks"] == ["no_stub"]
    assert "stub" in messages(result, "no_stub")


def test_missing_metrics_fails_with_a_clear_message(tmp_path):
    root = build(tmp_path)
    (root / "outputs" / "metrics_jan.json").unlink()
    code, result = report(root)
    assert code == 1 and result["failed_checks"] == ["metrics"]
    assert "нет файла метрик января" in messages(result, "metrics")
    assert "нет outputs/metrics_jan.json" in verify(root, *RANGE).stdout


def test_model_must_beat_every_baseline(tmp_path):
    root = build(tmp_path)
    metrics = json.loads(json.dumps(METRICS))
    metrics["methods"][1]["nmae"] = 0.1  # power_curve beats the model
    (root / "outputs" / "metrics_jan.json").write_text(json.dumps(metrics))
    code, result = report(root)
    assert code == 1 and result["failed_checks"] == ["metrics"]
    assert "power_curve" in messages(result, "metrics")


def test_duplicate_row_fails_rows(tmp_path):
    def duplicate(rows):
        rows[1] = dict(rows[0])  # turbine 1: h=1 twice, h=2 missing

    code, result = report(build(tmp_path, rows=only_issue(0, duplicate)))
    assert code == 1 and result["failed_checks"] == ["rows"]
    text = messages(result, "rows")
    assert "повтор turbine=1 h=1" in text and "turbine=1 h=2" in text


@pytest.mark.parametrize(
    "field, value, key",
    [
        ("target_time_local", "2026-02-02T01:00+05:00", "times"),  # h=1 starts at T
        ("issue_time_local", "2026-02-01T19:00+05:00", "times"),
        ("wind_fc_ms", "60", "wind"),
        ("version", "0", "version"),
    ],
)
def test_row_values(tmp_path, field, value, key):
    root = build(
        tmp_path, rows=only_issue(0, lambda rows: rows[0].update({field: value}))
    )
    code, result = report(root)
    assert code == 1 and result["failed_checks"] == [key]


def test_wrong_header_fails(tmp_path):
    root = build(tmp_path)
    path = root / "outputs" / "forecasts" / f"{ISSUES[0]}.csv"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("p10,p50,p90", "p50,p10,p90", 1), encoding="utf-8")
    code, result = report(root)
    assert code == 1 and result["failed_checks"] == ["header"]


def test_record_version_must_match_the_csv(tmp_path):
    root = build(tmp_path, record=only_issue(0, lambda r: r.update(latest_version=1)))
    code, result = report(root)
    assert code == 1 and result["failed_checks"] == ["records"]
    assert "latest_version=1, а в 2026-02-01.csv version=2" in messages(
        result, "records"
    )


def test_combined_file_must_match_the_issue_files(tmp_path):
    root = build(tmp_path)
    path = root / "outputs" / "forecast_feb2026.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    cells = lines[3].split(",")
    cells[COLUMNS.index("p50")] = "0.9999"
    lines[3] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, result = report(root)
    assert code == 1 and result["failed_checks"] == ["combined"]
    [problem] = check(result, "combined")["problems"]
    assert problem["line"] == 4 and "p50='0.9999'" in problem["message"]


def test_default_range_is_the_29_test_issues(tmp_path):
    done = verify(build(tmp_path), "--json")
    result = json.loads(done.stdout)
    assert done.returncode == 1
    assert (result["from"], result["to"], result["issues_expected"]) == (
        "2026-01-31",
        "2026-02-28",
        29,
    )
    assert check(result, "issues")["checked"] == 29
    assert check(result, "issues")["failed"] == 27


def test_bad_date_is_rejected(tmp_path):
    done = verify(tmp_path, "--from", "01.02.2026")
    assert done.returncode == 2 and "ГГГГ-ММ-ДД" in done.stderr
