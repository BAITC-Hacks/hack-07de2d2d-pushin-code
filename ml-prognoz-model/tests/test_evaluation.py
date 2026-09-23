from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wind_forecast.artifact import ForecastArtifact
from wind_forecast.evaluation import (
    EvaluationConfig,
    RollingFold,
    _metric_block,
    _metrics,
    _prepare_dataset,
    evaluation_plan,
    run_evaluation,
)
from wind_forecast.features import CAT_FEATURES, FEATURE_COLUMNS, NUMERIC_FEATURES
from wind_forecast.models import Candidate


def _weather_rows() -> pd.DataFrame:
    rows = []
    for turbine in ("1", "2"):
        for origin in pd.date_range("2025-01-01T00:00:00Z", "2025-01-07T23:00:00Z", freq="h"):
            lead = 1
            target = origin + pd.Timedelta(int(lead), unit="h")
            rows.append(
                {
                    "turbine_id": turbine,
                    "forecast_origin": origin,
                    "target_time": target,
                    "weather_run_time": origin - pd.Timedelta(1, unit="h"),
                    "weather_available_at": origin,
                    "lead_hours": lead,
                    "wind_speed_10m": 5.0,
                    "wind_speed_100m": 7.0,
                    "wind_u_100m": 3.0,
                    "wind_v_100m": 4.0,
                    "temperature_2m": 10.0,
                    "surface_pressure": 1000.0,
                    "gust_speed": 9.0,
                    "power": 0.2 if turbine == "1" else 0.6,
                }
            )
    # This target belongs in the frozen holdout by target time but its 24-hour
    # forecast origin precedes the frozen train cutoff, so it must not be scored.
    rows.append(
        {
            "turbine_id": "1",
            "forecast_origin": pd.Timestamp("2025-01-05T00:00:00Z"),
            "target_time": pd.Timestamp("2025-01-06T00:00:00Z"),
            "weather_run_time": pd.Timestamp("2025-01-04T23:00:00Z"),
            "weather_available_at": pd.Timestamp("2025-01-05T00:00:00Z"),
            "lead_hours": 24,
            "wind_speed_10m": 5.0,
            "wind_speed_100m": 7.0,
            "wind_u_100m": 3.0,
            "wind_v_100m": 4.0,
            "temperature_2m": 10.0,
            "surface_pressure": 1000.0,
            "gust_speed": 9.0,
            "power": 0.2,
        }
    )
    return pd.DataFrame(rows)


def test_rolling_evaluation_excludes_pre_cutoff_forecast_origins():
    config = EvaluationConfig(
        feature_columns=tuple(FEATURE_COLUMNS),
        numeric_features=tuple(NUMERIC_FEATURES),
        categorical_features=tuple(CAT_FEATURES),
        candidates=(Candidate("seasonal_mean", "baseline", {}),),
        tune_folds=(
            RollingFold(
                "jan4", "2025-01-04T00:00:00Z", "2025-01-04T00:00:00Z", "2025-01-05T00:00:00Z"
            ),
            RollingFold(
                "jan5", "2025-01-05T00:00:00Z", "2025-01-05T00:00:00Z", "2025-01-06T00:00:00Z"
            ),
        ),
        frozen_train_label_cutoff="2025-01-06T00:00:00Z",
        holdout_start="2025-01-06T00:00:00Z",
        holdout_end="2025-01-07T00:00:00Z",
        operational_origin="2025-01-07T00:00:00Z",
        operational_refit_label_cutoff="2025-01-07T00:00:00Z",
    )
    result = run_evaluation(_weather_rows(), config)
    holdout = result["frozen_holdout_family_winners"][0]["metrics"]["overall"]
    # The cutoff excludes target midnight (its lead-1 origin is pre-cutoff), leaving
    # 23 valid hourly origins per turbine; the added lead-24 overlap is excluded too.
    assert holdout["rows"] == 46
    assert result["selected_by_tuning_only"]["name"] == "baseline"


def test_holdout_winner_cannot_replace_validation_winner(monkeypatch):
    from wind_forecast import evaluation

    data = _weather_rows()
    data.loc[data.target_time >= pd.Timestamp("2025-01-06T00:00:00Z"), "power"] = 0.9
    config = EvaluationConfig(
        feature_columns=tuple(FEATURE_COLUMNS),
        numeric_features=tuple(NUMERIC_FEATURES),
        categorical_features=tuple(CAT_FEATURES),
        candidates=(
            Candidate("seasonal_mean", "validation_winner", {}),
            Candidate("power_curve", "holdout_winner", {}),
        ),
        tune_folds=(
            RollingFold(
                "jan4", "2025-01-04T00:00:00Z", "2025-01-04T00:00:00Z", "2025-01-05T00:00:00Z"
            ),
        ),
        frozen_train_label_cutoff="2025-01-06T00:00:00Z",
        holdout_start="2025-01-06T00:00:00Z",
        holdout_end="2025-01-07T00:00:00Z",
        operational_origin="2025-01-07T00:00:00Z",
        operational_refit_label_cutoff="2025-01-07T00:00:00Z",
    )

    class ConstantModel:
        def __init__(self, value):
            self.value = value

        def predict(self, X):
            return np.full(len(X), self.value)

    fitted_names = []

    def fake_fit(candidate, train, config):
        fitted_names.append(candidate.name)
        return ConstantModel(0.4 if candidate.name == "validation_winner" else 0.9)

    monkeypatch.setattr(evaluation, "_fit", fake_fit)
    result = run_evaluation(data, config)
    actual_holdout_winner = min(
        result["frozen_holdout_family_winners"], key=lambda row: row["metrics"]["overall"]["mae"]
    )
    assert actual_holdout_winner["name"] == "holdout_winner"
    assert result["selected_by_tuning_only"]["name"] == "validation_winner"
    assert fitted_names[-1] == "validation_winner"  # Final refit keeps the frozen decision.


