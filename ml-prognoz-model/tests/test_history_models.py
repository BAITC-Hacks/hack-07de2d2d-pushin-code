from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wind_forecast.history_models import (
    HORIZON,
    Candidate,
    SeasonalMedianRegressor,
    build_estimator,
    candidate_catalogue,
)


def _tiny_history() -> tuple[pd.DataFrame, np.ndarray]:
    rows = 12
    X = pd.DataFrame(
        {
            "power_lag_1": np.linspace(0.05, 0.6, rows),
            "power_lag_2": np.linspace(0.04, 0.5, rows),
            "rolling_power_mean_6": np.linspace(0.03, 0.55, rows),
            "origin_hour_utc": np.arange(rows) % 24,
            "origin_month_utc": [1, 2, 3] * 4,
            "turbine_id": ["1", "2"] * 6,
        }
    )
    X.index = np.arange(100, 100 + rows * 2, 2)
    Y = np.column_stack([np.clip(X["power_lag_1"] + hour * 0.001, 0, 1) for hour in range(HORIZON)])
    return X, Y


def test_catalogue_has_exactly_fourteen_unique_practical_candidates():
    catalogue = candidate_catalogue()
    assert len(catalogue) == 14
    assert len({(candidate.family, candidate.name) for candidate in catalogue}) == 14
    assert {candidate.family for candidate in catalogue} == {
        "persistence",
        "seasonal_median",
        "ridge",
        "extra_trees",
        "random_forest",
        "catboost",
    }


@pytest.mark.parametrize(
    "candidate",
    [
        Candidate("persistence", "tiny_persistence", {}),
        Candidate("seasonal_median", "tiny_seasonal", {}),
        Candidate("ridge", "tiny_ridge", {"alpha": 1.0}),
        Candidate(
            "extra_trees", "tiny_extra", {"n_estimators": 3, "min_samples_leaf": 1, "max_depth": 3}
        ),
        Candidate(
            "random_forest",
            "tiny_forest",
            {"n_estimators": 3, "min_samples_leaf": 1, "max_depth": 3},
        ),
        Candidate(
            "catboost",
            "tiny_catboost",
            {"iterations": 2, "depth": 2, "learning_rate": 0.1, "l2_leaf_reg": 1.0},
        ),
    ],
)
def test_each_family_fits_and_returns_finite_direct_48_vectors(candidate: Candidate):
    X, Y = _tiny_history()
    estimator = build_estimator(
        candidate,
        numeric_features=[
            "power_lag_1",
            "power_lag_2",
            "rolling_power_mean_6",
            "origin_hour_utc",
            "origin_month_utc",
        ],
        categorical_features=["turbine_id"],
        random_state=7,
        n_jobs=1,
    )
    estimator.fit(X, Y)
    prediction = np.asarray(estimator.predict(X), dtype=float)
    assert prediction.shape == (len(X), HORIZON)
    assert np.isfinite(prediction).all()
    if candidate.family == "persistence":
        assert np.allclose(prediction, X["power_lag_1"].to_numpy()[:, None])


def test_direct_target_shape_and_thread_count_are_checked():
    X, Y = _tiny_history()
    estimator = build_estimator(
        Candidate("persistence", "p", {}), ["power_lag_1"], ["turbine_id"], n_jobs=1
    )
    with pytest.raises(ValueError, match="shape"):
        estimator.fit(X, Y[:, :2])
    with pytest.raises(ValueError, match="n_jobs"):
        build_estimator(Candidate("ridge", "r", {"alpha": 1.0}), ["power_lag_1"], [], n_jobs=0)


def test_seasonal_median_uses_actual_target_calendar_keys():
    X = pd.DataFrame(
        {
            "turbine_id": ["1", "1", "1", "1"],
            "target_hour_utc": [1, 1, 2, 2],
            "target_month_utc": [1, 1, 1, 1],
        }
    )
    Y = np.vstack([np.zeros(HORIZON), np.zeros(HORIZON), np.ones(HORIZON), np.ones(HORIZON)])
    model = SeasonalMedianRegressor().fit(X, Y)
    prediction = model.predict(X.iloc[[0, 2]])
    assert np.allclose(prediction[0], 0.0)
    assert np.allclose(prediction[1], 1.0)
