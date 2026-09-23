"""Leakage-safe weather feature construction and hourly-label joins."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from .data import TURBINE_IDS

WEATHER_VALUE_COLUMNS = [
    "wind_speed_10m",
    "wind_speed_100m",
    "wind_u_100m",
    "wind_v_100m",
    "temperature_2m",
    "surface_pressure",
    "gust_speed",
]
WEATHER_TIME_COLUMNS = [
    "forecast_origin",
    "target_time",
    "weather_run_time",
    "weather_available_at",
]
WEATHER_REQUIRED_COLUMNS = [
    "turbine_id",
    *WEATHER_TIME_COLUMNS,
    "lead_hours",
    *WEATHER_VALUE_COLUMNS,
]

# This exact order is the model-facing feature contract. Keep the target and
# observation-time readings out of it: neither exists at forecast inference.
NUMERIC_FEATURES = [
    *WEATHER_VALUE_COLUMNS,
    "wind_direction_sin",
    "wind_direction_cos",
    "lead_hours",
    "run_age_hours",
    "target_hour_sin",
    "target_hour_cos",
    "target_dayofyear_sin",
    "target_dayofyear_cos",
    "target_hour_utc",
    "target_month_utc",
]
CAT_FEATURES = ["turbine_id"]
FEATURE_COLUMNS = [*NUMERIC_FEATURES, *CAT_FEATURES]


def _missing(frame: pd.DataFrame, required: Iterable[str], name: str) -> None:
    absent = sorted(set(required).difference(frame.columns))
    if absent:
        raise ValueError(f"{name} is missing required columns: {absent}")


def _require_utc(frame: pd.DataFrame, columns: Iterable[str], name: str) -> None:
    for column in columns:
        series = frame[column]
        if not isinstance(series.dtype, pd.DatetimeTZDtype) or str(series.dt.tz) != "UTC":
            raise ValueError(f"{name}.{column} must be timezone-aware UTC timestamps")
        if series.isna().any():
            raise ValueError(f"{name}.{column} must not contain missing timestamps")


def validate_weather(weather: pd.DataFrame) -> pd.DataFrame:
    """Validate a weather forecast table without mutating it.

    A weather row can only be used if it was available by the forecast origin
    and forecasts a strictly later target hour. Lead values are constrained to
    the competition's 1--48 hour horizon.
    """

    _missing(weather, WEATHER_REQUIRED_COLUMNS, "weather")
    _require_utc(weather, WEATHER_TIME_COLUMNS, "weather")
    if weather["turbine_id"].isna().any() or (weather["turbine_id"].astype(str) == "").any():
        raise ValueError("weather.turbine_id must be non-empty")
    invalid_turbines = set(weather["turbine_id"].astype(str)).difference(TURBINE_IDS)
    if invalid_turbines:
        raise ValueError(
            "weather.turbine_id must be one of "
            f"{sorted(TURBINE_IDS)}, got {sorted(invalid_turbines)}"
        )

    numeric = weather[[*WEATHER_VALUE_COLUMNS, "lead_hours"]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError("weather numeric values must be finite and non-missing")
    if not numeric["lead_hours"].between(1, 48).all():
        raise ValueError("weather.lead_hours must be within the 1--48 hour horizon")
    if not np.equal(numeric["lead_hours"], np.floor(numeric["lead_hours"])).all():
        raise ValueError("weather.lead_hours must be an integer number of hours")
    if not (weather["weather_available_at"] <= weather["forecast_origin"]).all():
        raise ValueError("weather_available_at must be <= forecast_origin")
    if not (weather["forecast_origin"] < weather["target_time"]).all():
        raise ValueError("forecast_origin must be strictly before target_time")
    if not (weather["weather_run_time"] <= weather["weather_available_at"]).all():
        raise ValueError("weather_run_time must be <= weather_available_at")
    if not (weather["forecast_origin"] == weather["forecast_origin"].dt.floor("h")).all():
        raise ValueError("forecast_origin must be aligned to a UTC hour")
    if not (weather["target_time"] == weather["target_time"].dt.floor("h")).all():
        raise ValueError("target_time must be aligned to a UTC hour")
    expected_lead = pd.to_timedelta(numeric["lead_hours"], unit="h")
    if not ((weather["target_time"] - weather["forecast_origin"]) == expected_lead).all():
        raise ValueError("lead_hours must exactly equal target_time - forecast_origin in hours")
    if weather.duplicated(["turbine_id", "forecast_origin", "target_time"]).any():
        raise ValueError("weather has duplicate turbine_id/forecast_origin/target_time rows")
    if not (numeric[["wind_speed_10m", "wind_speed_100m", "gust_speed"]] >= 0).all().all():
        raise ValueError("weather wind speeds and gust_speed must be non-negative")
    if not (numeric["surface_pressure"] > 0).all():
        raise ValueError("weather.surface_pressure must be positive")
    return weather


def build_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Produce the stable inference feature matrix from validated weather rows.

    ``frame`` may be weather alone or the result of ``build_training_table``.
    It intentionally never reads ``power``, ``observed_wind`` or
    ``observed_temperature``.
    """

    validate_weather(frame)
    features = pd.DataFrame(index=frame.index)
    for column in WEATHER_VALUE_COLUMNS:
        features[column] = pd.to_numeric(frame[column], errors="raise").astype("float64")
    features["lead_hours"] = pd.to_numeric(frame["lead_hours"], errors="raise").astype("float64")
    features["run_age_hours"] = (
        (frame["forecast_origin"] - frame["weather_run_time"]).dt.total_seconds() / 3600.0
    ).astype("float64")
    direction_norm = np.hypot(features["wind_u_100m"], features["wind_v_100m"])
    # Direction is undefined in calm air. Encode that deterministic case as the
    # origin rather than manufacturing a bearing.
    features["wind_direction_sin"] = np.divide(
        features["wind_u_100m"],
        direction_norm,
        out=np.zeros(len(features)),
        where=direction_norm != 0,
    )
    features["wind_direction_cos"] = np.divide(
        features["wind_v_100m"],
        direction_norm,
        out=np.zeros(len(features)),
        where=direction_norm != 0,
    )
    target = frame["target_time"]
    hour_angle = 2.0 * np.pi * target.dt.hour / 24.0
    days_in_year = np.where(target.dt.is_leap_year, 366.0, 365.0)
    day_angle = 2.0 * np.pi * (target.dt.dayofyear - 1) / days_in_year
    features["target_hour_sin"] = np.sin(hour_angle)
    features["target_hour_cos"] = np.cos(hour_angle)
    features["target_dayofyear_sin"] = np.sin(day_angle)
    features["target_dayofyear_cos"] = np.cos(day_angle)
    features["target_hour_utc"] = target.dt.hour.astype("int16")
    features["target_month_utc"] = target.dt.month.astype("int16")
    features["turbine_id"] = frame["turbine_id"].astype("string")
    return features[FEATURE_COLUMNS]


