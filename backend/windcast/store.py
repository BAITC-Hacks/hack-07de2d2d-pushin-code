"""Published issues on disk (contract §3, §5.1, §6.2, §7) — the only files the API reads.

- outputs/forecasts/{D}.json — issue record with every version (live: outputs/live/latest.json);
- outputs/forecasts/{D}.csv — latest version only, §7 columns (live: outputs/live/latest.csv);
- outputs/traces/{D}.jsonl — events of the last full issue (live: outputs/live/latest_trace.jsonl);
- outputs/forecast_feb2026.csv — the 29 test issues in one file.

Every write is atomic (temp file in the same directory + os.replace): the API reads while the
agent writes.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import math
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from windcast import paths, timeline

log = logging.getLogger(__name__)

LIVE = "live"
CSV_COLUMNS = (
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
)
COMBINED_CSV = "forecast_feb2026.csv"
_ACTUALS: dict = {}


# ---------- paths ----------
def issue_key(issue_date: str | date) -> str:
    """ "live" or the canonical YYYY-MM-DD; ValueError (Russian) for anything else."""
    if str(issue_date) == LIVE:
        return LIVE
    return timeline.parse_issue_date(issue_date).isoformat()


def record_path(issue_date: str | date) -> Path:
    key = issue_key(issue_date)
    if key == LIVE:
        return paths.live_dir() / "latest.json"
    return paths.forecasts_dir() / f"{key}.json"


def csv_path(issue_date: str | date) -> Path:
    key = issue_key(issue_date)
    if key == LIVE:
        return paths.live_dir() / "latest.csv"
    return paths.forecasts_dir() / f"{key}.csv"


def trace_path(issue_date: str | date) -> Path:
    key = issue_key(issue_date)
    if key == LIVE:
        return paths.live_dir() / "latest_trace.jsonl"
    return paths.traces_dir() / f"{key}.jsonl"


def combined_csv_path() -> Path:
    return paths.outputs_dir() / COMBINED_CSV


def relative(path: Path) -> str:
    """Path as shown to people: relative to the repository root when possible."""
    try:
        return str(Path(path).resolve().relative_to(paths.root().resolve()))
    except ValueError:
        return str(path)


def atomic_write_text(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return path


def now_local() -> str:
    return timeline.iso_local(datetime.now(timezone.utc))


# ---------- records ----------
def load_record(issue_date: str | date) -> dict | None:
    """The issue record (§5.1) or None when the issue was never published."""
    try:
        return json.loads(record_path(issue_date).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def version_entry(record: dict, version: int | str = "latest") -> dict | None:
    """One version of a record: "latest" or its number; None when absent."""
    if not record or not record.get("versions"):
        return None
    key = record.get("latest_version") if version == "latest" else version
    return record["versions"].get(str(key))


def new_record(issue_date: str, issue_time_utc: datetime, mode: str) -> dict:
    return {
        "issue_date": issue_key(issue_date),
        "issue_time_local": timeline.iso_local(issue_time_utc),
        "issue_time_utc": timeline.iso_utc(issue_time_utc),
        "latest_version": 0,
        "mode": mode,
        "recorded_at": now_local(),
        "versions": {},
    }


def write_record(record: dict) -> Path:
    text = json.dumps(record, ensure_ascii=False, indent=1)
    return atomic_write_text(record_path(record["issue_date"]), text + "\n")


def save_version(
    record: dict, entry: dict, init_by_h: dict[int, str] | None = None
) -> tuple[Path, Path]:
    """Add a version to the record, write the record and the CSV of the latest version."""
    number = int(entry["version"])
    record.setdefault("versions", {})[str(number)] = entry
    record["latest_version"] = max(int(k) for k in record["versions"])
    record["recorded_at"] = now_local()
    rec_path = write_record(record)
    latest = record["versions"][str(record["latest_version"])]
    inits = init_by_h if latest is entry else None
    out_csv = atomic_write_text(
        csv_path(record["issue_date"]), csv_text(record, latest, inits)
    )
    key = record["issue_date"]
    if key != LIVE and timeline.in_test_range(key):
        write_combined_csv()
    return rec_path, out_csv


# ---------- rows (§6.2) ----------
def _number(value, digits: int):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else round(value, digits)


def forecast_rows(frame: pd.DataFrame) -> list[dict]:
    """§6.2 rows from a predict() frame; actual from data/processed when it covers the hour."""
    actuals = load_actuals()
    rows = []
    for rec in frame.itertuples(index=False):
        target = pd.Timestamp(rec.target_time_utc)
        turbine = str(rec.turbine)
        rows.append(
            {
                "h": int(rec.h),
                "target_time_local": timeline.iso_local(target),
                "turbine": turbine,
                "p10": _number(rec.p10, 4),
                "p50": _number(rec.p50, 4),
                "p90": _number(rec.p90, 4),
                "wind_fc_ms": _number(rec.wind_fc_ms, 2),
                "temp_fc_c": _number(rec.temp_fc_c, 2),
                "actual": actuals.get((timeline.iso_utc(target), turbine)),
            }
        )
    return rows


def _turbine_key(value) -> str:
    text = str(value).strip()
    try:
        return str(int(float(text)))
    except ValueError:
        match = re.search(r"(\d+)$", text)
        return match.group(1) if match else text


def load_actuals() -> dict[tuple[str, str], float]:
    """{(hour "YYYY-MM-DDTHH:MMZ", turbine): power} from data/processed/hourly.parquet.

    Valid rows only; plant = mean of both turbines when both are valid. Empty when the file
    is missing or unreadable — the forecast never fails because of the fact.
    """
    path = paths.processed_dir() / "hourly.parquet"
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    if _ACTUALS.get("stamp") == stamp:
        return _ACTUALS["value"]
    value: dict[tuple[str, str], float] = {}
    try:
        df = pd.read_parquet(path)
        if "valid" in df.columns:
            df = df[df["valid"].fillna(False).astype(bool)]
        df = pd.DataFrame(
            {
                "ts": pd.to_datetime(df["ts_utc"], utc=True).dt.floor("h"),
                "turbine": df["turbine"].map(_turbine_key),
                "power": pd.to_numeric(df["power"], errors="coerce"),
            }
        ).dropna()
        wide = df.groupby(["ts", "turbine"])["power"].mean().unstack("turbine")
        if {"1", "2"} <= set(wide.columns):
            wide["plant"] = wide[["1", "2"]].mean(axis=1, skipna=False)
        for turbine in wide.columns:
            series = wide[turbine].dropna()
            for ts, power in zip(series.index, series.to_numpy()):
                value[(timeline.iso_utc(ts), str(turbine))] = round(float(power), 4)
    except Exception as exc:  # noqa: BLE001 — the fact is optional, never break a forecast
        log.warning("actuals unavailable from %s: %s", path, exc)
        value = {}
    _ACTUALS.update(stamp=stamp, value=value)
    return value


# ---------- CSV (§7) ----------
def hour_range(hours: str) -> tuple[int, int] | None:
    match = re.match(r"\s*(\d+)\s*[-–—]\s*(\d+)\s*$", str(hours))
    return (int(match.group(1)), int(match.group(2))) if match else None


def init_by_h_from_runs(weather_runs: list[dict]) -> dict[int, str]:
    """Hour -> init of the run that covered it, from a version's weather_runs."""
    out: dict[int, str] = {}
    for run in weather_runs or []:
        span = hour_range(run.get("hours", ""))
        if span and run.get("init_utc"):
            for h in range(span[0], span[1] + 1):
                out[h] = max(out.get(h, ""), str(run["init_utc"]))
    return out


