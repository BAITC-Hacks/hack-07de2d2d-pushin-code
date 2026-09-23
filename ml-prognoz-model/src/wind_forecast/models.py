"""Small, deterministic model catalogue for direct 1--48 hour forecasts.

The module deliberately constructs estimators only; it never fits them at import
time.  Optional dependencies are imported when their candidate is selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

DEFAULT_WIND_COLUMNS = ("wind_speed_10m", "wind_speed_100m", "gust_speed")


@dataclass(frozen=True)
class Candidate:
    """One predeclared configuration, suitable for reproducible comparison."""

    family: str
    name: str
    params: Mapping[str, Any]


def candidate_catalogue(include_catboost: bool = True) -> tuple[Candidate, ...]:
    """Return the intentionally small, fixed search space (no random search)."""
    candidates = [
        Candidate("seasonal_mean", "seasonal_mean_hour_month", {}),
        Candidate("power_curve", "weather_binned_power_curve", {"n_bins": 24}),
        Candidate("ridge", "ridge_alpha_0.1", {"alpha": 0.1, "degree": 2}),
        Candidate("ridge", "ridge_alpha_1", {"alpha": 1.0, "degree": 2}),
        Candidate("ridge", "ridge_alpha_10", {"alpha": 10.0, "degree": 2}),
        Candidate("ridge", "ridge_alpha_1_degree_3", {"alpha": 1.0, "degree": 3}),
        Candidate(
            "extra_trees",
            "extra_trees_300_leaf_2",
            {"n_estimators": 300, "min_samples_leaf": 2, "max_features": 1.0},
        ),
        Candidate(
            "extra_trees",
            "extra_trees_500_leaf_4",
            {"n_estimators": 500, "min_samples_leaf": 4, "max_features": 0.8},
        ),
        Candidate(
            "extra_trees",
            "extra_trees_400_leaf_8",
            {"n_estimators": 400, "min_samples_leaf": 8, "max_features": 1.0},
        ),
        Candidate(
            "extra_trees",
            "extra_trees_300_leaf_2_depth_18",
            {"n_estimators": 300, "min_samples_leaf": 2, "max_depth": 18, "max_features": 1.0},
        ),
        Candidate(
            "hist_gradient_boosting",
            "hgb_300_lr_005",
            {
                "max_iter": 300,
                "learning_rate": 0.05,
                "max_leaf_nodes": 31,
                "l2_regularization": 1.0,
            },
        ),
        Candidate(
            "hist_gradient_boosting",
            "hgb_500_lr_003",
            {
                "max_iter": 500,
                "learning_rate": 0.03,
                "max_leaf_nodes": 31,
                "l2_regularization": 1.0,
            },
        ),
        Candidate(
            "hist_gradient_boosting",
            "hgb_350_leaf_15",
            {
                "max_iter": 350,
                "learning_rate": 0.05,
                "max_leaf_nodes": 15,
                "l2_regularization": 0.3,
            },
        ),
        Candidate(
            "hist_gradient_boosting",
            "hgb_400_leaf_63",
            {
                "max_iter": 400,
                "learning_rate": 0.04,
                "max_leaf_nodes": 63,
                "l2_regularization": 3.0,
            },
        ),
    ]
    if include_catboost:
        candidates += [
            Candidate(
                "catboost",
                "catboost_500_depth_6",
                {"iterations": 500, "depth": 6, "learning_rate": 0.05, "l2_leaf_reg": 3.0},
            ),
            Candidate(
                "catboost",
                "catboost_700_depth_8",
                {"iterations": 700, "depth": 8, "learning_rate": 0.035, "l2_leaf_reg": 5.0},
            ),
            Candidate(
                "catboost",
                "catboost_400_depth_5",
                {"iterations": 400, "depth": 5, "learning_rate": 0.06, "l2_leaf_reg": 1.0},
            ),
            Candidate(
                "catboost",
                "catboost_600_depth_7",
                {"iterations": 600, "depth": 7, "learning_rate": 0.04, "l2_leaf_reg": 8.0},
            ),
        ]
    return tuple(candidates)


class SeasonalMeanRegressor:
    """Calendar seasonal baseline pooled over turbines, with safe fallbacks."""

    def fit(self, X, y):
        import pandas as pd

        frame = X.copy()
        target = pd.Series(y, index=frame.index, name="_target")
        keys = [c for c in ("turbine_id", "target_hour_utc", "target_month_utc") if c in frame]
        if not keys:
            keys = [c for c in ("turbine_id", "lead_hours") if c in frame]
        self.keys_ = keys
        self.global_mean_ = float(target.mean())
        self.means_ = (
            frame.assign(_target=target).groupby(keys, dropna=False)["_target"].mean()
            if keys
            else None
        )
        return self

    def predict(self, X):
        import numpy as np

        if not self.keys_:
            return np.full(len(X), self.global_mean_)
        lookup = self.means_.rename("_prediction").reset_index()
        out = X[self.keys_].merge(lookup, how="left", on=self.keys_, sort=False)["_prediction"]
        return out.fillna(self.global_mean_).to_numpy()


class BinnedPowerCurveRegressor:
    """Weather-only binned curve baseline (never consumes observed test power)."""

    def __init__(self, n_bins: int = 24):
        self.n_bins = n_bins

    def fit(self, X, y):
        import numpy as np
        import pandas as pd

        self.wind_column_ = _find_wind_column(X.columns)
        values = pd.to_numeric(X[self.wind_column_], errors="coerce")
        self.global_mean_ = float(np.nanmean(y))
        lo, hi = float(values.min()), float(values.max())
        self.edges_ = np.linspace(lo, hi if hi > lo else lo + 1.0, self.n_bins + 1)
        bins = np.clip(np.digitize(values, self.edges_[1:-1], right=False), 0, self.n_bins - 1)
        frame = pd.DataFrame({"_bin": bins, "_target": y})
        self.means_ = (
            frame.groupby("_bin")["_target"]
            .mean()
            .reindex(range(self.n_bins))
            .fillna(self.global_mean_)
            .to_numpy()
        )
        self.has_turbine_ = "turbine_id" in X
        if self.has_turbine_:
            local = pd.DataFrame(
                {"turbine_id": X["turbine_id"].astype(str), "_bin": bins, "_target": y}
            )
            self.turbine_means_ = local.groupby(["turbine_id", "_bin"])["_target"].mean()
        return self

    def predict(self, X):
        import numpy as np
        import pandas as pd

        values = (
            pd.to_numeric(X[self.wind_column_], errors="coerce").fillna(self.edges_[0]).to_numpy()
        )
        bins = np.clip(np.digitize(values, self.edges_[1:-1], right=False), 0, self.n_bins - 1)
        prediction = self.means_[bins].astype(float)
        if self.has_turbine_ and "turbine_id" in X:
            index = pd.MultiIndex.from_arrays([X["turbine_id"].astype(str), bins])
            local = self.turbine_means_.reindex(index).to_numpy()
            found = ~pd.isna(local)
            prediction[found] = local[found]
        return prediction


def _find_wind_column(columns: Sequence[str]) -> str:
    # Hub-height wind is generally the most physical curve input when supplied.
    for name in ("wind_speed_100m", "wind_speed_10m", "gust_speed"):
        if name in columns:
            return name
    matches = [c for c in columns if "wind" in c.lower() and "speed" in c.lower()]
    if not matches:
        raise ValueError("A weather binned-power-curve baseline needs a wind-speed feature.")
    return matches[0]


def build_estimator(
    candidate: Candidate,
    numeric_features: Sequence[str],
    categorical_features: Sequence[str],
    *,
    random_state: int = 42,
    n_jobs: int = 4,
):
    """Build a fitted-artifact-friendly sklearn/CatBoost estimator for a candidate."""
    if isinstance(n_jobs, bool) or not isinstance(n_jobs, int) or n_jobs < 1:
        raise ValueError("n_jobs must be a positive integer.")
    family, params = candidate.family, dict(candidate.params)
    if family == "seasonal_mean":
        return SeasonalMeanRegressor()
    if family == "power_curve":
        return BinnedPowerCurveRegressor(**params)

    try:
        from sklearn.compose import ColumnTransformer
        from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import (
            OneHotEncoder,
            OrdinalEncoder,
            PolynomialFeatures,
            StandardScaler,
        )
    except ImportError as exc:  # keeps pure planning usable on minimal installations
        raise RuntimeError("scikit-learn is required to fit classical candidates.") from exc

    numeric = list(numeric_features)
    categorical = list(categorical_features)
    wind = [
        c
        for c in numeric
        if c in DEFAULT_WIND_COLUMNS or ("wind" in c.lower() and "speed" in c.lower())
    ]
    other_numeric = [c for c in numeric if c not in wind]
    onehot = OneHotEncoder(handle_unknown="ignore", sparse_output=False)

    if family == "ridge":
        transforms = []
        if wind:
            transforms.append(
                (
                    "wind_poly",
                    Pipeline(
                        [
                            ("impute", SimpleImputer(strategy="median")),
                            (
                                "poly",
                                PolynomialFeatures(degree=params.pop("degree"), include_bias=False),
                            ),
                            ("scale", StandardScaler()),
                        ]
                    ),
                    wind,
                )
            )
        if other_numeric:
            transforms.append(
                (
                    "numeric",
                    Pipeline(
                        [("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]
                    ),
                    other_numeric,
                )
            )
        if categorical:
            transforms.append(
                (
                    "categorical",
                    Pipeline(
                        [("impute", SimpleImputer(strategy="most_frequent")), ("onehot", onehot)]
                    ),
                    categorical,
                )
            )
        return Pipeline(
            [
                ("preprocess", ColumnTransformer(transforms, remainder="drop")),
                ("model", Ridge(**params)),
            ]
        )
    if family == "extra_trees":
        transforms = []
        if numeric:
            transforms.append(("numeric", SimpleImputer(strategy="median"), numeric))
        if categorical:
            transforms.append(
                (
                    "categorical",
                    Pipeline(
                        [("impute", SimpleImputer(strategy="most_frequent")), ("onehot", onehot)]
                    ),
                    categorical,
                )
            )
        return Pipeline(
            [
                ("preprocess", ColumnTransformer(transforms)),
                ("model", ExtraTreesRegressor(random_state=random_state, n_jobs=n_jobs, **params)),
            ]
        )
    if family == "hist_gradient_boosting":
        transforms = []
        if numeric:
            transforms.append(("numeric", SimpleImputer(strategy="median"), numeric))
        if categorical:
            transforms.append(
                (
                    "categorical",
                    Pipeline(
                        [
                            ("impute", SimpleImputer(strategy="most_frequent")),
                            (
                                "ordinal",
                                OrdinalEncoder(
                                    handle_unknown="use_encoded_value", unknown_value=-1
                                ),
                            ),
                        ]
                    ),
                    categorical,
                )
            )
        return Pipeline(
            [
                ("preprocess", ColumnTransformer(transforms)),
                (
                    "model",
                    HistGradientBoostingRegressor(
                        random_state=random_state, early_stopping=False, **params
                    ),
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
        # CatBoost accepts DataFrames and categorical column names directly.
        return CatBoostRegressor(
            loss_function="MAE",
            verbose=False,
            random_seed=random_state,
            thread_count=n_jobs,
            allow_writing_files=False,
            **params,
        )
    raise ValueError(f"Unknown candidate family: {family}")
