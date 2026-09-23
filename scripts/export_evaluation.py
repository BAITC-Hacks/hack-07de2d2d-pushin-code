"""Export forecasts in a tidy, jury-friendly format for scoring (standard library only).

Writes outputs/evaluation/:
  february_day_ahead.csv    one forecast per target hour (issue D-1, horizon 1-24), 2016 rows
  february_all_horizons.csv every (issue, horizon, turbine) row, horizons 1-48
  january_day_ahead.csv     the January backtest series from outputs/metrics_jan.json + actual

Run: python3 scripts/export_evaluation.py
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "evaluation"
LOCAL = timezone(timedelta(hours=5))
TURBINE_ORDER = {"1": 0, "2": 1, "plant": 2}
COLUMNS = [
    "target_time_utc",
    "target_time_local",
    "turbine",
    "p10",
    "p50",
    "p90",
    "issue_date",
    "horizon_h",
]


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_local(moment: datetime) -> str:
    return moment.astimezone(LOCAL).strftime("%Y-%m-%dT%H:%M+05:00")


def sort_key(row: dict) -> tuple:
    return (
        row["target_time_utc"],
        TURBINE_ORDER.get(row["turbine"], 9),
        int(row["horizon_h"]),
    )


def write(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{path.relative_to(ROOT)}: {len(rows)} rows")


def february() -> None:
    with (ROOT / "outputs" / "forecast_feb2026.csv").open(encoding="utf-8") as handle:
        source = list(csv.DictReader(handle))
    rows = []
    for item in source:
        target = parse_time(item["target_time_local"])
        rows.append(
            {
                "target_time_utc": iso_utc(target),
                "target_time_local": iso_local(target),
                "turbine": item["turbine"],
                "p10": item["p10"],
                "p50": item["p50"],
                "p90": item["p90"],
                "issue_date": item["issue_date"],
                "horizon_h": item["horizon_h"],
                "version": item["version"],
            }
        )
    rows.sort(key=sort_key)
    write(OUT / "february_all_horizons.csv", rows, COLUMNS + ["version"])

    start = datetime(2026, 2, 1, tzinfo=LOCAL)
    end = datetime(2026, 3, 1, tzinfo=LOCAL)
    best: dict[tuple[str, str], dict] = {}
    for row in rows:
        target = parse_time(row["target_time_local"])
        if not (start <= target < end and 1 <= int(row["horizon_h"]) <= 24):
            continue
        key = (row["target_time_utc"], row["turbine"])
        current = best.get(key)
        if current is None or row["issue_date"] > current["issue_date"]:
            best[key] = row
    day_ahead = sorted(
        ({k: v for k, v in row.items() if k != "version"} for row in best.values()),
        key=sort_key,
    )
    expected = 28 * 24 * 3
    if len(day_ahead) != expected:
        raise SystemExit(f"day-ahead: {len(day_ahead)} rows, expected {expected}")
    write(OUT / "february_day_ahead.csv", day_ahead, COLUMNS)


def january() -> None:
    metrics = json.loads((ROOT / "outputs" / "metrics_jan.json").read_text("utf-8"))
    last_issue = date.fromisoformat(metrics["period"]["to"])
    rows = []
    for item in metrics["series"]:
        target = parse_time(item["target_time_local"])
        local = target.astimezone(LOCAL)
        # The series keeps the lowest horizon per target hour: issue D-1 (h 1-24),
        # or the last issue (h 25-48) for hours after its day-ahead window.
        issue = min(local.date() - timedelta(days=1), last_issue)
        issue_moment = datetime(
            issue.year, issue.month, issue.day, 19, tzinfo=timezone.utc
        )
        horizon = int((target - issue_moment).total_seconds() // 3600) + 1
        rows.append(
            {
                "target_time_utc": iso_utc(target),
                "target_time_local": iso_local(target),
                "turbine": str(item["turbine"]),
                "p10": repr(float(item["p10"])),
                "p50": repr(float(item["p50"])),
                "p90": repr(float(item["p90"])),
                "issue_date": issue.isoformat(),
                "horizon_h": str(horizon),
                "actual": repr(float(item["actual"])),
            }
        )
    rows.sort(key=sort_key)
    write(OUT / "january_day_ahead.csv", rows, COLUMNS + ["actual"])


def main() -> None:
    february()
    january()


if __name__ == "__main__":
    main()
