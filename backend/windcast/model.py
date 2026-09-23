"""Weather-only quantile power forecasts and honest weather baselines."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression

from windcast.paths import models_dir
from windcast.timeline import TURBINES, issue_time_utc, live_times, target_times_utc

MODEL_VERSION = "histgb-q-2026-01-31"
FEATURE_COLUMNS = [
    "wind_100m_ms",
    "wind_10m_ms",
    "wind_cubed",
    "direction_sin",
    "direction_cos",
    "temp_c",
    "local_hour",
    "local_month",
    "h",
    "turbine_id",
    "lag_days",
]
QUANTILES = (0.1, 0.5, 0.9)


def _utc(values: pd.Series | pd.DatetimeIndex) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True, errors="raise"))


def build_feature_frame(
    weather: pd.DataFrame, *, turbine: str, lag_days: int | None = None
) -> pd.DataFrame:
    """Make inference features from forecast weather only.

    ``weather`` deliberately has no SCADA input.  The time fields use the product's
    fixed UTC+5 convention, while all internal timestamps remain UTC.
    """
    required = {
        "target_time_utc",
        "wind_100m_ms",
        "wind_10m_ms",
        "wind_dir_deg",
        "temp_c",
    }
    missing = required - set(weather.columns)
    if missing:
        raise ValueError(f"В погоде отсутствуют признаки: {', '.join(sorted(missing))}")
    if turbine not in TURBINES:
        raise ValueError("turbine должен быть 1, 2 или plant")
    frame = weather.copy()
    target = _utc(frame["target_time_utc"])
    local = target + pd.Timedelta(hours=5)
    direction = pd.to_numeric(frame["wind_dir_deg"], errors="coerce")
    wind = pd.to_numeric(frame["wind_100m_ms"], errors="coerce")
    frame["wind_100m_ms"] = wind
    frame["wind_10m_ms"] = pd.to_numeric(frame["wind_10m_ms"], errors="coerce")
    frame["temp_c"] = pd.to_numeric(frame["temp_c"], errors="coerce")
    frame["wind_cubed"] = wind**3
    frame["direction_sin"] = np.sin(np.deg2rad(direction))
    frame["direction_cos"] = np.cos(np.deg2rad(direction))
    frame["local_hour"] = local.hour
    frame["local_month"] = local.month
    if "h" not in frame:
        frame["h"] = np.arange(1, len(frame) + 1)
    frame["h"] = pd.to_numeric(frame["h"], errors="coerce")
    if lag_days is None:
        if "lag_days" in frame:
            frame["lag_days"] = pd.to_numeric(frame["lag_days"], errors="coerce")
        elif "init_time_utc" in frame:
            init = _utc(frame["init_time_utc"])
            frame["lag_days"] = (target - init).total_seconds() / 86_400
        else:
            frame["lag_days"] = np.where(frame["h"] <= 24, 1, 2)
    else:
        frame["lag_days"] = lag_days
    frame["turbine_id"] = {"1": 1, "2": 2, "plant": 3}[turbine]
    if not np.isfinite(frame[FEATURE_COLUMNS].to_numpy(dtype=float)).all():
        raise ValueError("Погодные признаки должны быть конечными")
    if not frame["lag_days"].isin((1, 2)).all():
        raise ValueError("Погодный лаг должен быть ровно 1 или 2 суток")
    return frame[FEATURE_COLUMNS].copy()


def _plant_labels(hourly: pd.DataFrame) -> pd.DataFrame:
    first = hourly.loc[hourly["turbine"] == "1", ["ts_utc", "power", "valid"]]
    second = hourly.loc[hourly["turbine"] == "2", ["ts_utc", "power", "valid"]]
    merged = first.merge(second, on="ts_utc", suffixes=("_1", "_2"), how="inner")
    return pd.DataFrame(
        {
            "ts_utc": merged["ts_utc"],
            "turbine": "plant",
            "power": (merged["power_1"] + merged["power_2"]) / 2,
            "valid": merged["valid_1"] & merged["valid_2"],
        }
    )


def _labels(hourly: pd.DataFrame) -> pd.DataFrame:
    actual = hourly[["ts_utc", "turbine", "power", "valid"]].copy()
    return pd.concat([actual, _plant_labels(actual)], ignore_index=True)


def training_frame(
    hourly: pd.DataFrame, weather: pd.DataFrame, *, cutoff_utc: str
) -> pd.DataFrame:
    """Join each historical Previous Runs lag with valid target labels before cutoff."""
    cutoff = pd.Timestamp(cutoff_utc)
    cutoff = (
        cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    )
    weather = weather.copy()
    weather["target_time_utc"] = _utc(weather["target_time_utc"])
    if "lag_days" not in weather:
        raise ValueError("Обучающая погода должна содержать lag_days 1 или 2")
    local = weather["target_time_utc"] + pd.Timedelta(hours=5)
    weather["h"] = local.dt.hour + 1 + 24 * (weather["lag_days"].astype(int) - 1)
    # v1 is deliberately trained with its all-previous-day2 first day as well.
    # It has the same weather timestamp but a distinct horizon 1..24.
    previous = weather.loc[weather["lag_days"] == 2].copy()
    previous["h"] = (previous["target_time_utc"] + pd.Timedelta(hours=5)).dt.hour + 1
    weather = pd.concat([weather, previous], ignore_index=True)
    labels = _labels(hourly)
    labels["ts_utc"] = _utc(labels["ts_utc"])
    # A target hour is known only after its end, which prevents label leakage.
    labels = labels.loc[
        labels["valid"]
        & (labels["ts_utc"] + pd.Timedelta(hours=1) <= cutoff)
        & (labels["ts_utc"] >= pd.Timestamp("2024-06-01T00:00Z"))
    ]
    parts: list[pd.DataFrame] = []
    for turbine in TURBINES:
        features = build_feature_frame(weather, turbine=turbine)
        source = weather[["target_time_utc"]].copy()
        source[FEATURE_COLUMNS] = features
        part = source.merge(
            labels.loc[labels["turbine"] == turbine, ["ts_utc", "power"]],
            left_on="target_time_utc",
            right_on="ts_utc",
            how="inner",
            validate="many_to_one",
        )
        parts.append(part.drop(columns="ts_utc"))
    result = pd.concat(parts, ignore_index=True)
    return result.dropna(subset=FEATURE_COLUMNS + ["power"])


def _baseline_metadata(train: pd.DataFrame) -> dict[str, Any]:
    output: dict[str, Any] = {"climatology": {}, "global": {}, "power_curve": {}}
    for turbine in TURBINES:
        key = {"1": 1, "2": 2, "plant": 3}[turbine]
        frame = train.loc[train["turbine_id"] == key]
        if frame.empty:
            # This only supports small injected unit-test datasets; real artifacts
            # always contain the separately trained paired plant labels.
            frame = train
        grouped = frame.groupby(["local_month", "local_hour"])["power"].median()
        output["climatology"][turbine] = {
            f"{month}-{hour}": float(value) for (month, hour), value in grouped.items()
        }
        output["global"][turbine] = float(frame["power"].median())
        curves: dict[str, IsotonicRegression] = {}
        for lag in (1, 2):
            subset = frame.loc[frame["lag_days"] == lag]
            curves[str(lag)] = IsotonicRegression(out_of_bounds="clip").fit(
                subset["wind_100m_ms"], subset["power"]
            )
        output["power_curve"][turbine] = curves
    return output


def train_artifact(
    train: pd.DataFrame, *, cutoff_utc: str, offset_hours: int
) -> dict[str, Any]:
    """Fit three deterministic HistGradientBoosting quantile estimators."""
    train = train.copy()
    if not set(FEATURE_COLUMNS).issubset(train.columns):
        parts = []
        for turbine in TURBINES:
            part = train.loc[train.get("turbine", "") == turbine].copy()
            if part.empty:
                continue
            part[FEATURE_COLUMNS] = build_feature_frame(part, turbine=turbine)
            parts.append(part)
        train = pd.concat(parts, ignore_index=True) if parts else train
    if train.empty:
        raise ValueError("Нет валидных наблюдений для обучения")
    models = {
        str(quantile): HistGradientBoostingRegressor(
            loss="quantile",
            quantile=quantile,
            max_iter=160,
            max_leaf_nodes=15,
            min_samples_leaf=50,
            l2_regularization=1.0,
            learning_rate=0.08,
            early_stopping=False,
            random_state=42,
        ).fit(train[FEATURE_COLUMNS], train["power"])
        for quantile in QUANTILES
    }
    return {
        "models": models,
        "baselines": _baseline_metadata(train),
        "metadata": {
            "model_version": MODEL_VERSION,
            "cutoff_utc": str(pd.Timestamp(cutoff_utc)),
            "offset_hours": offset_hours,
            "features": FEATURE_COLUMNS,
            "weather_model": "best_match",
            "training_rows": len(train),
        },
    }


def save_artifact(artifact: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        pickle.dump(artifact, handle, protocol=pickle.HIGHEST_PROTOCOL)


def load_artifact(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        artifact = pickle.load(handle)
    if artifact.get("metadata", {}).get("model_version") != MODEL_VERSION:
        raise ValueError("Несовместимая версия модели")
    return artifact


def artifact_path(issue_date: str) -> Path:
    return models_dir() / (
        "quantile_2025-12-31.pkl"
        if issue_date < "2026-01-31"
        else "quantile_2026-01-31.pkl"
    )


def predict(
    issue_date: str, weather: dict, *, artifact_path: Path | None = None
) -> pd.DataFrame:
    """Return 48 hourly quantile forecasts for each turbine and the paired plant."""
    path = artifact_path or globals()["artifact_path"](issue_date)
    artifact = load_artifact(path)
    hourly = weather.get("hourly")
    if not isinstance(hourly, pd.DataFrame) or len(hourly) != 48:
        raise ValueError("Для прогноза нужны 48 строк погоды")
    supplied = _utc(hourly["target_time_utc"])
    h = pd.to_numeric(hourly.get("h"), errors="coerce")
    if not (
        h.notna().all()
        and (h % 1 == 0).all()
        and set(h.astype(int)) == set(range(1, 49))
        and h.is_unique
    ):
        raise ValueError("Погодное окно должно содержать горизонты 1–48")
    supplied_issue = pd.Timestamp(weather.get("issue_time_utc"))
    supplied_issue = (
        supplied_issue.tz_localize("UTC")
        if supplied_issue.tzinfo is None
        else supplied_issue.tz_convert("UTC")
    )
    inits = _utc(hourly["init_time_utc"])
    if (inits > supplied_issue).any():
        raise ValueError("Погодный прогон позже момента выпуска")
    expected = (
        pd.DatetimeIndex(live_times(supplied_issue.to_pydatetime())[1])
        if issue_date == "live"
        else pd.DatetimeIndex(target_times_utc(issue_date))
    )
    if not supplied.equals(expected):
        raise ValueError("Целевые часы погоды не совпадают с горизонтом выпуска")
    if issue_date != "live":
        canonical_issue = pd.Timestamp(issue_time_utc(issue_date))
        if supplied_issue != canonical_issue:
            raise ValueError("Момент выпуска в погоде не совпадает с датой выпуска")
        cutoff = pd.Timestamp(artifact["metadata"]["cutoff_utc"])
        cutoff = (
            cutoff.tz_localize("UTC")
            if cutoff.tzinfo is None
            else cutoff.tz_convert("UTC")
        )
        if cutoff > pd.Timestamp(issue_time_utc(issue_date)):
            raise ValueError("Артефакт обучен на данных после момента выпуска")
    rows: list[pd.DataFrame] = []
    feature_weather = hourly.copy()
    if issue_date == "live":
        # Live has a fetched-at availability bound, rather than archived day lags.
        # Keep that provenance intact and bucket the 48-hour lead into the same
        # model horizon convention used for archival day1/day2 training.
        feature_weather["lag_days"] = np.where(feature_weather["h"] <= 24, 1, 2)
    for turbine in TURBINES:
        features = build_feature_frame(feature_weather, turbine=turbine)
        values = np.column_stack(
            [
                artifact["models"][str(quantile)].predict(features)
                for quantile in QUANTILES
            ]
        )
        values = np.clip(np.sort(values, axis=1), 0, 1)
        result = hourly[["h", "target_time_utc", "wind_100m_ms", "temp_c"]].copy()
        result["turbine"] = turbine
        result["p10"] = values[:, 0]
        result["p50"] = values[:, 1]
        result["p90"] = values[:, 2]
        result = result.rename(
            columns={"wind_100m_ms": "wind_fc_ms", "temp_c": "temp_fc_c"}
        )
        rows.append(
            result[
                [
                    "h",
                    "target_time_utc",
                    "turbine",
                    "p10",
                    "p50",
                    "p90",
                    "wind_fc_ms",
                    "temp_fc_c",
                ]
            ]
        )
    return pd.concat(rows, ignore_index=True)