def csv_text(record: dict, entry: dict, init_by_h: dict[int, str] | None = None) -> str:
    """§7 CSV of one version. init_by_h: exact init per hour; else from weather_runs."""
    inits = init_by_h or init_by_h_from_runs(entry.get("weather_runs") or [])
    fallback = max(inits.values()) if inits else ""
    issue_date = record["issue_date"]
    if issue_date == LIVE:
        issue_date = record["issue_time_local"][:10]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in entry["rows"]:
        writer.writerow(
            [
                issue_date,
                record["issue_time_local"],
                row["target_time_local"],
                row["h"],
                row["turbine"],
                f"{row['p10']:.4f}",
                f"{row['p50']:.4f}",
                f"{row['p90']:.4f}",
                "" if row.get("wind_fc_ms") is None else f"{row['wind_fc_ms']:.2f}",
                inits.get(int(row["h"]), fallback),
                entry["version"],
            ]
        )
    return buf.getvalue()


def write_combined_csv(
    start: date = timeline.TEST_FROM, end: date = timeline.TEST_TO
) -> tuple[Path, int]:
    """outputs/forecast_feb2026.csv from the per-issue CSVs present in the test range."""
    lines = [",".join(CSV_COLUMNS)]
    count = 0
    for day in timeline.issue_dates(start, end):
        try:
            text = csv_path(day).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        lines.extend(line for line in text.splitlines()[1:] if line)
        count += 1
    path = atomic_write_text(combined_csv_path(), "\n".join(lines) + "\n")
    return path, count


# ---------- traces ----------
def write_trace(issue_date: str | date, events: list[dict]) -> Path:
    text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    return atomic_write_text(trace_path(issue_date), text)


def load_trace(issue_date: str | date) -> list[dict]:
    try:
        text = trace_path(issue_date).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]
