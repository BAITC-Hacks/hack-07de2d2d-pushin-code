from __future__ import annotations

import sys
import types

import numpy as np
import pandas as pd
import pytest

from wind_forecast.history_artifact import HistoryForecastArtifact


class _NativeModel:
    def predict(self, X):
        return np.full((len(X), 48), 1.2)


def _install_history(monkeypatch):
    module = types.ModuleType("wind_forecast.history")
    module.FEATURE_COLUMNS = ("power_lag_1", "turbine_id")

    def build_history_features(hourly, origins):
        origin = pd.Timestamp(origins[0])
        past = hourly.loc[hourly["target_time"] < origin]
        turbines = sorted(past["turbine_id"].astype(str).unique())
        return pd.DataFrame(
            {
                "forecast_origin": [origin] * len(turbines),
                "turbine_id": turbines,
                "power_lag_1": [
                    past.loc[past.turbine_id.astype(str) == turbine, "power"].iloc[-1]
                    for turbine in turbines
                ],
            }
        )

    module.build_history_features = build_history_features
    monkeypatch.setitem(sys.modules, "wind_forecast.history", module)


def test_native48_artifact_ignores_future_measurements_and_roundtrips(monkeypatch, tmp_path):
    _install_history(monkeypatch)
    origin = pd.Timestamp("2026-02-01T19:00:00Z")
    hourly = pd.DataFrame(
        {
            "turbine_id": ["1", "2", "1", "2"],
            "target_time": [
                origin - pd.Timedelta(1, unit="h"),
                origin - pd.Timedelta(1, unit="h"),
                origin,
                origin,
            ],
            "power": [0.3, 0.4, 0.99, 0.01],
        }
    )
    artifact = HistoryForecastArtifact(
        _NativeModel(),
        ("power_lag_1", "turbine_id"),
        origin,
        {"family": "dummy"},
        {"trained_turbines": ["1", "2"]},
    )
    first = artifact.predict(hourly, origin)
    changed = hourly.copy()
    changed.loc[changed.target_time >= origin, "power"] = [0.0, 1.0]
    second = artifact.predict(changed, origin)
    assert len(first) == 96 and first.equals(second)
    assert first.power_prediction.eq(1.0).all()
    path, _ = artifact.save(tmp_path / "best_model.pkl")
    assert len(HistoryForecastArtifact.load(path).predict(hourly, origin)) == 96
    with pytest.raises(ValueError, match="time travel"):
        artifact.predict(hourly, origin - pd.Timedelta(1, unit="h"))
