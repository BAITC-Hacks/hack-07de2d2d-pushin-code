from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wind_forecast.artifact import ForecastArtifact
from wind_forecast.features import FEATURE_COLUMNS


class _ConstantModel:
    def predict(self, X):
        self.columns = list(X.columns)
        return np.repeat(1.2, len(X))


class _NonFiniteModel:
    def predict(self, X):
        return np.repeat(np.nan, len(X))


def _raw_weather():
    origin = pd.Timestamp("2026-02-01T18:00:00Z")
    return pd.DataFrame(
        [
            {
                "turbine_id": "1",
                "forecast_origin": origin,
                "target_time": origin + pd.Timedelta(1, unit="h"),
                "weather_run_time": origin - pd.Timedelta(1, unit="h"),
                "weather_available_at": origin,
                "lead_hours": 1,
                "wind_speed_10m": 4.0,
                "wind_speed_100m": 6.0,
                "wind_u_100m": 3.0,
                "wind_v_100m": 4.0,
                "temperature_2m": 8.0,
                "surface_pressure": 1000.0,
                "gust_speed": 7.0,
            }
        ]
    )


def test_artifact_builds_features_from_raw_weather_and_clips(tmp_path):
    model = _ConstantModel()
    artifact = ForecastArtifact(
        model=model,
        feature_schema=FEATURE_COLUMNS,
        trained_until="2026-02-01T18:00:00Z",
        model_config={"family": "seasonal_mean"},
        weather_contract={"categorical_features": ["turbine_id"]},
    )
    prediction = artifact.predict(_raw_weather())
    assert prediction["power_prediction"].tolist() == [1.0]
    assert model.columns == FEATURE_COLUMNS
    assert prediction.attrs["raw_out_of_range_count"] == 1
    path, manifest = artifact.save(tmp_path / "best_model.pkl")
    assert manifest.exists()
    assert isinstance(ForecastArtifact.load(path), ForecastArtifact)


def test_artifact_rejects_time_travel_prediction():
    artifact = ForecastArtifact(_ConstantModel(), FEATURE_COLUMNS, "2026-02-01T19:00:00Z", {}, {})
    with pytest.raises(ValueError, match="trained after"):
        artifact.predict(_raw_weather())


def test_artifact_rejects_naive_weather_timestamps_before_utc_coercion():
    artifact = ForecastArtifact(_ConstantModel(), FEATURE_COLUMNS, "2026-02-01T18:00:00Z", {}, {})
    weather = _raw_weather()
    weather["forecast_origin"] = weather["forecast_origin"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="forecast_origin.*naive"):
        artifact.predict(weather)


def test_artifact_rejects_unknown_turbine_and_non_finite_model_prediction():
    artifact = ForecastArtifact(_ConstantModel(), FEATURE_COLUMNS, "2026-02-01T18:00:00Z", {}, {})
    unknown = _raw_weather()
    unknown.loc[0, "turbine_id"] = "unknown"
    with pytest.raises(ValueError, match="turbine_id"):
        artifact.predict(unknown)

    non_finite = ForecastArtifact(
        _NonFiniteModel(), FEATURE_COLUMNS, "2026-02-01T18:00:00Z", {}, {}
    )
    with pytest.raises(ValueError, match="non-finite"):
        non_finite.predict(_raw_weather())


def test_artifact_enforces_recorded_weather_model_only_when_contract_requires_it():
    artifact = ForecastArtifact(
        _ConstantModel(),
        FEATURE_COLUMNS,
        "2026-02-01T18:00:00Z",
        {},
        {"trained_weather_models": ["gfs.0p25"]},
    )
    weather = _raw_weather().assign(weather_model="other_provider")
    with pytest.raises(ValueError, match="incompatible"):
        artifact.predict(weather)

    accepted = _raw_weather().assign(weather_model="gfs.0p25")
    assert artifact.predict(accepted)["power_prediction"].tolist() == [1.0]


def test_artifact_rejects_nat_training_cutoff():
    with pytest.raises(ValueError, match="timezone-aware"):
        ForecastArtifact(_ConstantModel(), FEATURE_COLUMNS, pd.NaT, {}, {})
