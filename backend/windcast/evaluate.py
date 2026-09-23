"""January backtest, baseline comparison, and metrics API artifact."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from windcast.data import load_hourly_dataset
from windcast.model import (
    artifact_path,
    build_feature_frame,
    predict,
    save_artifact,
    train_artifact,
    training_frame,
)
from windcast.paths import metrics_file, models_dir
from windcast.timeline import BACKTEST_FROM, BACKTEST_TO, LOCAL_TZ, TURBINES, iso_local
from windcast.weather import fetch_training_weather, fetch_weather

METHODS = ("model", "power_curve", "climatology", "persistence")
LABELS = {
    "model": "Модель квантилей",
    "power_curve": "Кривая мощности",
    "climatology": "Климатология",
    "persistence": "Персистентность",
}


def _metric(actual: np.ndarray, predicted: np.ndarray) -> tuple[float, float]:
    error = predicted - actual
    return float(np.abs(error).mean()), float(np.sqrt(np.square(error).mean()))


def evaluate_predictions(
    rows: Iterable[dict[str, Any]] | pd.DataFrame,
) -> dict[str, Any]:
    frame = pd.DataFrame(rows).copy()
    required = {"actual", "p10", "p90", *METHODS}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"В метриках отсутствуют поля: {sorted(missing)}")
    mask = frame["actual"].notna()
    if not np.isfinite(
        frame.loc[mask, ["actual", "p10", "p90", *METHODS]].to_numpy(float)
    ).all():
        raise ValueError(
            "Прогноз и базовые линии на строках с фактом должны быть конечными"
        )
    scored = frame.loc[mask]
    if scored.empty:
        raise ValueError("Нет факта для расчёта метрик")
    methods = []
    for method in METHODS:
        nmae, nrmse = _metric(
            scored["actual"].to_numpy(float), scored[method].to_numpy(float)
        )
        methods.append(
            {"key": method, "label": LABELS[method], "nmae": nmae, "nrmse": nrmse}
        )
    horizon = []
    if "h" in scored:
        for h, group in scored.groupby("h"):
            horizon.append(
                {
                    "h": int(h),
                    "model": _metric(
                        group["actual"].to_numpy(float), group["model"].to_numpy(float)
                    )[0],
                    "power_curve": _metric(
                        group["actual"].to_numpy(float),
                        group["power_curve"].to_numpy(float),
                    )[0],
                }
            )
    by_day = []
    if "day" in scored:
        for day, group in scored.groupby("day"):
            by_day.append(
                {
                    "day": str(day),
                    "model": _metric(
                        group["actual"].to_numpy(float), group["model"].to_numpy(float)
                    )[0],
                    "power_curve": _metric(
                        group["actual"].to_numpy(float),
                        group["power_curve"].to_numpy(float),
                    )[0],
                }
            )
    return {
        "period": {"from": "2025-12-31", "to": "2026-01-29"},
        "issues_count": int(frame.get("day", pd.Series(["unknown"])).nunique()),
        "scored_rows": len(scored),
        "excluded_rows": int(len(frame) - len(scored)),
        "coverage_p10_p90": float(
            (
                (scored["actual"] >= scored["p10"])
                & (scored["actual"] <= scored["p90"])
            ).mean()
        ),
        "methods": methods,
        "by_horizon": horizon,
        "by_day": by_day,
    }


def build_metrics_response(metrics: dict[str, Any]) -> dict[str, Any]:
    """Boundary used by the future FastAPI route; keeps payload JSON-native."""
    return json.loads(json.dumps(metrics, default=_json_default, allow_nan=False))


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    raise TypeError(f"Не сериализуется: {type(value)!r}")


def select_offset(
    candidates: dict[int, Iterable[dict[str, Any]] | pd.DataFrame],
    *,
    selection_days: int = 7,
) -> int:
    """Pick the lowest model nMAE using only the first seven January target days."""
    scored: list[tuple[float, int]] = []
    for offset, values in candidates.items():
        frame = pd.DataFrame(values).copy()
        if "target_time_utc" in frame:
            local_date = (
                pd.to_datetime(frame["target_time_utc"], utc=True)
                .dt.tz_convert(LOCAL_TZ)
                .dt.date
            )
            first = pd.Timestamp("2026-01-01").date()
            last = (pd.Timestamp(first) + pd.Timedelta(days=selection_days - 1)).date()
            selected = frame.loc[(local_date >= first) & (local_date <= last)]
        else:
            selected = frame
        selected = selected.loc[selected["actual"].notna()]
        if selected.empty:
            raise ValueError("Нет факта на окне выбора смещения")
        scored.append(
            (
                float(np.abs(selected["model"] - selected["actual"]).mean()),
                offset,
            )
        )
    return min(scored)[1]


def _actuals(hourly: pd.DataFrame) -> dict[str, pd.DataFrame]:
    result = {}
    for turbine in ("1", "2"):
        result[turbine] = hourly.loc[hourly["turbine"] == turbine].set_index("ts_utc")[
            ["power", "valid"]
        ]
    paired = result["1"].join(result["2"], lsuffix="_1", rsuffix="_2", how="inner")
    result["plant"] = pd.DataFrame(
        {
            "power": (paired["power_1"] + paired["power_2"]) / 2,
            "valid": paired["valid_1"] & paired["valid_2"],
        },
        index=paired.index,
    )
    return result


def _baseline_values(
    artifact: dict[str, Any],
    weather: pd.DataFrame,
    turbine: str,
    issue_time: pd.Timestamp,
    actual: pd.DataFrame,
) -> pd.DataFrame:
    features = build_feature_frame(weather, turbine=turbine)
    baseline = artifact["baselines"]
    climate = baseline["climatology"][turbine]
    global_value = baseline["global"][turbine]
    values = pd.DataFrame(index=weather.index)
    values["climatology"] = [
        climate.get(f"{int(month)}-{int(hour)}", global_value)
        for month, hour in zip(
            features["local_month"], features["local_hour"], strict=True
        )
    ]
    past = pd.DatetimeIndex(
        [
            issue_time - pd.Timedelta(hours=24) + pd.Timedelta(hours=index % 24)
            for index in range(48)
        ]
    )
    previous = actual.reindex(past)
    values["persistence"] = previous["power"].where(previous["valid"]).to_numpy()
    values["persistence_fallback"] = (
        previous["power"].where(previous["valid"]).isna().to_numpy()
    )
    values["persistence"] = values["persistence"].fillna(values["climatology"])
    curves = baseline["power_curve"][turbine]
    values["power_curve"] = np.concatenate(
        [
            curves[str(lag)].predict(
                features.loc[features["lag_days"] == lag, "wind_100m_ms"]
            )
            for lag in (1, 2)
        ]
    )
    values[["climatology", "persistence", "power_curve"]] = values[
        ["climatology", "persistence", "power_curve"]
    ].clip(0, 1)
    return values


def _issue_rows(
    issue_date: str,
    artifact: dict[str, Any],
    actuals: dict[str, pd.DataFrame],
    weather: dict | None = None,
) -> pd.DataFrame:
    weather = weather or fetch_weather(issue_date, run="latest")
    prediction = predict(issue_date, weather, artifact_path=artifact_path(issue_date))
    issue_time = pd.Timestamp(weather["issue_time_utc"])
    parts = []
    for turbine in TURBINES:
        model_rows = prediction.loc[prediction["turbine"] == turbine].copy()
        horizon_weather = weather["hourly"].copy()
        baseline = _baseline_values(
            artifact, horizon_weather, turbine, issue_time, actuals[turbine]
        )
        observed = actuals[turbine].reindex(
            pd.DatetimeIndex(model_rows["target_time_utc"])
        )
        model_rows["actual"] = observed["power"].where(observed["valid"]).to_numpy()
        model_rows["valid"] = observed["valid"].fillna(False).to_numpy()
        model_rows[baseline.columns] = baseline.to_numpy()
        model_rows["model"] = model_rows["p50"]
        model_rows["day"] = issue_date
        parts.append(model_rows)
    return pd.concat(parts, ignore_index=True)


def _training_weather(cutoff: str) -> pd.DataFrame:
    return fetch_training_weather("2024-06-01T00:00Z", cutoff)


def _fit_for_offset(offset: int, cutoff: str) -> tuple[dict[str, Any], pd.DataFrame]:
    hourly = load_hourly_dataset(rebuild=True, offset_hours=offset)
    weather = _training_weather(cutoff)
    train = training_frame(hourly, weather, cutoff_utc=cutoff)
    return train_artifact(train, cutoff_utc=cutoff, offset_hours=offset), hourly


def run_january_evaluation() -> dict[str, Any]:
    """Evaluate a frozen pre-January model, choose offset on days 1–7, and write JSON."""
    cutoff = "2025-12-31T19:00Z"
    candidates: dict[int, pd.DataFrame] = {}
    fitted: dict[int, tuple[dict[str, Any], pd.DataFrame]] = {}
    selection_paths: list[Path] = []
    issues = pd.date_range(BACKTEST_FROM, BACKTEST_TO, freq="D")
    weather_by_issue = {
        issue.date().isoformat(): fetch_weather(issue.date().isoformat(), run="latest")
        for issue in issues
    }
    for offset in (5, 6):
        artifact, hourly = _fit_for_offset(offset, cutoff)
        temporary = models_dir() / f".selection_offset_{offset}.pkl"
        save_artifact(artifact, temporary)
        selection_paths.append(temporary)
        actuals = _actuals(hourly)
        rows = []
        for issue in issues:
            date_value = issue.date().isoformat()
            weather = weather_by_issue[date_value]
            forecast = predict(date_value, weather, artifact_path=temporary)
            plant = forecast.loc[forecast["turbine"] == "plant"].copy()
            baseline = _baseline_values(
                artifact,
                weather["hourly"],
                "plant",
                pd.Timestamp(weather["issue_time_utc"]),
                actuals["plant"],
            )
            observed = actuals["plant"].reindex(
                pd.DatetimeIndex(plant["target_time_utc"])
            )
            plant["actual"] = observed["power"].where(observed["valid"]).to_numpy()
            plant[baseline.columns] = baseline.to_numpy()
            plant["model"] = plant["p50"]
            rows.append(plant)
        candidates[offset] = pd.concat(rows, ignore_index=True)
        fitted[offset] = (artifact, hourly)
    selected = select_offset(candidates)
    for temporary in selection_paths:
        temporary.unlink(missing_ok=True)
    artifact, hourly = fitted[selected]
    save_artifact(artifact, artifact_path("2026-01-29"))
    actuals = _actuals(hourly)
    rows = [
        _issue_rows(
            issue.date().isoformat(),
            artifact,
            actuals,
            weather_by_issue[issue.date().isoformat()],
        )
        for issue in issues
    ]
    all_rows = pd.concat(rows, ignore_index=True)
    plant_rows = all_rows.loc[all_rows["turbine"] == "plant"]
    metrics = evaluate_predictions(plant_rows)
    metrics["selection"] = {
        "offset_hours": selected,
        "selection_target_days": "2026-01-01..2026-01-07",
        "diagnostic_target_days": "2026-01-08..2026-01-31",
    }
    diagnostic = plant_rows.loc[
        pd.to_datetime(plant_rows["target_time_utc"], utc=True)
        .dt.tz_convert(LOCAL_TZ)
        .dt.date
        >= pd.Timestamp("2026-01-08").date()
    ].dropna(subset=["actual"])
    metrics["diagnostics"] = {
        "untouched_jan_08_31_rows": len(diagnostic),
        "untouched_jan_08_31_model_nmae": _metric(
            diagnostic["actual"].to_numpy(float), diagnostic["model"].to_numpy(float)
        )[0],
        "untouched_jan_08_31_persistence_nmae": _metric(
            diagnostic["actual"].to_numpy(float),
            diagnostic["persistence"].to_numpy(float),
        )[0],
        "persistence_fallbacks": int(plant_rows["persistence_fallback"].sum()),
    }
    series = all_rows.loc[
        (all_rows["target_time_utc"] < pd.Timestamp("2026-02-01T00:00Z"))
        & all_rows["actual"].notna()
    ].sort_values(["turbine", "target_time_utc", "h"])
    series = series.drop_duplicates(["turbine", "target_time_utc"], keep="first")
    metrics["series"] = [
        {
            "target_time_local": iso_local(value.target_time_utc.to_pydatetime()),
            "turbine": value.turbine,
            "p10": float(value.p10),
            "p50": float(value.p50),
            "p90": float(value.p90),
            "actual": float(value.actual),
        }
        for value in series.itertuples(index=False)
    ]
    path = metrics_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(metrics, default=_json_default, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return metrics


def train_february_artifact(offset_hours: int) -> Path:
    cutoff = "2026-01-31T19:00Z"
    artifact, _ = _fit_for_offset(offset_hours, cutoff)
    path = artifact_path("2026-02-01")
    save_artifact(artifact, path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default="2025-12-31")
    parser.add_argument("--to", dest="to_date", default="2026-01-29")
    args = parser.parse_args()
    if (args.from_date, args.to_date) != ("2025-12-31", "2026-01-29"):
        raise ValueError("T3 поддерживает фиксированный январский бэктест из контракта")
    metrics = run_january_evaluation()
    train_february_artifact(metrics["selection"]["offset_hours"])
    for method in metrics["methods"]:
        print(f"{method['key']}: nMAE={method['nmae']:.4%} nRMSE={method['nrmse']:.4%}")
    print(f"SCADA_UTC_OFFSET_H={metrics['selection']['offset_hours']}")


if __name__ == "__main__":
    main()
