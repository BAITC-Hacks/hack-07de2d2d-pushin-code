"""Normalize the supplied 10-minute SCADA CSV files into an hourly UTC dataset."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from windcast.config import scada_utc_offset_hours
from windcast.paths import processed_dir, raw_dir
from windcast.timeline import (
    issue_time_utc,
    live_times,
    run_available_at,
    target_times_utc,
)

TIMESTAMP_COLUMN = "Статистическое время"
WIND_COLUMN = "Средняя скорость ветра(m/s)"
POWER_COLUMN = "Нормализованная активная мощность"
TEMPERATURE_COLUMN = "Средняя температура окружающей среды(°C)"
HOURLY_COLUMNS = ["ts_utc", "turbine", "wind_ms", "power", "temp_c", "valid"]
SAMPLES_PER_HOUR = 6
IDLE_POWER_THRESHOLD = 0.02
IDLE_WIND_THRESHOLD = 5.0


@dataclass(frozen=True)
class DataQualityReport:
    """Counts emitted whenever the raw SCADA files are normalized."""

    output_path: Path
    rows_by_turbine: dict[str, int]
    valid_fraction_by_turbine: dict[str, float]


def hourly_path() -> Path:
    return processed_dir() / "hourly.parquet"


def _read_turbine(turbine: str, offset_hours: int) -> pd.DataFrame:
    source = raw_dir() / f"turbine_{turbine}.csv"
    if not source.is_file():
        raise FileNotFoundError(f"Не найден исходный SCADA CSV: {source}")
    frame = pd.read_csv(
        source,
        usecols=[TIMESTAMP_COLUMN, WIND_COLUMN, POWER_COLUMN, TEMPERATURE_COLUMN],
    ).rename(
        columns={
            TIMESTAMP_COLUMN: "ts_local",
            WIND_COLUMN: "wind_ms",
            POWER_COLUMN: "power",
            TEMPERATURE_COLUMN: "temp_c",
        }
    )
    frame["ts_local"] = pd.to_datetime(frame["ts_local"], errors="raise")
    frame["ts_utc"] = pd.to_datetime(
        frame["ts_local"] - pd.Timedelta(hours=offset_hours), utc=True
    )
    for column in ("wind_ms", "power", "temp_c"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["finite_sample"] = np.isfinite(frame[["wind_ms", "power", "temp_c"]]).all(
        axis=1
    )
    frame["hour"] = frame["ts_utc"].dt.floor("h")
    return frame


def _blackout_mask(hours: pd.Series, turbine: str, offset_hours: int) -> pd.Series:
    if turbine != "1":
        return pd.Series(False, index=hours.index)
    start = pd.Timestamp("2024-05-18T00:00:00") - pd.Timedelta(hours=offset_hours)
    end = pd.Timestamp("2024-07-17T23:59:59") - pd.Timedelta(hours=offset_hours)
    return (hours >= start.tz_localize("UTC")) & (hours <= end.tz_localize("UTC"))


def _hourly_turbine(turbine: str, offset_hours: int) -> pd.DataFrame:
    raw = _read_turbine(turbine, offset_hours)
    aggregate = (
        raw.groupby("hour", as_index=False)
        .agg(
            wind_ms=("wind_ms", "mean"),
            power=("power", "mean"),
            temp_c=("temp_c", "mean"),
            sample_count=("ts_utc", "size"),
            slot_count=("ts_utc", "nunique"),
            finite_count=("finite_sample", "sum"),
        )
        .rename(columns={"hour": "ts_utc"})
    )
    hourly_grid = pd.DataFrame(
        {
            "ts_utc": pd.date_range(
                aggregate["ts_utc"].min(), aggregate["ts_utc"].max(), freq="h", tz="UTC"
            )
        }
    )
    hourly = hourly_grid.merge(
        aggregate, on="ts_utc", how="left", validate="one_to_one"
    )
    complete = (
        (hourly["sample_count"] == SAMPLES_PER_HOUR)
        & (hourly["slot_count"] == SAMPLES_PER_HOUR)
        & (hourly["finite_count"] == SAMPLES_PER_HOUR)
    )
    idle = (hourly["power"] < IDLE_POWER_THRESHOLD) & (
        hourly["wind_ms"] > IDLE_WIND_THRESHOLD
    )
    blackout = _blackout_mask(hourly["ts_utc"], turbine, offset_hours)
    hourly["turbine"] = turbine
    hourly["valid"] = (complete & ~idle & ~blackout).astype(bool)
    return hourly[HOURLY_COLUMNS]


def hourly_quality_summary(hourly: pd.DataFrame) -> pd.DataFrame:
    """Return printable per-turbine row counts and valid-hour fractions."""
    summary = (
        hourly.groupby("turbine", as_index=False)
        .agg(hours=("valid", "size"), valid_hours=("valid", "sum"))
        .sort_values("turbine", ignore_index=True)
    )
    summary["valid_hours"] = summary["valid_hours"].astype(int)
    summary["valid_fraction"] = (summary["valid_hours"] / summary["hours"]).round(6)
    return summary


def _report(hourly: pd.DataFrame, output_path: Path) -> DataQualityReport:
    summary = hourly_quality_summary(hourly)
    return DataQualityReport(
        output_path=output_path,
        rows_by_turbine=dict(zip(summary["turbine"], summary["hours"], strict=True)),
        valid_fraction_by_turbine=dict(
            zip(summary["turbine"], summary["valid_fraction"], strict=True)
        ),
    )


def format_quality_report(report: DataQualityReport) -> str:
    """Format the acceptance-check output without hiding real row counts."""
    lines = [f"hourly parquet: {report.output_path}"]
    for turbine in sorted(report.rows_by_turbine):
        fraction = report.valid_fraction_by_turbine[turbine]
        lines.append(
            f"turbine {turbine}: hours={report.rows_by_turbine[turbine]} valid_fraction={fraction:.4f}"
        )
    return "\n".join(lines)


def build_hourly_dataset(offset_hours: int | None = None) -> DataQualityReport:
    """Build ``data/processed/hourly.parquet`` from both supplied raw CSV files."""
    offset = scada_utc_offset_hours(offset_hours)
    hourly = pd.concat(
        [_hourly_turbine("1", offset), _hourly_turbine("2", offset)], ignore_index=True
    ).sort_values(["turbine", "ts_utc"], ignore_index=True)
    output = hourly_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(hourly, preserve_index=False)
    metadata = dict(table.schema.metadata or {})
    metadata[b"scada_utc_offset_h"] = str(offset).encode()
    pq.write_table(table.replace_schema_metadata(metadata), output)
    return _report(hourly, output)


def load_hourly_dataset(
    *, rebuild: bool = False, offset_hours: int | None = None
) -> pd.DataFrame:
    """Load normalized SCADA data; ``rebuild`` lets T3 compare offsets 5 and 6."""
    output = hourly_path()
    requested_offset = scada_utc_offset_hours(offset_hours)
    stored_offset: str | None = None
    if output.is_file():
        stored_offset = (
            (pq.read_metadata(output).metadata or {})
            .get(b"scada_utc_offset_h", b"")
            .decode()
        )
    if rebuild or stored_offset != str(requested_offset):
        build_hourly_dataset(offset_hours=requested_offset)
    hourly = pd.read_parquet(output, columns=HOURLY_COLUMNS)
    hourly["ts_utc"] = pd.to_datetime(hourly["ts_utc"], utc=True)
    hourly["turbine"] = hourly["turbine"].astype(str)
    hourly["valid"] = hourly["valid"].astype(bool)
    return hourly


def check_data(issue_date: str, weather: dict) -> dict[str, object]:
    """Validate the weather payload handed from T2 before model inference.

    This does not put SCADA into forecast features. It only checks the 48-hour
    weather contract and proves every supplied run was published by the
    requested issue moment.
    """
    if issue_date == "live":
        supplied_issue_time = pd.to_datetime(
            weather.get("issue_time_utc"), utc=True, errors="coerce"
        )
        issue_time = (
            supplied_issue_time.to_pydatetime()
            if not pd.isna(supplied_issue_time)
            else None
        )
        expected_targets = (
            pd.DatetimeIndex(live_times(issue_time)[1]) if issue_time else None
        )
    else:
        issue_time = issue_time_utc(issue_date)
        expected_targets = pd.DatetimeIndex(target_times_utc(issue_date))
    hourly = weather.get("hourly")
    missing_hours = 48
    coverage_ok = False
    targets_ok = False
    values_ok = False
    if isinstance(hourly, pd.DataFrame):
        expected = set(range(1, 49))
        h_values = (
            pd.to_numeric(hourly["h"], errors="coerce")
            if "h" in hourly
            else pd.Series(index=hourly.index, dtype="float64")
        )
        integral_h = h_values.notna() & (h_values % 1 == 0)
        present = set(h_values.loc[integral_h].astype(int))
        missing_hours = len(expected - present)
        coverage_ok = (
            len(hourly) == 48
            and present == expected
            and h_values.is_unique
            and integral_h.all()
        )
        if "target_time_utc" in hourly and expected_targets is not None:
            targets = pd.to_datetime(
                hourly["target_time_utc"], utc=True, errors="coerce"
            )
            expected_by_h = pd.Series(expected_targets, index=range(1, 49))
            targets_ok = bool(
                len(targets) == 48
                and targets.notna().all()
                and coverage_ok
                and pd.DatetimeIndex(targets).equals(
                    pd.DatetimeIndex(h_values.map(expected_by_h))
                )
            )
        required = {"wind_100m_ms", "temp_c"}
        if required.issubset(hourly.columns):
            values = hourly[["wind_100m_ms", "temp_c"]].apply(
                pd.to_numeric, errors="coerce"
            )
            values_ok = bool(np.isfinite(values).all().all())
        init_values = hourly.get("init_time_utc", pd.Series(dtype="object"))
    else:
        init_values = pd.Series(dtype="object")
    parsed_inits = pd.to_datetime(init_values, utc=True, errors="coerce")
    valid_inits = bool(
        issue_time is not None
        and not parsed_inits.empty
        and parsed_inits.notna().all()
        and all(
            # Live snapshots come from the Forecast API, which serves only published runs;
            # their init is the fetch time. Archive runs must be published by T (§2).
            (
                init.to_pydatetime() <= issue_time
                if issue_date == "live"
                else run_available_at(init.to_pydatetime()) <= issue_time
            )
            for init in parsed_inits
        )
    )
    notes: list[str] = []
    if missing_hours:
        notes.append(f"В погодном окне отсутствует часов: {missing_hours}")
    if not coverage_ok:
        notes.append(
            "Погодное окно должно содержать ровно 48 уникальных горизонтов 1–48"
        )
    if not targets_ok:
        notes.append("Целевые UTC-часы погоды не совпадают с горизонтом выпуска")
    if not values_ok:
        notes.append(
            "В погоде отсутствуют конечные значения ветра 100 м или температуры"
        )
    if not valid_inits:
        notes.append(
            "Есть погодный прогон, опубликованный позже момента выпуска или без времени инициализации"
        )
    if not notes:
        notes.append("48 погодных часов и все прогоны опубликованы к моменту выпуска")
    return {
        "ok": coverage_ok and targets_ok and values_ok and valid_inits,
        "missing_hours": missing_hours,
        "runs_before_issue": valid_inits,
        "notes": notes,
    }


def main() -> None:
    """CLI acceptance check: generate the parquet file and print actual quality."""
    print(format_quality_report(build_hourly_dataset()))


if __name__ == "__main__":
    main()
