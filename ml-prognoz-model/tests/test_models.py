from __future__ import annotations

import pickle

import numpy as np
import pandas as pd
import pytest

from wind_forecast.evaluation import EvaluationConfig, _fit
from wind_forecast.features import CAT_FEATURES, FEATURE_COLUMNS, NUMERIC_FEATURES, build_features
from wind_forecast.models import Candidate, build_estimator


def _tiny_features() -> tuple[pd.DataFrame, pd.Series]:
    origins = pd.date_range("2025-01-01T00:00:00Z", periods=8, freq="h")
    raw = pd.DataFrame(
        {
            "turbine_id": ["1", "2"] * 4,
            "forecast_origin": origins,
            "target_time": origins + pd.Timedelta(1, unit="h"),
            "weather_run_time": origins - pd.Timedelta(6, unit="h"),
            "weather_available_at": origins - pd.Timedelta(1, unit="h"),
            "lead_hours": [1] * 8,
            "wind_speed_10m": np.linspace(2, 9, 8),
            "wind_speed_100m": np.linspace(3, 10, 8),
            "wind_u_100m": np.linspace(1, 8, 8),
            "wind_v_100m": np.linspace(-2, 5, 8),
            "temperature_2m": np.linspace(0, 7, 8),
            "surface_pressure": [100000.0] * 8,
            "gust_speed": np.linspace(4, 11, 8),
        }
    )
    return build_features(raw), pd.Series(np.linspace(0.1, 0.8, 8))


@pytest.mark.parametrize(
    "candidate",
    [
        Candidate("seasonal_mean", "seasonal", {}),
        Candidate("power_curve", "curve", {"n_bins": 3}),
        Candidate("ridge", "ridge", {"alpha": 1.0, "degree": 2}),
        Candidate("extra_trees", "trees", {"n_estimators": 3, "min_samples_leaf": 1}),
        Candidate("hist_gradient_boosting", "hgb", {"max_iter": 2, "max_leaf_nodes": 3}),
    ],
)
def test_estimator_families_fit_predict_and_pickle_round_trip(candidate: Candidate):
    pytest.importorskip("sklearn")
    X, y = _tiny_features()
    estimator = build_estimator(candidate, NUMERIC_FEATURES, CAT_FEATURES, n_jobs=1)
    estimator.fit(X, y)
    restored = pickle.loads(pickle.dumps(estimator))
    prediction = np.asarray(restored.predict(X), dtype=float)
    assert prediction.shape == (len(X),)
    assert np.isfinite(prediction).all()


def test_catboost_fit_predict_and_pickle_round_trip_with_categorical_names():
    pytest.importorskip("catboost")
    X, y = _tiny_features()
    candidate = Candidate("catboost", "cat", {"iterations": 2, "depth": 2, "learning_rate": 0.1})
    estimator = build_estimator(candidate, NUMERIC_FEATURES, CAT_FEATURES, n_jobs=1)
    X = X.copy()
    X["turbine_id"] = X["turbine_id"].astype(str)
    estimator.fit(X, y, cat_features=["turbine_id"])
    restored = pickle.loads(pickle.dumps(estimator))
    prediction = np.asarray(restored.predict(X), dtype=float)
    assert prediction.shape == (len(X),)
    assert np.isfinite(prediction).all()


def test_hgb_fit_honours_n_jobs_via_threadpool_limits(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("sklearn")
    import sys
    import types

    entered: list[int] = []

    class _Limits:
        def __init__(self, limits: int):
            self.limits = limits

        def __enter__(self):
            entered.append(self.limits)

        def __exit__(self, *args):
            return False

    monkeypatch.setitem(
        sys.modules, "threadpoolctl", types.SimpleNamespace(threadpool_limits=_Limits)
    )
    X, y = _tiny_features()
    train = X.copy()
    train["power"] = y
    config = EvaluationConfig(
        feature_columns=tuple(FEATURE_COLUMNS),
        numeric_features=tuple(NUMERIC_FEATURES),
        categorical_features=tuple(CAT_FEATURES),
        n_jobs=1,
        candidates=(
            Candidate("hist_gradient_boosting", "hgb", {"max_iter": 2, "max_leaf_nodes": 3}),
        ),
    )
    _fit(config.candidates[0], train, config)
    assert entered == [1]


def test_estimator_rejects_invalid_thread_count_before_constructing_model():
    with pytest.raises(ValueError, match="n_jobs"):
        build_estimator(
            Candidate("seasonal_mean", "seasonal", {}), NUMERIC_FEATURES, CAT_FEATURES, n_jobs=0
        )
