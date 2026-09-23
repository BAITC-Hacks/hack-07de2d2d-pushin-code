"""Chronological evaluation for native 48-output forecasts from CSV history."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .history_artifact import HistoryForecastArtifact

TUNING_FOLDS = (
    ("oct_2025", "2025-10-01T00:00:00Z", "2025-10-01T00:00:00Z", "2025-11-01T00:00:00Z"),
    ("nov_2025", "2025-11-01T00:00:00Z", "2025-11-01T00:00:00Z", "2025-12-01T00:00:00Z"),
)
FROZEN_CUTOFF = "2025-12-01T00:00:00Z"
HOLDOUT_START = "2025-12-01T00:00:00Z"
HOLDOUT_END = "2026-02-01T00:00:00Z"
DEFAULT_FINAL_REFIT_CUTOFF = "2026-01-31T19:00:00Z"
LEARNED_FAMILIES = frozenset({"ridge", "extra_trees", "random_forest", "catboost"})


def _utc(value: Any):
    import pandas as pd

    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError(f"Timestamp must be UTC/tz-aware: {value!r}")
    return stamp.tz_convert("UTC")


def _dependencies():
    try:
        from .history import (
            CAT_FEATURES,
            FEATURE_COLUMNS,
            HORIZON,
            NUMERIC_FEATURES,
            TARGET_COLUMNS,
        )
        from .history_models import build_estimator, candidate_catalogue
    except ImportError as exc:
        raise RuntimeError(
            "history and history_models modules are required for history evaluation."
        ) from exc
    return (
        FEATURE_COLUMNS,
        NUMERIC_FEATURES,
        CAT_FEATURES,
        TARGET_COLUMNS,
        HORIZON,
        build_estimator,
        candidate_catalogue,
    )


def _prepare(dataset, feature_columns: Sequence[str], target_columns: Sequence[str], horizon: int):
    import pandas as pd

    required = {"forecast_origin", "turbine_id", *feature_columns, *target_columns}
    missing = sorted(required.difference(dataset.columns))
    if missing:
        raise ValueError(f"History dataset is missing required columns: {missing}")
    if len(target_columns) != horizon:
        raise ValueError("TARGET_COLUMNS must have exactly HORIZON entries.")
    frame = dataset.copy()
    dtype = frame["forecast_origin"].dtype
    if not isinstance(dtype, pd.DatetimeTZDtype) or str(dtype.tz) != "UTC":
        raise ValueError(
            "forecast_origin must be timezone-aware UTC; naive timestamps are not allowed."
        )
    if frame["forecast_origin"].isna().any():
        raise ValueError("forecast_origin cannot be null.")
    if not frame["forecast_origin"].eq(frame["forecast_origin"].dt.floor("h")).all():
        raise ValueError("forecast_origin must be aligned to an exact UTC hour.")
    if frame.duplicated(["forecast_origin", "turbine_id"]).any():
        raise ValueError("History dataset has duplicate origin/turbine rows.")
    targets = frame.loc[:, target_columns].apply(pd.to_numeric, errors="raise")
    if targets.isna().any().any() or not targets.apply(lambda item: item.between(0, 1).all()).all():
        raise ValueError("Every native 48-output target must be present and normalized to [0, 1].")
    frame.loc[:, target_columns] = targets
    return frame


def _train_rows(frame, cutoff):
    # y_48 is target origin + 47h; its complete hourly label ends at origin +48h.
    return frame.loc[
        frame["forecast_origin"] + __import__("pandas").Timedelta(48, unit="h") <= cutoff
    ].copy()


def _validation_rows(frame, start, end):
    return frame.loc[
        (frame["forecast_origin"] >= start)
        & (frame["forecast_origin"] + __import__("pandas").Timedelta(48, unit="h") <= end)
        & (frame["forecast_origin"].dt.hour == 19)
    ].copy()


def _fit(
    build_estimator,
    candidate,
    train,
    feature_columns,
    target_columns,
    numeric_features,
    categorical_features,
    random_state,
    n_jobs,
):
    model = build_estimator(
        candidate,
        numeric_features,
        categorical_features,
        random_state=random_state,
        n_jobs=n_jobs,
    )
    model.fit(train.loc[:, feature_columns], train.loc[:, target_columns])
    return model


def _metrics(rows, prediction, target_columns):
    import numpy as np

    truth = rows.loc[:, target_columns].to_numpy(dtype=float)
    errors = prediction - truth

    def score(error):
        return {
            "rows": int(error.shape[0]),
            "cells": int(error.size),
            "mae": float(np.mean(np.abs(error))),
            "rmse": float(np.sqrt(np.mean(error**2))),
            "bias": float(np.mean(error)),
        }

    by_turbine = {}
    for turbine, indices in rows.groupby("turbine_id", sort=True).groups.items():
        mask = rows.index.isin(indices)
        by_turbine[str(turbine)] = score(errors[mask])
    return {
        "overall": score(errors),
        "by_turbine": by_turbine,
        "by_horizon": {"lead_01_24": score(errors[:, :24]), "lead_25_48": score(errors[:, 24:])},
    }


def _prediction_rows(rows, prediction, candidate_name: str | None = None):
    import numpy as np
    import pandas as pd

    output = []
    clipped = np.clip(prediction, 0.0, 1.0)
    for position, (_, row) in enumerate(rows.iterrows()):
        origin = row["forecast_origin"]
        for step in range(48):
            result = {
                "turbine_id": str(row["turbine_id"]),
                "forecast_origin": origin,
                "target_time": origin + pd.Timedelta(step, unit="h"),
                "lead_hours": step + 1,
                "power": float(row[f"y_{step + 1:02d}"]),
                "power_prediction": float(clipped[position, step]),
            }
            if candidate_name is not None:
                result["candidate"] = candidate_name
            output.append(result)
    return pd.DataFrame(output)


def _family_winners(rows: Sequence[Mapping[str, Any]]):
    winners = {}
    for row in rows:
        family = row["family"]
        if (
            family not in winners
            or row["mean_validation_mae"] < winners[family]["mean_validation_mae"]
        ):
            winners[family] = row
    return winners


def _write_leaderboard(report: Mapping[str, Any], output: Path) -> None:
    """Write a sorted comparison without using holdout to set the selection flag."""
    import pandas as pd

    selected = report.get("selected_by_tuning_only", {})
    holdout = {
        (item["family"], item["name"]): item["metrics"]["overall"]
        for item in report.get("frozen_holdout_family_winners", [])
    }
    rows = []
    for item in report.get("tuning", []):
        metrics = holdout.get((item["family"], item["name"]), {})
        rows.append(
            {
                "family": item["family"],
                "name": item["name"],
                "mean_validation_mae": item["mean_validation_mae"],
                "family_winner_holdout_mae": metrics.get("mae"),
                "family_winner_holdout_rmse": metrics.get("rmse"),
                "family_winner_holdout_bias": metrics.get("bias"),
                "selected_by_validation_only": (
                    item["family"] == selected.get("family")
                    and item["name"] == selected.get("name")
                ),
            }
        )
    pd.DataFrame(rows).sort_values(["mean_validation_mae", "name"]).to_csv(
        output / "leaderboard.csv", index=False
    )


def refit_best_learned_model(
    dataset,
    report: dict[str, Any],
    output_dir: str | Path,
    random_state: int = 42,
    n_jobs: int = 4,
    final_refit_cutoff: str = DEFAULT_FINAL_REFIT_CUTOFF,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Refit the best non-baseline candidate using tuning MAE only.

    ``report`` is intentionally read only from its tuning results.  Frozen
    holdout metrics are not consulted, so this supplementary artifact cannot
    change the primary winner selected by :func:`run_evaluation`.
    """
    (
        feature_columns,
        numeric_features,
        categorical_features,
        target_columns,
        horizon,
        build_estimator,
        candidate_catalogue,
    ) = _dependencies()
    frame = _prepare(dataset, feature_columns, target_columns, horizon)
    eligible = [item for item in report.get("tuning", []) if item.get("family") in LEARNED_FAMILIES]
    if not eligible:
        raise ValueError("Report contains no learned candidate tuning results.")
    selected = min(eligible, key=lambda item: (item["mean_validation_mae"], item["name"]))
    lookup = {(candidate.family, candidate.name): candidate for candidate in candidate_catalogue()}
    try:
        candidate = lookup[(selected["family"], selected["name"])]
    except KeyError as exc:
        raise ValueError(
            "Selected learned tuning candidate is absent from the installed catalogue."
        ) from exc
    cutoff = _utc(final_refit_cutoff)
    train = _train_rows(frame, cutoff)
    if train.empty:
        raise ValueError("Supplementary learned refit has no fully labeled rows.")
    print(f"History supplementary ML refit: {candidate.name}", file=sys.stderr, flush=True)
    started = time.perf_counter()
    model = _fit(
        build_estimator,
        candidate,
        train,
        feature_columns,
        target_columns,
        numeric_features,
        categorical_features,
        random_state,
        n_jobs,
    )
    artifact = HistoryForecastArtifact(
        model=model,
        feature_schema=feature_columns,
        trained_until=cutoff,
        model_config={
            "family": candidate.family,
            "name": candidate.name,
            "params": dict(candidate.params),
            "random_state": random_state,
            "n_jobs": n_jobs,
        },
        provenance={
            **dict(provenance or {}),
            "trained_turbines": sorted(frame["turbine_id"].astype(str).unique()),
            "target_columns": list(target_columns),
        },
        metrics={"tuning_selection": selected},
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    artifact_path, manifest_path = artifact.save(output / "best_ml_model.pkl")
    details = {
        "name": candidate.name,
        "family": candidate.family,
        "mean_validation_mae": selected["mean_validation_mae"],
        "fit_rows": int(len(train)),
        "trained_until": cutoff.isoformat(),
        "path": str(artifact_path),
        "manifest": str(manifest_path),
        "selection_rule": (
            "minimum mean validation MAE among ridge/extra_trees/random_forest/catboost; "
            "frozen holdout excluded"
        ),
    }
    report["supplementary_best_learned"] = details
    _write_leaderboard(report, output)
    print(
        f"History supplementary ML refit completed: {time.perf_counter() - started:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    return details


def run_evaluation(
    dataset,
    output_dir: str | Path,
    random_state: int = 42,
    n_jobs: int = 4,
    candidates=None,
    final_refit_cutoff: str = DEFAULT_FINAL_REFIT_CUTOFF,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit exactly 14 native-output candidates chronologically; no import side effects."""
    import numpy as np
    import pandas as pd

    (
        feature_columns,
        numeric_features,
        categorical_features,
        target_columns,
        horizon,
        build_estimator,
        candidate_catalogue,
    ) = _dependencies()
    if horizon != 48:
        raise ValueError("CSV history evaluation is defined only for a 48-output horizon.")
    frame = _prepare(dataset, feature_columns, target_columns, horizon)
    catalogue = tuple(candidates) if candidates is not None else tuple(candidate_catalogue())
    if not catalogue:
        raise ValueError("At least one candidate is required.")
    tuning = []
    for candidate_index, candidate in enumerate(catalogue, start=1):
        print(
            f"History tuning candidate {candidate_index}/{len(catalogue)}: {candidate.name}",
            file=sys.stderr,
            flush=True,
        )
        folds = []
        for name, cutoff_value, start_value, end_value in TUNING_FOLDS:
            started = time.perf_counter()
            cutoff, start, end = _utc(cutoff_value), _utc(start_value), _utc(end_value)
            train, valid = _train_rows(frame, cutoff), _validation_rows(frame, start, end)
            if train.empty or valid.empty:
                raise ValueError(f"{name} has empty train or daily-19Z validation rows.")
            if set(
                train[["forecast_origin", "turbine_id"]].itertuples(index=False, name=None)
            ).intersection(
                set(valid[["forecast_origin", "turbine_id"]].itertuples(index=False, name=None))
            ):
                raise ValueError(f"{name} has overlapping train/validation origin-turbine rows.")
            model = _fit(
                build_estimator,
                candidate,
                train,
                feature_columns,
                target_columns,
                numeric_features,
                categorical_features,
                random_state,
                n_jobs,
            )
            raw = np.asarray(model.predict(valid.loc[:, feature_columns]), dtype=float)
            if raw.shape != (len(valid), horizon):
                raise ValueError(
                    f"{candidate.name} returned {raw.shape}; native (n, 48) output required."
                )
            if not np.isfinite(raw).all():
                raise ValueError(f"{candidate.name} returned non-finite validation predictions.")
            prediction = np.clip(raw, 0.0, 1.0)
            folds.append(
                {
                    "fold": name,
                    "metrics": _metrics(valid, prediction, target_columns),
                    "raw_out_of_range_count": int(((raw < 0) | (raw > 1)).sum()),
                }
            )
            print(
                f"History tuning {candidate.name} {name}: {time.perf_counter() - started:.1f}s",
                file=sys.stderr,
                flush=True,
            )
        tuning.append(
            {
                "family": candidate.family,
                "name": candidate.name,
                "params": dict(candidate.params),
                "folds": folds,
                "mean_validation_mae": float(
                    np.mean([fold["metrics"]["overall"]["mae"] for fold in folds])
                ),
            }
        )
    winners = _family_winners(tuning)
    selected = min(winners.values(), key=lambda item: (item["mean_validation_mae"], item["name"]))
    lookup = {(candidate.family, candidate.name): candidate for candidate in catalogue}
    frozen_train = _train_rows(frame, _utc(FROZEN_CUTOFF))
    holdout = _validation_rows(frame, _utc(HOLDOUT_START), _utc(HOLDOUT_END))
    if frozen_train.empty or holdout.empty:
        raise ValueError("Frozen holdout has empty train or daily-19Z rows.")
    holdout_reports, holdout_predictions = [], []
    for winner in winners.values():
        candidate = lookup[(winner["family"], winner["name"])]
        print(
            f"History frozen holdout family winner: {candidate.name}", file=sys.stderr, flush=True
        )
        started = time.perf_counter()
        model = _fit(
            build_estimator,
            candidate,
            frozen_train,
            feature_columns,
            target_columns,
            numeric_features,
            categorical_features,
            random_state,
            n_jobs,
        )
        raw = np.asarray(model.predict(holdout.loc[:, feature_columns]), dtype=float)
        if raw.shape != (len(holdout), horizon) or not np.isfinite(raw).all():
            raise ValueError(
                f"{candidate.name} returned invalid frozen-holdout native predictions."
            )
        prediction = np.clip(raw, 0.0, 1.0)
        holdout_reports.append(
            {
                "family": candidate.family,
                "name": candidate.name,
                "params": dict(candidate.params),
                "metrics": _metrics(holdout, prediction, target_columns),
                "raw_out_of_range_count": int(((raw < 0) | (raw > 1)).sum()),
            }
        )
        holdout_predictions.append(_prediction_rows(holdout, prediction, candidate.name))
        print(
            f"History frozen holdout {candidate.name}: {time.perf_counter() - started:.1f}s",
            file=sys.stderr,
            flush=True,
        )
    final_cutoff = _utc(final_refit_cutoff)
    final_train = _train_rows(frame, final_cutoff)
    if final_train.empty:
        raise ValueError("Final refit has no rows with all 48 labels available by its cutoff.")
    chosen = lookup[(selected["family"], selected["name"])]
    print(f"History final refit: {chosen.name}", file=sys.stderr, flush=True)
    started = time.perf_counter()
    final_model = _fit(
        build_estimator,
        chosen,
        final_train,
        feature_columns,
        target_columns,
        numeric_features,
        categorical_features,
        random_state,
        n_jobs,
    )
    artifact = HistoryForecastArtifact(
        model=final_model,
        feature_schema=feature_columns,
        trained_until=final_cutoff,
        model_config={
            "family": chosen.family,
            "name": chosen.name,
            "params": dict(chosen.params),
            "random_state": random_state,
            "n_jobs": n_jobs,
        },
        provenance={
            **dict(provenance or {}),
            "trained_turbines": sorted(frame["turbine_id"].astype(str).unique()),
            "target_columns": list(target_columns),
        },
        metrics={"selection": selected, "frozen_holdout": holdout_reports},
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    artifact_path, manifest_path = artifact.save(output / "best_model.pkl")
    tuning_csv = []
    for candidate in tuning:
        for fold in candidate["folds"]:
            tuning_csv.append(
                {
                    "family": candidate["family"],
                    "name": candidate["name"],
                    "mean_validation_mae": candidate["mean_validation_mae"],
                    "fold": fold["fold"],
                    **fold["metrics"]["overall"],
                    "raw_out_of_range_count": fold["raw_out_of_range_count"],
                }
            )
    pd.DataFrame(tuning_csv).to_csv(output / "candidate_tuning.csv", index=False)
    pd.concat(holdout_predictions, ignore_index=True).to_parquet(
        output / "holdout_predictions.parquet", index=False
    )
    report = {
        "tuning": tuning,
        "selected_by_tuning_only": selected,
        "frozen_holdout_family_winners": holdout_reports,
        "final_refit": {"cutoff": final_cutoff.isoformat(), "rows": int(len(final_train))},
        "fold_configuration": {
            "tuning_folds": [
                {
                    "name": name,
                    "train_label_cutoff": cutoff,
                    "validation_start": start,
                    "validation_end": end,
                }
                for name, cutoff, start, end in TUNING_FOLDS
            ],
            "frozen_train_cutoff": FROZEN_CUTOFF,
            "holdout_start": HOLDOUT_START,
            "holdout_end": HOLDOUT_END,
        },
        "provenance": dict(provenance or {}),
        "artifacts": {"pickle": str(artifact_path), "manifest": str(manifest_path)},
    }
    refit_best_learned_model(
        dataset,
        report,
        output,
        random_state=random_state,
        n_jobs=n_jobs,
        final_refit_cutoff=final_refit_cutoff,
        provenance=provenance,
    )
    (output / "evaluation_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(
        f"History final refit completed: {time.perf_counter() - started:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    return report
