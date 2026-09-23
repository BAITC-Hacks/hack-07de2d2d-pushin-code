"""Score Windcast forecasts against actual power in one command (standard library only).

    python3 scripts/score.py --actual <dir with turbine_1.csv, turbine_2.csv>
    python3 scripts/score.py --actual turbine_1.csv turbine_2.csv --json
    python3 scripts/score.py --actual tidy.csv --forecast outputs/evaluation/february_all_horizons.csv

--actual accepts
  (a) organizer SCADA files (10-minute rows, Russian headers, CRLF/BOM ok). The turbine is taken
      from the file name (turbine_1 -> 1). Raw time is local UTC+offset (default 6, see
      --scada-utc-offset); it is converted to UTC and averaged per clock hour. An hour counts
      only with >= --min-samples distinct 10-minute rows with finite wind and power, and is
      dropped as downtime when mean power < 0.02 while mean wind > 5 m/s (same rule as
      backend/windcast/data.py). plant = mean of turbines 1 and 2 when both hours count.
  (b) a tidy CSV with columns target_time_utc or target_time_local, turbine, actual.

Metrics on p50, in share of rated power (0-1): nMAE = mean|p50 - actual|,
nRMSE = sqrt(mean (p50 - actual)^2), bias = mean(p50 - actual); coverage = share of hours with
p10 <= actual <= p90; pinball = mean over q in {0.1, 0.5, 0.9} of max(q*e, (q-1)*e), e = actual - pq.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FORECAST = ROOT / "outputs" / "evaluation" / "february_day_ahead.csv"
LOCAL = timezone(timedelta(hours=5))
IDLE_POWER = 0.02
IDLE_WIND = 5.0
QUANTILES = (("p10", 0.1), ("p50", 0.5), ("p90", 0.9))
TURBINE_ORDER = {"1": 0, "2": 1, "plant": 2, "all": 3}
TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)


def parse_iso(text: str) -> datetime:
    """Parse an ISO time with Z or an offset; a naive time is taken as UTC+5."""
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL)
    return value.astimezone(timezone.utc)


def parse_raw_time(text: str) -> datetime:
    text = text.strip()
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)  # noqa: DTZ007 - SCADA local time
        except ValueError:
            continue
    raise ValueError(f"unrecognised SCADA time: {text!r}")


def to_float(text: str | None) -> float:
    try:
        value = float(str(text).strip().replace(",", "."))
    except (TypeError, ValueError):
        return math.nan
    return value


def normalise_turbine(text: str) -> str:
    value = str(text).strip().lower()
    if value in {"plant", "ves", "вэс", "park", "farm"}:
        return "plant"
    match = re.search(r"(\d+)", value)
    return match.group(1) if match else value


def read_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.reader(handle) if any(cell.strip() for cell in row)]


def find_column(header: list[str], keywords: tuple[str, ...], fallback: int) -> int:
    lowered = [cell.strip().lower() for cell in header]
    for index, cell in enumerate(lowered):
        if any(word in cell for word in keywords):
            return index
    return fallback


def scada_hourly(
    path: Path, offset_hours: int, min_samples: int
) -> tuple[dict[datetime, float], dict[str, int]]:
    """Hourly mean power keyed by UTC hour start, plus counts of dropped hours."""
    rows = read_rows(path)
    header = rows[0]
    t_col = find_column(header, ("время", "time", "date"), 1)
    w_col = find_column(header, ("ветра", "wind"), 2)
    p_col = find_column(header, ("мощност", "power"), 3)
    samples: dict[datetime, dict[datetime, tuple[float, float]]] = {}
    for row in rows[1:]:
        if len(row) <= max(t_col, w_col, p_col):
            continue
        stamp = parse_raw_time(row[t_col]).replace(tzinfo=timezone.utc)
        stamp -= timedelta(hours=offset_hours)
        wind, power = to_float(row[w_col]), to_float(row[p_col])
        if not (math.isfinite(wind) and math.isfinite(power)):
            continue
        hour = stamp.replace(minute=0, second=0, microsecond=0)
        samples.setdefault(hour, {}).setdefault(stamp, (wind, power))
    hourly: dict[datetime, float] = {}
    dropped = {"incomplete": 0, "downtime": 0}
    for hour, slots in samples.items():
        if len(slots) < min_samples:
            dropped["incomplete"] += 1
            continue
        wind = sum(v[0] for v in slots.values()) / len(slots)
        power = sum(v[1] for v in slots.values()) / len(slots)
        if power < IDLE_POWER and wind > IDLE_WIND:
            dropped["downtime"] += 1
            continue
        hourly[hour] = power
    return hourly, dropped


def load_actuals(
    paths: list[Path], offset_hours: int, min_samples: int
) -> tuple[dict[tuple[datetime, str], float], dict]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("turbine_*.csv")))
        else:
            files.append(path)
    if not files:
        raise SystemExit(f"no actual files found in {[str(p) for p in paths]}")
    actuals: dict[tuple[datetime, str], float] = {}
    info: dict = {"mode": None, "files": [str(f) for f in files]}
    per_turbine: dict[str, dict[datetime, float]] = {}
    for path in files:
        header = [c.strip().lower() for c in read_rows(path)[0]]
        if "actual" in header and "turbine" in header:
            info["mode"] = "tidy"
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    row = {k.strip().lower(): v for k, v in row.items() if k}
                    stamp = row.get("target_time_utc") or row.get("target_time_local")
                    value = to_float(row.get("actual"))
                    if stamp and math.isfinite(value):
                        key = (parse_iso(stamp), normalise_turbine(row["turbine"]))
                        actuals[key] = value
            continue
        info["mode"] = "scada"
        info["scada_utc_offset_h"] = offset_hours
        info["min_samples"] = min_samples
        turbine = normalise_turbine(path.stem)
        hourly, dropped = scada_hourly(path, offset_hours, min_samples)
        per_turbine[turbine] = hourly
        info.setdefault("dropped_hours", {})[turbine] = dropped
        for hour, value in hourly.items():
            actuals[(hour, turbine)] = value
    if "1" in per_turbine and "2" in per_turbine:
        for hour in per_turbine["1"].keys() & per_turbine["2"].keys():
            actuals[(hour, "plant")] = (
                per_turbine["1"][hour] + per_turbine["2"][hour]
            ) / 2
    return actuals, info


def load_forecast(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            stamp = row.get("target_time_utc") or row.get("target_time_local")
            item = {
                "time": parse_iso(stamp),
                "turbine": normalise_turbine(row["turbine"]),
                "p10": float(row["p10"]),
                "p50": float(row["p50"]),
                "p90": float(row["p90"]),
            }
            if row.get("horizon_h"):
                item["horizon_h"] = int(row["horizon_h"])
            rows.append(item)
    return rows


def metrics(pairs: list[tuple[dict, float]]) -> dict:
    n = len(pairs)
    if n == 0:
        return {"hours": 0}
    errors = [f["p50"] - a for f, a in pairs]
    pinball = {}
    for column, q in QUANTILES:
        losses = [max(q * (a - f[column]), (q - 1) * (a - f[column])) for f, a in pairs]
        pinball[column] = sum(losses) / n
    return {
        "hours": n,
        "nmae": sum(abs(e) for e in errors) / n,
        "nrmse": math.sqrt(sum(e * e for e in errors) / n),
        "bias": sum(errors) / n,
        "coverage_p10_p90": sum(1 for f, a in pairs if f["p10"] <= a <= f["p90"]) / n,
        "pinball_mean": sum(pinball.values()) / len(pinball),
        "pinball": pinball,
    }


def score(forecast: list[dict], actuals: dict[tuple[datetime, str], float]) -> dict:
    pairs: dict[str, list[tuple[dict, float]]] = {}
    for row in forecast:
        value = actuals.get((row["time"], row["turbine"]))
        if value is None:
            continue
        groups = [row["turbine"], "all"]
        if "horizon_h" in row:
            band = "h1-24" if row["horizon_h"] <= 24 else "h25-48"
            groups += [f"{row['turbine']}|{band}", f"all|{band}"]
        for group in groups:
            pairs.setdefault(group, []).append((row, value))
    result: dict = {"by_turbine": {}, "by_horizon": {}}
    for group, items in pairs.items():
        if "|" in group:
            turbine, band = group.split("|")
            result["by_horizon"].setdefault(band, {})[turbine] = metrics(items)
        else:
            result["by_turbine"][group] = metrics(items)
    result["forecast_rows"] = len(forecast)
    result["headline"] = "plant" if "plant" in result["by_turbine"] else "all"
    return result


def order(names) -> list[str]:
    return sorted(names, key=lambda name: (TURBINE_ORDER.get(name, 9), name))


def table(result: dict) -> str:
    head = f"{'group':<16}{'hours':>6}{'nMAE':>9}{'nRMSE':>9}{'bias':>9}{'P10-P90':>9}{'pinball':>9}"
    lines = [head, "-" * len(head)]

    def line(label: str, m: dict) -> str:
        if not m.get("hours"):
            return f"{label:<16}{0:>6}"
        return (
            f"{label:<16}{m['hours']:>6}{m['nmae']:>9.2%}{m['nrmse']:>9.2%}"
            f"{m['bias']:>+9.2%}{m['coverage_p10_p90']:>9.1%}{m['pinball_mean']:>9.4f}"
        )

    for turbine in order(result["by_turbine"]):
        lines.append(line(f"turbine {turbine}", result["by_turbine"][turbine]))
    for band in sorted(result["by_horizon"]):
        for turbine in order(result["by_horizon"][band]):
            lines.append(line(f"{turbine} {band}", result["by_horizon"][band][turbine]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--actual", nargs="+", required=True, type=Path)
    parser.add_argument("--forecast", type=Path, default=DEFAULT_FORECAST)
    parser.add_argument("--scada-utc-offset", type=int, default=6)
    parser.add_argument("--min-samples", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    actuals, info = load_actuals(args.actual, args.scada_utc_offset, args.min_samples)
    forecast = load_forecast(args.forecast)
    result = score(forecast, actuals)
    result["forecast"] = str(args.forecast)
    result["actual"] = info
    result["units"] = "share of rated power (0-1); plant = mean of turbines 1 and 2"
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"forecast: {args.forecast}  ({result['forecast_rows']} rows)")
    print(f"actual:   {info['mode']} {', '.join(info['files'])}")
    if info["mode"] == "scada":
        print(
            f"          SCADA time = UTC+{args.scada_utc_offset}, hour kept with >= "
            f"{args.min_samples}/6 rows, downtime dropped: {info.get('dropped_hours')}"
        )
    print(table(result))
    headline = result["by_turbine"].get(result["headline"], {})
    if headline.get("hours"):
        print(
            f"\n{result['headline']}: nMAE {headline['nmae']:.2%} of rated power "
            f"over {headline['hours']} hours"
        )
    else:
        print(
            "\nno forecast hour matched an actual hour: check dates and --scada-utc-offset"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
