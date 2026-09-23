from __future__ import annotations

import numpy as np
import pandas as pd

from windcast.evaluate import _baseline_values
from windcast.model import (
    MODEL_VERSION,
    artifact_path,
    build_feature_frame,
    load_artifact,
    predict,
    save_artifact,
    train_artifact,
    training_frame,
)


def _weather() -> dict:
    targets = pd.date_range("2026-01-31T19:00Z", periods=48, freq="h")
    return {
        "issue_time_utc": pd.Timestamp("2026-01-31T19:00Z"),
        "hourly": pd.DataFrame(
            {
                "h": range(1, 49),
                "target_time_utc": targets,
                "wind_100m_ms": np.linspace(3, 14, 48),
                "wind_10m_ms": np.linspace(2, 10, 48),
                "wind_dir_deg": np.linspace(0, 350, 48),
                "temp_c": np.linspace(-8, 4, 48),
                "init_time_utc": [
                    target - pd.Timedelta(days=1 if h <= 24 else 2)
                    for h, target in enumerate(targets, start=1)
                ],
            }
        ),
    }


def _training() -> pd.DataFrame:
    index = pd.date_range("2024-06-01", periods=60, freq="h", tz="UTC")
    rows = []
    for turbine, bonus in (("1", 0.0), ("2", 0.04)):
        for position, target in enumerate(index):
            wind = 3 + position / 8
            rows.append(
                {
                    "target_time_utc": target,
                    "turbine": turbine,
                    "power": min(1.0, wind / 12 + bonus),
                    "valid": True,
                    "wind_100m_ms": wind,
                    "wind_10m_ms": wind - 1,
                    "wind_dir_deg": position * 11 % 360,
                    "temp_c": -5 + position / 20,
                    "lag_days": 1 if position % 2 else 2,
                }
            )
    return pd.DataFrame(rows)


def test_weather_only_features_ignore_scada_inference_values() -> None:
    weather = _weather()
    first = build_feature_frame(weather["hourly"], turbine="1")
    changed = weather["hourly"].assign(power=0.99, scada_wind_ms=99.0)
    second = build_feature_frame(changed, turbine="1")
    pd.testing.assert_frame_equal(first, second)


def test_predict_returns_exact_monotone_bounded_144_rows(tmp_path) -> None:
    artifact = train_artifact(
        _training(), cutoff_utc="2024-07-01T00:00Z", offset_hours=5
    )
    artifact_path = tmp_path / "quantiles.pkl"
    save_artifact(artifact, artifact_path)
    result = predict("2026-01-31", _weather(), artifact_path=artifact_path)
    assert MODEL_VERSION
    assert len(result) == 144
    assert set(result["turbine"]) == {"1", "2", "plant"}
    assert (result["p10"] <= result["p50"]).all()
    assert (result["p50"] <= result["p90"]).all()
    assert result[["p10", "p50", "p90"]].ge(0).all().all()
    assert result[["p10", "p50", "p90"]].le(1).all().all()


def test_saved_artifact_reloads_deterministically(tmp_path) -> None:
    artifact = train_artifact(
        _training(), cutoff_utc="2024-07-01T00:00Z", offset_hours=5
    )
    path = tmp_path / "model.pkl"
    save_artifact(artifact, path)
    expected = predict("2026-01-31", _weather(), artifact_path=path)
    actual = predict("2026-01-31", _weather(), artifact_path=path)
    pd.testing.assert_frame_equal(expected, actual)
    assert load_artifact(path)["metadata"]["offset_hours"] == 5


def test_training_excludes_target_hour_at_cutoff_and_keeps_previous_hour() -> None:
    timestamps = pd.DatetimeIndex(
        [pd.Timestamp("2025-12-31T18:00Z"), pd.Timestamp("2025-12-31T19:00Z")]
    )
    hourly = pd.DataFrame(
        [
            {"ts_utc": time, "turbine": turbine, "power": 0.5, "valid": True}
            for turbine in ("1", "2")
            for time in timestamps
        ]
    )
    weather = pd.DataFrame(
        {
            "target_time_utc": list(timestamps) * 2,
            "wind_100m_ms": 6.0,
            "wind_10m_ms": 5.0,
            "wind_dir_deg": 180.0,
            "temp_c": -2.0,
            "lag_days": [1, 1, 2, 2],
        }
    )
    train = training_frame(hourly, weather, cutoff_utc="2025-12-31T19:00Z")
    assert set(pd.to_datetime(train["target_time_utc"], utc=True)) == {timestamps[0]}


def test_predict_rejects_future_trained_artifact_and_wrong_weather_window(
    tmp_path,
) -> None:
    artifact = train_artifact(
        _training(), cutoff_utc="2026-02-01T00:00Z", offset_hours=5
    )
    path = tmp_path / "future.pkl"
    save_artifact(artifact, path)
    with np.testing.assert_raises_regex(ValueError, "после момента выпуска"):
        predict("2026-01-31", _weather(), artifact_path=path)
    artifact["metadata"]["cutoff_utc"] = "2026-01-31T19:00Z"
    save_artifact(artifact, path)
    broken = _weather()
    broken["hourly"].loc[0, "target_time_utc"] = pd.Timestamp("2026-01-31T20:00Z")
    with np.testing.assert_raises_regex(ValueError, "Целевые часы"):
        predict("2026-01-31", broken, artifact_path=path)


def test_first_february_issue_uses_february_training_boundary() -> None:
    assert artifact_path("2026-01-30").name == "quantile_2025-12-31.pkl"
    assert artifact_path("2026-01-31").name == "quantile_2026-01-31.pkl"


def test_persistence_only_reads_pre_issue_observations() -> None:
    artifact = train_artifact(
        _training(), cutoff_utc="2024-07-01T00:00Z", offset_hours=5
    )
    issue = pd.Timestamp("2026-01-31T19:00Z")
    index = pd.date_range(issue - pd.Timedelta(hours=24), periods=72, freq="h")
    actual = pd.DataFrame({"power": 0.2, "valid": True}, index=index)
    changed = actual.copy()
    changed.loc[changed.index >= issue, "power"] = 0.99
    expected = _baseline_values(artifact, _weather()["hourly"], "1", issue, actual)
    observed = _baseline_values(artifact, _weather()["hourly"], "1", issue, changed)
    pd.testing.assert_series_equal(expected["persistence"], observed["persistence"])


def test_live_prediction_accepts_fetched_at_provenance(tmp_path) -> None:
    artifact = train_artifact(
        _training(), cutoff_utc="2024-07-01T00:00Z", offset_hours=5
    )
    path = tmp_path / "live.pkl"
    save_artifact(artifact, path)
    issue = pd.Timestamp("2026-02-01T00:30Z")
    targets = pd.date_range("2026-02-01T01:00Z", periods=48, freq="h")
    weather = _weather()
    weather["issue_time_utc"] = issue
    weather["hourly"] = weather["hourly"].assign(
        target_time_utc=targets, init_time_utc=issue
    )
    assert len(predict("live", weather, artifact_path=path)) == 144