def build_training_table(hourly: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """Join complete hourly labels to forecast-safe weather rows.

    Output preserves ``power`` as the supervised target, while callers obtain
    the model matrix through ``build_features(result)``. Missing labels are
    skipped. Multiple weather rows for a turbine/origin/target are rejected so
    a training example cannot be selected arbitrarily.
    """

    _missing(hourly, ["turbine_id", "target_time", "power", "complete_hour"], "hourly")
    _require_utc(hourly, ["target_time"], "hourly")
    validate_weather(weather)

    labels = hourly.copy()
    labels["turbine_id"] = labels["turbine_id"].astype("string")
    labels["power"] = pd.to_numeric(labels["power"], errors="coerce")
    labels = labels.loc[
        labels["complete_hour"].astype(bool)
        & labels["power"].notna()
        & labels["power"].between(0.0, 1.0)
    ].copy()

    forecasts = weather.copy()
    forecasts["turbine_id"] = forecasts["turbine_id"].astype("string")
    keys = ["turbine_id", "forecast_origin", "target_time"]
    if forecasts.duplicated(keys).any():
        raise ValueError("weather has duplicate turbine_id/forecast_origin/target_time rows")

    joined = forecasts.merge(
        labels.drop(columns=["complete_hour"]),
        on=["turbine_id", "target_time"],
        how="inner",
        validate="many_to_one",
    )
    return joined.sort_values(keys).reset_index(drop=True)