def test_evaluation_rejects_naive_source_timestamps_and_invalid_split_order():
    config = EvaluationConfig(
        feature_columns=tuple(FEATURE_COLUMNS),
        numeric_features=tuple(NUMERIC_FEATURES),
        categorical_features=tuple(CAT_FEATURES),
        candidates=(Candidate("seasonal_mean", "baseline", {}),),
    )
    naive = _weather_rows()
    naive["weather_available_at"] = naive["weather_available_at"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="weather_available_at.*naive"):
        _prepare_dataset(naive, config)

    invalid = EvaluationConfig(
        feature_columns=tuple(FEATURE_COLUMNS),
        numeric_features=tuple(NUMERIC_FEATURES),
        categorical_features=tuple(CAT_FEATURES),
        candidates=(Candidate("seasonal_mean", "baseline", {}),),
        tune_folds=(
            RollingFold(
                "bad", "2025-10-02T00:00:00Z", "2025-10-01T00:00:00Z", "2025-10-03T00:00:00Z"
            ),
        ),
    )
    with pytest.raises(ValueError, match="train_label_cutoff"):
        evaluation_plan(invalid)


def test_metric_uses_configured_target_column_not_hardcoded_power():
    rows = pd.DataFrame(
        {
            "forecast_origin": pd.to_datetime(["2025-01-01T00:00:00Z"]),
            "target_time": pd.to_datetime(["2025-01-01T01:00:00Z"]),
            "turbine_id": ["1"],
            "lead_hours": [1],
            "label": [0.25],
        }
    )
    assert _metric_block(rows, np.array([0.25]), np.array([0.25]), "label")["mae"] == 0.0


def test_horizon_coverage_uses_24_expected_cells_not_48():
    origin = pd.Timestamp("2025-01-01T00:00:00Z")
    rows = pd.DataFrame(
        {
            "forecast_origin": [origin] * 48,
            "target_time": pd.date_range(origin, periods=49, freq="h")[1:],
            "turbine_id": ["1"] * 48,
            "lead_hours": range(1, 49),
            "power": [0.25] * 48,
        }
    )
    prediction = np.full(48, 0.25)
    metrics = _metrics(rows, prediction, prediction)
    assert metrics["overall"]["expected_forecast_cells"] == 48
    for horizon in metrics["by_horizon"].values():
        assert horizon["expected_forecast_cells"] == 24
        assert horizon["observed_forecast_cells"] == 24


def test_evaluation_rejects_feature_type_drift_and_records_weather_models(tmp_path):
    config = EvaluationConfig(
        feature_columns=tuple(FEATURE_COLUMNS),
        numeric_features=tuple(NUMERIC_FEATURES),
        categorical_features=tuple(CAT_FEATURES),
        candidates=(Candidate("seasonal_mean", "baseline", {}),),
        tune_folds=(
            RollingFold(
                "jan4", "2025-01-04T00:00:00Z", "2025-01-04T00:00:00Z", "2025-01-05T00:00:00Z"
            ),
            RollingFold(
                "jan5", "2025-01-05T00:00:00Z", "2025-01-05T00:00:00Z", "2025-01-06T00:00:00Z"
            ),
        ),
        frozen_train_label_cutoff="2025-01-06T00:00:00Z",
        holdout_start="2025-01-06T00:00:00Z",
        holdout_end="2025-01-07T00:00:00Z",
        operational_origin="2025-01-07T00:00:00Z",
        operational_refit_label_cutoff="2025-01-07T00:00:00Z",
    )
    drifted = EvaluationConfig(
        feature_columns=config.feature_columns,
        numeric_features=(*NUMERIC_FEATURES, "turbine_id"),
        categorical_features=(),
        candidates=config.candidates,
    )
    with pytest.raises(ValueError, match="numeric/categorical"):
        _prepare_dataset(_weather_rows(), drifted)

    result = run_evaluation(_weather_rows().assign(weather_model="gfs.0p25"), config, tmp_path)
    artifact = ForecastArtifact.load(result["artifacts"]["pickle"])
    assert artifact.weather_contract["trained_weather_models"] == ["gfs.0p25"]
    assert not any(isinstance(value, list) for value in artifact.metrics.values())
    assert (tmp_path / "candidate_tuning.csv").exists()
    leaderboard = pd.read_csv(tmp_path / "leaderboard.csv")
    assert {"mean_validation_mae", "holdout_mae", "holdout_rmse", "holdout_bias"}.issubset(
        leaderboard
    )
    holdout_predictions = pd.read_parquet(tmp_path / "holdout_predictions.parquet")
    assert {
        "turbine_id",
        "forecast_origin",
        "target_time",
        "weather_run_time",
        "weather_available_at",
        "lead_hours",
        "power_actual",
        "power_prediction",
        "model_family",
        "model_name",
    }.issubset(holdout_predictions)

    with pytest.raises(ValueError, match="weather_model"):
        _prepare_dataset(_weather_rows().assign(weather_model=""), config)
