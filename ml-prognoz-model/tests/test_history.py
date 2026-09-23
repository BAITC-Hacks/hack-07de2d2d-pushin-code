from __future__ import annotations

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from wind_forecast.history import TARGET_COLUMNS, build_history_features, build_supervised_history


def _hourly(hours: int = 280) -> pd.DataFrame:
    times = pd.date_range("2025-01-01", periods=hours, freq="h", tz="UTC")
    values = np.arange(hours, dtype=float)
    return pd.DataFrame(
        {
            "turbine_id": "1",
            "target_time": times,
            "power": values / 1000,
            "observed_wind": values + 10,
            "observed_temperature": values - 20,
            "n_samples": 6,
            "complete_hour": True,
        }
    )


def test_features_use_only_the_observation_prefix() -> None:
    hourly = _hourly()
    origin = hourly.loc[220, "target_time"]
    expected = build_history_features(hourly, [origin])
    changed = hourly.copy()
    future = changed["target_time"].ge(origin)
    changed.loc[future, ["power", "observed_wind", "observed_temperature"]] = [0.99, 9999, -9999]
    actual = build_history_features(changed, [origin])
    pdt.assert_frame_equal(actual, expected)


def test_hour_boundary_and_direct_label_alignment() -> None:
    hourly = _hourly()
    origin = hourly.loc[200, "target_time"]
    featured = build_history_features(hourly, [origin]).iloc[0]
    assert featured["power_lag_1"] == pytest.approx(hourly.loc[199, "power"])
    assert featured["target_hour_utc"] == origin.hour
    supervised = build_supervised_history(hourly, [origin]).iloc[0]
    assert tuple(TARGET_COLUMNS) == tuple(f"y_{item:02d}" for item in range(1, 49))
    assert supervised["y_01"] == pytest.approx(hourly.loc[200, "power"])
    assert supervised["y_48"] == pytest.approx(hourly.loc[247, "power"])


def test_recent_gap_rejected_but_old_gap_is_exposed_without_imputation() -> None:
    hourly = _hourly()
    origin = hourly.loc[220, "target_time"]
    recent_gap = hourly.copy()
    recent_gap.loc[208, ["complete_hour", "n_samples"]] = [False, 5]
    with pytest.raises(ValueError, match="last 24 complete"):
        build_history_features(recent_gap, [origin])

    old_gap = hourly.copy()
    old_gap.loc[172, ["complete_hour", "n_samples"]] = [False, 5]
    featured = build_history_features(old_gap, [origin]).iloc[0]
    assert np.isnan(featured["power_lag_48"])
    assert featured["history_coverage_48"] < 1.0


def test_duplicate_and_naive_times_are_rejected() -> None:
    hourly = _hourly()
    duplicate = pd.concat([hourly, hourly.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        build_history_features(duplicate, [hourly.loc[200, "target_time"]])

    naive_hourly = hourly.copy()
    naive_hourly["target_time"] = naive_hourly["target_time"].dt.tz_localize(None)
    with pytest.raises(ValueError, match=r"datetime64\[ns, UTC\]"):
        build_history_features(naive_hourly, [hourly.loc[200, "target_time"]])
    with pytest.raises(ValueError, match="naive"):
        build_history_features(hourly, [pd.Timestamp("2025-01-09 08:00")])


def test_supervised_requires_all_48_complete_future_targets() -> None:
    hourly = _hourly()
    origin = hourly.loc[200, "target_time"]
    missing_future = hourly.copy()
    missing_future.loc[230, ["complete_hour", "n_samples"]] = [False, 5]
    result = build_supervised_history(missing_future, [origin])
    assert result.empty


def test_supervised_drops_only_origins_with_incomplete_recent_history() -> None:
    hourly = _hourly()
    bad_origin = hourly.loc[200, "target_time"]
    good_origin = hourly.loc[230, "target_time"]
    hourly.loc[190, ["complete_hour", "n_samples"]] = [False, 5]

    # Public inference remains fail-closed when any requested origin has a gap.
    with pytest.raises(ValueError, match="last 24 complete"):
        build_history_features(hourly, [bad_origin, good_origin])

    supervised = build_supervised_history(hourly, [bad_origin, good_origin])
    assert supervised["forecast_origin"].tolist() == [good_origin]
