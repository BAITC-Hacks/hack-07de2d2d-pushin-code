from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import numpy as np
import pandas as pd

from wind_forecast.history_evaluation import _train_rows, run_evaluation


@dataclass(frozen=True)
class _Candidate:
    family: str
    name: str
    params: dict


class _Model:
    def __init__(self, value):
        self.value = value

    def fit(self, X, Y):
        return self

    def predict(self, X):
        return np.full((len(X), 48), self.value)


def _install_modules(monkeypatch):
    history = types.ModuleType("wind_forecast.history")
    history.FEATURE_COLUMNS = ("x", "turbine_id")
    history.NUMERIC_FEATURES = ("x",)
    history.CAT_FEATURES = ("turbine_id",)
    history.TARGET_COLUMNS = tuple(f"y_{index:02d}" for index in range(1, 49))
    history.HORIZON = 48
    models = types.ModuleType("wind_forecast.history_models")
    models.candidate_catalogue = lambda: (
        _Candidate("ridge", "tuning", {"value": 0.1}),
        _Candidate("catboost", "holdout", {"value": 0.9}),
    )
    models.build_estimator = lambda candidate, *_args, **_kwargs: _Model(candidate.params["value"])
    monkeypatch.setitem(sys.modules, "wind_forecast.history", history)
    monkeypatch.setitem(sys.modules, "wind_forecast.history_models", models)
    return history


def test_holdout_is_report_only_and_native_output_files(monkeypatch, tmp_path):
    history = _install_modules(monkeypatch)
    origins = pd.date_range("2025-08-01T19:00:00Z", "2026-01-30T19:00:00Z", freq="D")
    rows = []
    for origin in origins:
        # Candidate 'tuning' wins Oct/Nov (0.1); candidate 'holdout' wins Dec/Jan (0.9).
        value = 0.1 if origin < pd.Timestamp("2025-12-01T00:00:00Z") else 0.9
        for turbine in ("1", "2"):
            row = {"forecast_origin": origin, "turbine_id": turbine, "x": 1.0}
            row.update({target: value for target in history.TARGET_COLUMNS})
            rows.append(row)
    report = run_evaluation(
        pd.DataFrame(rows),
        tmp_path,
        candidates=(
            _Candidate("ridge", "tuning", {"value": 0.1}),
            _Candidate("catboost", "holdout", {"value": 0.9}),
        ),
        final_refit_cutoff="2026-01-31T19:00:00Z",
    )
    assert report["selected_by_tuning_only"]["name"] == "tuning"
    holdout = {
        item["name"]: item["metrics"]["overall"]["mae"]
        for item in report["frozen_holdout_family_winners"]
    }
    assert holdout["holdout"] < holdout["tuning"]
    assert (tmp_path / "best_model.pkl").exists()
    assert (tmp_path / "best_ml_model.pkl").exists()
    assert (tmp_path / "candidate_tuning.csv").exists()
    assert len(pd.read_parquet(tmp_path / "holdout_predictions.parquet")) > 0


def test_training_boundary_requires_end_of_48th_label():
    cutoff = pd.Timestamp("2025-10-01T00:00:00Z")
    frame = pd.DataFrame(
        {
            "forecast_origin": [
                cutoff - pd.Timedelta(48, unit="h"),
                cutoff - pd.Timedelta(47, unit="h"),
            ]
        }
    )
    accepted = _train_rows(frame, cutoff)
    assert accepted.forecast_origin.tolist() == [cutoff - pd.Timedelta(48, unit="h")]
