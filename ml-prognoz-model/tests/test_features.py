from __future__ import annotations

import pandas as pd
import pytest

from wind_forecast.features import (
    CAT_FEATURES,
    FEATURE_COLUMNS,
    NUMERIC_FEATURES,
    build_features,
    build_training_table,
)


def _weather():
    return pd.DataFrame(
        {
            "turbine_id": ["1"],
            "forecast_origin": pd.to_datetime(["2025-01-01 00:00Z"], utc=True),
            "target_time": pd.to_datetime(["2025-01-01 06:00Z"], utc=True),
            "weather_run_time": pd.to_datetime(["2024-12-31 23:00Z"], utc=True),
            "weather_available_at": pd.to_datetime(["2025-01-01 00:00Z"], utc=True),
            "lead_hours": [6],
            "wind_speed_10m": [4.0],
            "wind_speed_100m": [7.0],
            "wind_u_100m": [3.0],
            "wind_v_100m": [-2.0],
            "temperature_2m": [11.0],
            "surface_pressure": [101325.0],
            "gust_speed": [9.0],
        }
    )


def test_feature_schema_is_stable_and_excludes_observations():
    frame = _weather().assign(power=0.7, observed_wind=99.0, observed_temperature=99.0)

    features = build_features(frame)

    assert list(features.columns) == FEATURE_COLUMNS
    assert "power" not in features
    assert "observed_wind" not in features
    assert features.iloc[0]["run_age_hours"] == 1.0
    assert features.iloc[0]["target_hour_sin"] == 1.0
    assert features.iloc[0]["wind_direction_sin"] > 0
    assert set(NUMERIC_FEATURES).isdisjoint(CAT_FEATURES)


def test_training_join_keeps_only_complete_valid_labels_and_target_separate():
    weather = _weather()
    hourly = pd.DataFrame(
        {
            "turbine_id": ["1", "1"],
            "target_time": pd.to_datetime(["2025-01-01 06:00Z", "2025-01-01 07:00Z"], utc=True),
            "power": [0.7, 0.3],
            "complete_hour": [True, False],
        }
    )

    training = build_training_table(hourly, weather)

    assert training["power"].tolist() == [0.7]
    assert list(build_features(training).columns) == FEATURE_COLUMNS


def test_weather_must_have_been_available_at_origin():
    weather = _weather()
    weather.loc[0, "weather_available_at"] = pd.Timestamp("2025-01-01 00:01Z")

    with pytest.raises(ValueError, match="weather_available_at"):
        build_features(weather)


def test_weather_rejects_chronological_leakage_and_wrong_horizon():
    weather = _weather()
    weather.loc[0, "weather_run_time"] = pd.Timestamp("2025-01-01 00:30Z")
    with pytest.raises(ValueError, match="weather_run_time"):
        build_features(weather)

    weather = _weather()
    weather.loc[0, "lead_hours"] = 5
    with pytest.raises(ValueError, match="lead_hours must exactly"):
        build_features(weather)


def test_weather_rejects_duplicate_forecasts_even_for_inference():
    weather = pd.concat([_weather(), _weather()], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        build_features(weather)
