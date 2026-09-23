"""Past-only features and direct 48-hour labels from turbine observations.

An origin is the *start* of the first predicted hour.  Thus an observation at
``target_time == origin - 1 hour`` is usable (its interval has ended), while
an observation at ``target_time >= origin`` is never used as a feature.  This
module deliberately has no weather or network dependency.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from .data import HOURLY_COLUMNS, TURBINE_IDS

HORIZON = 48
MAX_LOOKBACK_HOURS = 168
LAG_HOURS = (1, 2, 3, 6, 12, 24, 48, 72, 168)
ROLLING_WINDOWS = (6, 24, 48, 168)
_MEASUREMENTS = {
    "power": "power",
    "wind": "observed_wind",
    "temperature": "observed_temperature",
}

TARGET_COLUMNS = tuple(f"y_{step:02d}" for step in range(1, HORIZON + 1))
CAT_FEATURES = ("turbine_id",)

_LAG_FEATURES = tuple(f"{name}_lag_{lag}" for name in _MEASUREMENTS for lag in LAG_HOURS)
_ROLLING_FEATURES = tuple(
    f"{name}_{stat}_{window}"
    for name in _MEASUREMENTS
    for window in ROLLING_WINDOWS
    for stat in ("mean", "std", "min", "max")
)
_COVERAGE_FEATURES = tuple(f"history_coverage_{window}" for window in ROLLING_WINDOWS)
_CALENDAR_FEATURES = (
    "target_hour_utc",
    "target_month_utc",
    "origin_hour_sin",
    "origin_hour_cos",
    "origin_dayofweek_sin",
    "origin_dayofweek_cos",
    "origin_dayofyear_sin",
    "origin_dayofyear_cos",
    "origin_month_sin",
    "origin_month_cos",
)
NUMERIC_FEATURES = _LAG_FEATURES + _ROLLING_FEATURES + _COVERAGE_FEATURES + _CALENDAR_FEATURES
FEATURE_COLUMNS = NUMERIC_FEATURES + CAT_FEATURES


def _utc_hour(value: object, name: str) -> pd.Timestamp:
    """Return one UTC, whole-hour timestamp without guessing a timezone."""

    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a timezone-aware UTC timestamp.") from exc
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware; naive timestamps are not allowed.")
    stamp = stamp.tz_convert("UTC")
    if stamp != stamp.floor("h"):
        raise ValueError(f"{name} must be aligned to an exact UTC hour.")
    return stamp


def _normalise_origins(origins: Iterable[object]) -> pd.DatetimeIndex:
    try:
        values = list(origins)
    except TypeError as exc:
        raise ValueError("origins must be an iterable of timezone-aware UTC hours.") from exc
    if not values:
        return pd.DatetimeIndex([], tz="UTC")
    result = pd.DatetimeIndex([_utc_hour(value, "origin") for value in values]).as_unit("ns")
    if result.has_duplicates:
        raise ValueError("origins cannot contain duplicate timestamps.")
    return result.sort_values()


def _validate_hourly(hourly: pd.DataFrame) -> pd.DataFrame:
    """Validate and normalize the complete-hourly observation contract."""

    if not isinstance(hourly, pd.DataFrame):
        raise TypeError("hourly must be a pandas DataFrame.")
    missing = set(HOURLY_COLUMNS).difference(hourly.columns)
    if missing:
        raise ValueError(f"hourly is missing required columns: {sorted(missing)}")
    frame = hourly.loc[:, HOURLY_COLUMNS].copy()

    target_dtype = frame["target_time"].dtype
    if not isinstance(target_dtype, pd.DatetimeTZDtype) or str(target_dtype.tz) != "UTC":
        raise ValueError(
            "hourly.target_time must have dtype datetime64[ns, UTC]; do not pass naive times."
        )
    if frame["target_time"].isna().any():
        raise ValueError("hourly.target_time cannot contain missing timestamps.")
    if not frame["target_time"].eq(frame["target_time"].dt.floor("h")).all():
        raise ValueError("hourly.target_time must be aligned to exact UTC hours.")

    frame["turbine_id"] = frame["turbine_id"].astype("string")
    invalid_turbines = set(frame["turbine_id"].dropna()).difference(TURBINE_IDS)
    if frame["turbine_id"].isna().any() or invalid_turbines:
        raise ValueError(f"hourly.turbine_id must be one of {sorted(TURBINE_IDS)}")
    if frame.duplicated(["turbine_id", "target_time"]).any():
        raise ValueError("hourly cannot contain duplicate (turbine_id, target_time) rows.")

    if not pd.api.types.is_bool_dtype(frame["complete_hour"]):
        raise ValueError("hourly.complete_hour must have boolean dtype.")
    if frame["complete_hour"].isna().any():
        raise ValueError("hourly.complete_hour cannot contain missing values.")
    n_samples = pd.to_numeric(frame["n_samples"], errors="coerce")
    if (
        n_samples.isna().any()
        or not np.isfinite(n_samples).all()
        or not np.equal(n_samples, np.floor(n_samples)).all()
        or not n_samples.between(0, 6).all()
    ):
        raise ValueError("hourly.n_samples must be a finite integer from 0 through 6.")
    frame["n_samples"] = n_samples.astype("int64")
    if not frame["complete_hour"].eq(frame["n_samples"].eq(6)).all():
        raise ValueError("complete_hour must be true exactly when n_samples equals six.")

    for column in _MEASUREMENTS.values():
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    complete = frame["complete_hour"]
    complete_values = frame.loc[complete, list(_MEASUREMENTS.values())].to_numpy(dtype=float)
    if not np.isfinite(complete_values).all():
        raise ValueError("complete hourly rows must have finite power, wind, and temperature.")
    if not frame.loc[complete, "power"].between(0.0, 1.0).all():
        raise ValueError("complete hourly power must be normalized to the [0, 1] range.")
    return frame.sort_values(["turbine_id", "target_time"]).reset_index(drop=True)


def _calendar_features(origins: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    hour = origins.hour.to_numpy(dtype=float)
    month = origins.month.to_numpy(dtype=float)
    day_of_week = origins.dayofweek.to_numpy(dtype=float)
    day_of_year = origins.dayofyear.to_numpy(dtype=float)
    return {
        "target_hour_utc": hour,
        "target_month_utc": month,
        "origin_hour_sin": np.sin(2 * np.pi * hour / 24),
        "origin_hour_cos": np.cos(2 * np.pi * hour / 24),
        "origin_dayofweek_sin": np.sin(2 * np.pi * day_of_week / 7),
        "origin_dayofweek_cos": np.cos(2 * np.pi * day_of_week / 7),
        "origin_dayofyear_sin": np.sin(2 * np.pi * (day_of_year - 1) / 365.25),
        "origin_dayofyear_cos": np.cos(2 * np.pi * (day_of_year - 1) / 365.25),
        "origin_month_sin": np.sin(2 * np.pi * (month - 1) / 12),
        "origin_month_cos": np.cos(2 * np.pi * (month - 1) / 12),
    }


def _empty_features() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "forecast_origin": pd.Series(dtype="datetime64[ns, UTC]"),
            "turbine_id": pd.Series(dtype="string"),
            **{feature: pd.Series(dtype="float64") for feature in NUMERIC_FEATURES},
        }
    ).loc[:, ["forecast_origin", *FEATURE_COLUMNS]]


def _build_features(
    frame: pd.DataFrame, origins: pd.DatetimeIndex, *, strict_recent: bool = True
) -> pd.DataFrame:
    if origins.empty:
        return _empty_features()

    # The dense index makes gaps explicit.  Every lookup ends at origin - 1h,
    # so later measurements cannot enter a feature even if they are present in
    # the supplied frame.
    history_index = pd.date_range(
        origins.min() - pd.offsets.Hour(MAX_LOOKBACK_HOURS),
        origins.max() - pd.offsets.Hour(1),
        freq="h",
        tz="UTC",
    )
    ending_times = origins - pd.offsets.Hour(1)
    calendar = _calendar_features(origins)
    output: list[pd.DataFrame] = []

    for turbine_id, turbine_frame in frame.groupby("turbine_id", sort=True, observed=True):
        complete = turbine_frame.loc[turbine_frame["complete_hour"]].set_index("target_time")
        availability = pd.Series(True, index=complete.index).reindex(
            history_index, fill_value=False
        )
        recent = availability.astype(float).rolling(24, min_periods=24).sum().reindex(ending_times)
        valid_mask = recent.eq(24).to_numpy()
        if strict_recent and not valid_mask.all():
            failed = origins[~valid_mask]
            preview = ", ".join(item.isoformat() for item in failed[:3])
            raise ValueError(
                f"turbine {turbine_id!r} needs all last 24 complete hourly observations; "
                f"missing or partial history before {preview}."
            )
        if not valid_mask.any():
            continue
        valid_origins = origins[valid_mask]
        valid_ending_times = ending_times[valid_mask]

        values: dict[str, object] = {
            "forecast_origin": valid_origins,
            "turbine_id": pd.Series(
                str(turbine_id), index=range(len(valid_origins)), dtype="string"
            ),
        }
        for feature, feature_values in calendar.items():
            values[feature] = feature_values[valid_mask]

        for name, source_column in _MEASUREMENTS.items():
            series = complete[source_column].reindex(history_index).astype(float)
            for lag in LAG_HOURS:
                values[f"{name}_lag_{lag}"] = series.reindex(
                    valid_origins - pd.offsets.Hour(lag)
                ).to_numpy()
            for window in ROLLING_WINDOWS:
                rolling = series.rolling(window, min_periods=1)
                values[f"{name}_mean_{window}"] = (
                    rolling.mean().reindex(valid_ending_times).to_numpy()
                )
                values[f"{name}_std_{window}"] = (
                    rolling.std().reindex(valid_ending_times).to_numpy()
                )
                values[f"{name}_min_{window}"] = (
                    rolling.min().reindex(valid_ending_times).to_numpy()
                )
                values[f"{name}_max_{window}"] = (
                    rolling.max().reindex(valid_ending_times).to_numpy()
                )
        for window in ROLLING_WINDOWS:
            values[f"history_coverage_{window}"] = (
                availability.astype(float)
                .rolling(window, min_periods=1)
                .mean()
                .reindex(valid_ending_times)
                .to_numpy()
            )
        output.append(pd.DataFrame(values).loc[:, ["forecast_origin", *FEATURE_COLUMNS]])

    if not output:
        if frame.empty:
            raise ValueError("hourly contains no turbine observations.")
        return _empty_features()
    return pd.concat(output, ignore_index=True)


def build_history_features(hourly: pd.DataFrame, origins: Iterable[object]) -> pd.DataFrame:
    """Create origin-time features using only complete observations before each origin.

    Each requested origin produces a row for every turbine represented in
    ``hourly``.  The immediately preceding 24 hours must all be complete;
    older gaps stay as ``NaN`` in lag/statistic features and are quantified by
    ``history_coverage_*`` rather than imputed here.
    """

    return _build_features(_validate_hourly(hourly), _normalise_origins(origins))


def build_supervised_history(hourly: pd.DataFrame, origins: Iterable[object]) -> pd.DataFrame:
    """Append complete direct labels ``y_01`` through ``y_48`` to past-only features.

    ``y_01`` is power at the origin hour and ``y_48`` is power 47 hours later.
    An origin/turbine is retained only when every one of those 48 future target
    hours is complete.  Labels are never imputed.
    """

    frame = _validate_hourly(hourly)
    origin_index = _normalise_origins(origins)
    features = _build_features(frame, origin_index, strict_recent=False)
    if features.empty:
        return features.assign(**{column: pd.Series(dtype="float64") for column in TARGET_COLUMNS})

    labels: list[pd.DataFrame] = []
    for turbine_id, turbine_frame in frame.groupby("turbine_id", sort=True, observed=True):
        complete_power = turbine_frame.loc[turbine_frame["complete_hour"]].set_index("target_time")[
            "power"
        ]
        values = {
            column: complete_power.reindex(origin_index + pd.offsets.Hour(step - 1)).to_numpy()
            for step, column in enumerate(TARGET_COLUMNS, start=1)
        }
        labels.append(
            pd.DataFrame(
                {
                    "forecast_origin": origin_index,
                    "turbine_id": pd.Series(
                        str(turbine_id), index=range(len(origin_index)), dtype="string"
                    ),
                    **values,
                }
            )
        )
    target_frame = pd.concat(labels, ignore_index=True)
    result = features.merge(
        target_frame,
        on=["forecast_origin", "turbine_id"],
        how="inner",
        validate="one_to_one",
    )
    return result.loc[result.loc[:, TARGET_COLUMNS].notna().all(axis=1)].reset_index(drop=True)
