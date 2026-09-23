"""Raw turbine observations and hourly training-label construction.

Raw timestamps have no timezone in the competition files.  This module never
silently assumes that a daylight-saving/offset transition was harmless: callers
must select a source timezone and, when necessary, an explicit localisation
policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

RAW_COLUMNS = {
    "timestamp": "Статистическое время",
    "wind": "Средняя скорость ветра(m/s)",
    "power": "Нормализованная активная мощность",
    "temperature": "Средняя температура окружающей среды(°C)",
}
TURBINE_IDS = frozenset({"1", "2"})

HOURLY_COLUMNS = [
    "turbine_id",
    "target_time",
    "power",
    "observed_wind",
    "observed_temperature",
    "n_samples",
    "complete_hour",
]


@dataclass(frozen=True)
class ObservationReport:
    """Diagnostics emitted alongside complete hourly labels.

    ``summary`` is one row per turbine and distinguishes all incomplete hours,
    entirely missing hours, and hours with some (but fewer than six) valid
    observations. ``incomplete_hours`` contains every non-complete hour in the
    observed time span. ``invalid_rows`` identifies rows excluded before the
    hourly calculation.
    """

    summary: pd.DataFrame
    incomplete_hours: pd.DataFrame
    invalid_rows: pd.DataFrame

    @property
    def partial_hours(self) -> pd.DataFrame:
        """Backward-compatible alias; use ``incomplete_hours`` for clarity."""

        return self.incomplete_hours


def _localize_to_utc(
    timestamps: pd.Series,
    *,
    timezone: str,
    ambiguous: Any,
    nonexistent: Any,
) -> pd.Series:
    """Localize naive civil timestamps and return UTC timestamps.

    Pandas' ``ambiguous`` and ``nonexistent`` options are intentionally passed
    through. Their safe defaults are ``'raise'``; e.g. Asia/Almaty's 2024
    historical offset transition is then surfaced rather than guessed.
    """

    try:
        return timestamps.dt.tz_localize(
            timezone, ambiguous=ambiguous, nonexistent=nonexistent
        ).dt.tz_convert("UTC")
    except Exception as exc:
        raise ValueError(
            "Cannot localize raw observation timestamps using timezone "
            f"{timezone!r} (ambiguous={ambiguous!r}, nonexistent={nonexistent!r}). "
            "The raw CSV has undocumented civil timestamps; choose the source "
            "timezone and any transition policy explicitly."
        ) from exc


def _empty_hourly() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "turbine_id": pd.Series(dtype="string"),
            "target_time": pd.Series(dtype="datetime64[ns, UTC]"),
            "power": pd.Series(dtype="float64"),
            "observed_wind": pd.Series(dtype="float64"),
            "observed_temperature": pd.Series(dtype="float64"),
            "n_samples": pd.Series(dtype="int64"),
            "complete_hour": pd.Series(dtype="bool"),
        }
    )


def read_observations(
    paths: Mapping[str, str | Path],
    timezone: str = "Asia/Almaty",
    *,
    ambiguous: Any = "raise",
    nonexistent: Any = "raise",
    require_complete: bool = True,
) -> tuple[pd.DataFrame, ObservationReport]:
    """Read raw CSVs and create UTC-hourly turbine labels.

    A complete label has exactly six *unique, valid and aligned* 10-minute
    observations. Validity requires finite wind, temperature and power, with
    normalized power in ``[0, 1]``. Duplicate timestamps are excluded, never
    averaged. By default incomplete hours are reported but not returned.

    ``timezone`` is an explicit assumption because the source does not document
    it. With the default strict transition policies, an ambiguous or nonexistent
    local timestamp raises a clear error; callers may pass a documented pandas
    policy (for example ``ambiguous=False``) only when they know the convention.
    """

    if not paths:
        raise ValueError("paths must contain at least one turbine CSV")

    hourly_parts: list[pd.DataFrame] = []
    partial_parts: list[pd.DataFrame] = []
    invalid_parts: list[pd.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []

    for raw_turbine_id, path in paths.items():
        turbine_id = str(raw_turbine_id)
        if turbine_id not in TURBINE_IDS:
            raise ValueError(f"turbine_id must be one of {sorted(TURBINE_IDS)}, got {turbine_id!r}")
        frame = pd.read_csv(Path(path))
        missing_columns = set(RAW_COLUMNS.values()).difference(frame.columns)
        if missing_columns:
            raise ValueError(f"{path}: missing required raw CSV columns: {sorted(missing_columns)}")

        source_rows = len(frame)
        parsed_time = pd.to_datetime(frame[RAW_COLUMNS["timestamp"]], errors="coerce")
        numeric = pd.DataFrame(
            {
                "power": pd.to_numeric(frame[RAW_COLUMNS["power"]], errors="coerce"),
                "observed_wind": pd.to_numeric(frame[RAW_COLUMNS["wind"]], errors="coerce"),
                "observed_temperature": pd.to_numeric(
                    frame[RAW_COLUMNS["temperature"]], errors="coerce"
                ),
            }
        )

        time_valid = parsed_time.notna() & (parsed_time == parsed_time.dt.floor("10min"))
        numeric_valid = (
            np.isfinite(numeric["power"])
            & np.isfinite(numeric["observed_wind"])
            & np.isfinite(numeric["observed_temperature"])
            & numeric["power"].between(0.0, 1.0)
        )
        duplicate = parsed_time.notna() & parsed_time.duplicated(keep=False)
        valid = time_valid & numeric_valid & ~duplicate

        reason = np.select(
            [
                ~time_valid.to_numpy(),
                duplicate.to_numpy(),
                ~numeric_valid.to_numpy(),
            ],
            ["invalid_or_unaligned_timestamp", "duplicate_timestamp", "invalid_measurement"],
            default="",
        )
        invalid = frame.loc[~valid, [RAW_COLUMNS["timestamp"]]].copy()
        invalid.columns = ["raw_timestamp"]
        invalid.insert(0, "turbine_id", turbine_id)
        invalid["reason"] = reason[~valid.to_numpy()]
        invalid_parts.append(invalid)

        # Localize only timestamp-valid rows. Invalid numeric rows still define
        # the diagnostic time span, but never contribute to a label.
        localizable = parsed_time[time_valid]
        utc_localizable = _localize_to_utc(
            localizable, timezone=timezone, ambiguous=ambiguous, nonexistent=nonexistent
        )
        utc_time = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
        utc_time.loc[localizable.index] = utc_localizable

        observations = numeric.loc[valid].copy()
        observations.insert(0, "target_time", utc_time.loc[valid])
        observations["hour"] = observations["target_time"].dt.floor("h")

        # A range expansion exposes total missing hours as diagnostics, instead
        # of reporting only partial hours that happened to contain a row.
        available_times = utc_time.dropna()
        if available_times.empty:
            summary_rows.append(
                {
                    "turbine_id": turbine_id,
                    "source_rows": source_rows,
                    "valid_rows": int(valid.sum()),
                    "invalid_rows": int((~valid).sum()),
                    "candidate_hours": 0,
                    "complete_hours": 0,
                    "incomplete_hours": 0,
                    "total_missing_hours": 0,
                    "partially_observed_hours": 0,
                    "missing_slots": 0,
                }
            )
            continue

        hours = pd.date_range(
            available_times.min().floor("h"), available_times.max().floor("h"), freq="h", tz="UTC"
        )
        grouped = observations.groupby("hour", sort=True).agg(
            power=("power", "mean"),
            observed_wind=("observed_wind", "mean"),
            observed_temperature=("observed_temperature", "mean"),
            n_samples=("target_time", "nunique"),
        )
        labels = grouped.reindex(hours)
        labels.index.name = "target_time"
        labels["n_samples"] = labels["n_samples"].fillna(0).astype("int64")
        labels["complete_hour"] = labels["n_samples"].eq(6)
        labels.insert(0, "turbine_id", turbine_id)
        labels = labels.reset_index()[HOURLY_COLUMNS]

        partial = labels.loc[
            ~labels["complete_hour"], ["turbine_id", "target_time", "n_samples", "complete_hour"]
        ].copy()
        partial["missing_slots"] = 6 - partial["n_samples"]
        partial_parts.append(partial)

        summary_rows.append(
            {
                "turbine_id": turbine_id,
                "source_rows": source_rows,
                "valid_rows": int(valid.sum()),
                "invalid_rows": int((~valid).sum()),
                "candidate_hours": len(labels),
                "complete_hours": int(labels["complete_hour"].sum()),
                "incomplete_hours": int((~labels["complete_hour"]).sum()),
                "total_missing_hours": int(labels["n_samples"].eq(0).sum()),
                "partially_observed_hours": int(
                    ((labels["n_samples"] > 0) & (~labels["complete_hour"])).sum()
                ),
                "missing_slots": int((6 - labels["n_samples"]).sum()),
            }
        )
        hourly_parts.append(labels if not require_complete else labels.loc[labels["complete_hour"]])

    hourly = (
        pd.concat(hourly_parts, ignore_index=True).sort_values(["turbine_id", "target_time"])
        if hourly_parts
        else _empty_hourly()
    )
    if not hourly.empty:
        hourly = hourly.reset_index(drop=True)
        hourly["turbine_id"] = hourly["turbine_id"].astype("string")
    report = ObservationReport(
        summary=pd.DataFrame(summary_rows),
        incomplete_hours=pd.concat(partial_parts, ignore_index=True)
        if partial_parts
        else pd.DataFrame(
            columns=["turbine_id", "target_time", "n_samples", "complete_hour", "missing_slots"]
        ),
        invalid_rows=pd.concat(invalid_parts, ignore_index=True)
        if invalid_parts
        else pd.DataFrame(columns=["turbine_id", "raw_timestamp", "reason"]),
    )
    return hourly, report
