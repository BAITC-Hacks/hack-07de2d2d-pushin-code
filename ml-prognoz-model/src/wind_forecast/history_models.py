"""Direct multi-horizon estimators for past-only turbine-history features.

The module contains model construction only.  It neither reads observations nor
fits a model at import time.  Every estimator predicts a vector of 48 future
normalized-power values from one origin row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

HORIZON = 48
PERSISTENCE_FEATURE = "power_lag_1"


@dataclass(frozen=True)
class Candidate:
    """One fixed, reproducible direct-forecast model configuration."""

    family: str
    name: str
    params: Mapping[str, Any]


def candidate_catalogue() -> tuple[Candidate, ...]:
    """Return the practical fixed 14-candidate history-only catalogue."""
    return (
        Candidate("persistence", "persistence_last_power", {}),
        Candidate("seasonal_median", "seasonal_median_turbine_origin_calendar", {}),
        Candidate("ridge", "ridge_alpha_0.1", {"alpha": 0.1}),
        Candidate("ridge", "ridge_alpha_1", {"alpha": 1.0}),
        Candidate("ridge", "ridge_alpha_10", {"alpha": 10.0}),
        Candidate(
            "extra_trees",
            "extra_trees_150_leaf_4_depth_16",
            {"n_estimators": 150, "min_samples_leaf": 4, "max_depth": 16},
        ),
        Candidate(
            "extra_trees",
            "extra_trees_200_leaf_12_depth_16",
            {"n_estimators": 200, "min_samples_leaf": 12, "max_depth": 16},
        ),
        Candidate(
            "extra_trees",
            "extra_trees_200_leaf_24_depth_12",
            {"n_estimators": 200, "min_samples_leaf": 24, "max_depth": 12},
        ),
        Candidate(
            "random_forest",
            "random_forest_120_leaf_8_depth_14",
            {"n_estimators": 120, "min_samples_leaf": 8, "max_depth": 14},
        ),
        Candidate(
            "random_forest",
            "random_forest_150_leaf_16_depth_12",
            {"n_estimators": 150, "min_samples_leaf": 16, "max_depth": 12},
        ),
        Candidate(
            "random_forest",
            "random_forest_150_leaf_32_depth_10",
            {"n_estimators": 150, "min_samples_leaf": 32, "max_depth": 10},
        ),
        Candidate(
            "catboost",
            "catboost_250_depth_4",
            {"iterations": 250, "depth": 4, "learning_rate": 0.05, "l2_leaf_reg": 3.0},
        ),
        Candidate(
            "catboost",
            "catboost_350_depth_5",
            {"iterations": 350, "depth": 5, "learning_rate": 0.04, "l2_leaf_reg": 5.0},
        ),
        Candidate(
            "catboost",
            "catboost_400_depth_6",
            {"iterations": 400, "depth": 6, "learning_rate": 0.03, "l2_leaf_reg": 8.0},
        ),
    )


def _as_2d_target(y):
    import numpy as np

    values = np.asarray(y, dtype=float)
    if values.ndim != 2 or values.shape[1] != HORIZON:
        raise ValueError(f"History direct models require y with shape (n_rows, {HORIZON}).")
    if not np.isfinite(values).all():
        raise ValueError("History training targets must be finite.")
    return values


class PersistenceRegressor:
    """Repeat the most recently known normalized power across all 48 horizons."""

    def __init__(self, lag_feature: str = PERSISTENCE_FEATURE):
        self.lag_feature = lag_feature

    def fit(self, X, y):
        _as_2d_target(y)
        if self.lag_feature not in X.columns:
            raise ValueError(f"Persistence baseline requires {self.lag_feature!r}.")
        return self

    def predict(self, X):
        import numpy as np
        import pandas as pd

        if self.lag_feature not in X.columns:
            raise ValueError(f"Persistence baseline requires {self.lag_feature!r}.")
        values = pd.to_numeric(X[self.lag_feature], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("Persistence prediction requires finite power_lag_1 values.")
        return np.repeat(values[:, None], HORIZON, axis=1)


class SeasonalMedianRegressor:
    """Per-turbine origin-calendar vector median with a global vector fallback."""

    _CALENDAR_KEYS = (
        ("target_hour_utc", "target_month_utc"),
        ("origin_hour_utc", "origin_month_utc"),
        ("origin_hour", "origin_month"),
    )

    @staticmethod
    def _keys(X) -> tuple[str, ...]:
        for hour, month in SeasonalMedianRegressor._CALENDAR_KEYS:
            if hour in X.columns and month in X.columns:
                return tuple(key for key in ("turbine_id", hour, month) if key in X.columns)
        return ("turbine_id",) if "turbine_id" in X.columns else ()

    def fit(self, X, y):
        import numpy as np

        values = _as_2d_target(y)
        self.keys_ = self._keys(X)
        self.global_median_ = np.median(values, axis=0)
        self.medians_: dict[tuple[str, ...], Any] = {}
        if self.keys_:
            key_frame = X.loc[:, self.keys_].fillna("__MISSING__").astype(str)
            # ``indices`` are positional, unlike ``groups``' dataframe index
            # labels, and therefore stay aligned to the NumPy target matrix.
            for key, positions in key_frame.groupby(list(self.keys_), dropna=False).indices.items():
                key_tuple = key if isinstance(key, tuple) else (key,)
                self.medians_[key_tuple] = np.median(values[positions], axis=0)
        return self

    def predict(self, X):
        import numpy as np

        if not self.keys_:
            return np.repeat(self.global_median_[None, :], len(X), axis=0)
        key_frame = X.loc[:, self.keys_].fillna("__MISSING__").astype(str)
        rows = []
        for row in key_frame.itertuples(index=False, name=None):
            key = tuple(row)
            rows.append(self.medians_.get(key, self.global_median_))
        return np.asarray(rows, dtype=float)


def _validate_features(
    numeric_features: Sequence[str], categorical_features: Sequence[str], n_jobs: int
) -> None:
    numeric, categorical = tuple(numeric_features), tuple(categorical_features)
    if len(set(numeric)) != len(numeric) or len(set(categorical)) != len(categorical):
        raise ValueError("numeric_features and categorical_features cannot contain duplicates.")
    if set(numeric).intersection(categorical):
        raise ValueError("numeric_features and categorical_features must be disjoint.")
    if isinstance(n_jobs, bool) or not isinstance(n_jobs, int) or n_jobs < 1:
        raise ValueError("n_jobs must be a positive integer.")


def _sklearn_preprocessor(
    numeric: Sequence[str], categorical: Sequence[str], *, impute_numeric: bool
):
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    transforms = []
    if numeric:
        numeric_steps = []
        if impute_numeric:
            numeric_steps.append(("impute", SimpleImputer(strategy="median")))
        numeric_steps.append(("scale", StandardScaler()))
        transforms.append(("numeric", Pipeline(numeric_steps), list(numeric)))
    if categorical:
        transforms.append(
            (
                "categorical",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="constant", fill_value="__MISSING__")),
                        ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
                    ]
                ),
                list(categorical),
            )
        )
    if not transforms:
        raise ValueError("At least one numeric or categorical history feature is required.")
    return ColumnTransformer(transforms, remainder="drop")


def build_estimator(
    candidate: Candidate,
    numeric_features: Sequence[str],
    categorical_features: Sequence[str],
    *,
    random_state: int = 42,
    n_jobs: int = 4,
):
    """Build a deterministic estimator that accepts X and produces ``(n, 48)``."""
    _validate_features(numeric_features, categorical_features, n_jobs)
    family, params = candidate.family, dict(candidate.params)
    if family == "persistence":
        return PersistenceRegressor(**params)
    if family == "seasonal_median":
        return SeasonalMedianRegressor()

    try:
        from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import Pipeline
    except ImportError as exc:
        raise RuntimeError("scikit-learn is required for history model candidates.") from exc

    numeric, categorical = list(numeric_features), list(categorical_features)
    if family == "ridge":
        return Pipeline(
            [
                ("preprocess", _sklearn_preprocessor(numeric, categorical, impute_numeric=True)),
                ("model", Ridge(**params)),
            ]
        )
    if family == "extra_trees":
        return Pipeline(
            [
                ("preprocess", _sklearn_preprocessor(numeric, categorical, impute_numeric=True)),
                ("model", ExtraTreesRegressor(random_state=random_state, n_jobs=n_jobs, **params)),
            ]
        )
    if family == "random_forest":
        return Pipeline(
            [
                ("preprocess", _sklearn_preprocessor(numeric, categorical, impute_numeric=True)),
                (
                    "model",
                    RandomForestRegressor(random_state=random_state, n_jobs=n_jobs, **params),
                ),
            ]
        )
    if family == "catboost":
        try:
            from catboost import CatBoostRegressor
        except ImportError as exc:
            raise RuntimeError(
                "CatBoost candidate selected but catboost is not installed."
            ) from exc
        # CatBoost accepts missing numerical values. Categories are one-hot
        # encoded so its fit/predict interface is identical to sklearn models.
        return Pipeline(
            [
                ("preprocess", _sklearn_preprocessor(numeric, categorical, impute_numeric=False)),
                (
                    "model",
                    CatBoostRegressor(
                        loss_function="MultiRMSE",
                        verbose=False,
                        random_seed=random_state,
                        thread_count=n_jobs,
                        allow_writing_files=False,
                        **params,
                    ),
                ),
            ]
        )
    raise ValueError(f"Unknown history candidate family: {family}")
