"""Chronological, point-in-time-safe model selection for weather-to-power forecasts.

`run_evaluation` performs fitting only when called.  Importing this module and
calling `evaluation_plan` are side-effect free, which keeps CLI approval and
execution separate.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, Sequence

from .artifact import IDENTITY_COLUMNS, ForecastArtifact
from .models import Candidate, build_estimator, candidate_catalogue

REQUIRED_COLUMNS = (
    "turbine_id",
    "forecast_origin",
    "target_time",
    "weather_run_time",
    "weather_available_at",
    "lead_hours",
    "power",
)


@dataclass(frozen=True)
class RollingFold:
    """Training labels end at ``train_label_cutoff``; validation is target time."""

    name: str
    train_label_cutoff: str
    validation_start: str
    validation_end: str


@dataclass(frozen=True)
class EvaluationConfig:
    """Explicit dates make the intended 2025/26 experiment reproducible."""

    feature_columns: tuple[str, ...]
    numeric_features: tuple[str, ...]
    categorical_features: tuple[str, ...]
    target_column: str = "power"
    random_state: int = 42
    n_jobs: int = 4
    include_catboost: bool = True
    candidates: tuple[Candidate, ...] | None = None
    tune_folds: tuple[RollingFold, ...] = (
        RollingFold(
            "oct_2025", "2025-10-01T00:00:00Z", "2025-10-01T00:00:00Z", "2025-11-01T00:00:00Z"
        ),
        RollingFold(
            "nov_2025", "2025-11-01T00:00:00Z", "2025-11-01T00:00:00Z", "2025-12-01T00:00:00Z"
        ),
    )
    frozen_train_label_cutoff: str = "2025-12-01T00:00:00Z"
    holdout_start: str = "2025-12-01T00:00:00Z"
    holdout_end: str = "2026-02-01T00:00:00Z"
    operational_origin: str = "2026-01-31T18:00:00Z"
    # The first delivery is Feb 1 00:00 in fixed UTC+05, i.e. 19:00 UTC.
    operational_refit_label_cutoff: str = "2026-01-31T18:00:00Z"

    def resolved_candidates(self) -> tuple[Candidate, ...]:
        return (
            self.candidates
            if self.candidates is not None
            else candidate_catalogue(self.include_catboost)
        )


def default_config_from_features(
    feature_columns: Sequence[str],
    numeric_features: Sequence[str],
    categorical_features: Sequence[str],
) -> EvaluationConfig:
    """Convenience bridge for ``wind_forecast.features`` exports."""
    return EvaluationConfig(
        tuple(feature_columns), tuple(numeric_features), tuple(categorical_features)
    )


def default_config() -> EvaluationConfig:
    """Lazily load the data-worker feature contract for the training CLI."""
    try:
        from .features import CAT_FEATURES, FEATURE_COLUMNS, NUMERIC_FEATURES
    except ImportError as exc:
        raise RuntimeError(
            "wind_forecast.features is required for default_config(); "
            "pass EvaluationConfig explicitly otherwise."
        ) from exc
    return default_config_from_features(FEATURE_COLUMNS, NUMERIC_FEATURES, CAT_FEATURES)


def config_from_dict(values: Mapping[str, Any]) -> EvaluationConfig:
    """Create a typed config from CLI JSON, retaining safe defaults for omitted keys."""
    base = default_config()
    allowed = set(EvaluationConfig.__dataclass_fields__)
    unknown = set(values).difference(allowed)
    if unknown:
        raise ValueError(f"Unknown evaluation config fields: {sorted(unknown)}")
    parsed = dict(values)
    if "candidates" in parsed and parsed["candidates"] is not None:
        parsed["candidates"] = tuple(
            item
            if isinstance(item, Candidate)
            else Candidate(item["family"], item["name"], item.get("params", {}))
            for item in parsed["candidates"]
        )
    if "tune_folds" in parsed:
        parsed["tune_folds"] = tuple(
            item if isinstance(item, RollingFold) else RollingFold(**item)
            for item in parsed["tune_folds"]
        )
    for name in ("feature_columns", "numeric_features", "categorical_features"):
        if name in parsed:
            parsed[name] = tuple(parsed[name])
    defaults = {name: getattr(base, name) for name in allowed}
    defaults.update(parsed)
    config = EvaluationConfig(**defaults)
    _validate_config(config)
    return config


def evaluation_plan(config: EvaluationConfig) -> dict[str, Any]:
    """Return the full no-fit experiment plan, safe for CLI ``--plan`` output."""
    _validate_config(config)
    return {
        "selection_metric": "MAE (validation folds only)",
        "prediction": "48 direct hourly weather-to-power outputs; clipped to [0, 1]",
        "pooling": "all turbine rows are pooled; turbine_id is a categorical feature",
        "tune_folds": [asdict(fold) for fold in config.tune_folds],
        "frozen_holdout": {
            "train_label_cutoff": config.frozen_train_label_cutoff,
            "target_interval": [config.holdout_start, config.holdout_end],
            "selection_use": "report-only; never used to select a winner",
        },
        "operational_refit": {
            "forecast_origin": config.operational_origin,
            "label_cutoff": config.operational_refit_label_cutoff,
            "label_rule": "target_time + 1 hour <= label_cutoff",
        },
        "candidates": [
            {"family": c.family, "name": c.name, "params": dict(c.params)}
            for c in config.resolved_candidates()
        ],
    }


def _utc(value: Any):
    import pandas as pd

    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError(f"Timestamp must be explicit UTC/tz-aware, got {value!r}")
    return ts.tz_convert("UTC")


def _aware_timestamp_series(values: Any, name: str):
    """Reject naive raw timestamps before pandas' ``utc=True`` can assume UTC."""
    import pandas as pd

    for index, value in values.items():
        if pd.isna(value):
            continue
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} contains an invalid timestamp at row {index!r}.") from exc
        if timestamp.tzinfo is None:
            raise ValueError(
                f"{name} must contain timezone-aware timestamps; row {index!r} is naive."
            )
    parsed = pd.to_datetime(values, utc=True, errors="raise")
    if parsed.isna().any():
        raise ValueError(f"{name} cannot contain null timestamps.")
    return parsed


def _validate_config(config: EvaluationConfig) -> None:
    """Validate split chronology before any feature construction or fitting."""
    if not config.feature_columns or len(set(config.feature_columns)) != len(
        config.feature_columns
    ):
        raise ValueError("feature_columns must be non-empty and contain no duplicates.")
    numeric, categorical = tuple(config.numeric_features), tuple(config.categorical_features)
    if len(set(numeric)) != len(numeric) or len(set(categorical)) != len(categorical):
        raise ValueError("numeric_features and categorical_features must not contain duplicates.")
    if set(numeric).intersection(categorical) or set(numeric).union(categorical) != set(
        config.feature_columns
    ):
        raise ValueError(
            "numeric/categorical features must be disjoint and exactly cover feature_columns."
        )
    if isinstance(config.n_jobs, bool) or not isinstance(config.n_jobs, int) or config.n_jobs < 1:
        raise ValueError("n_jobs must be a positive integer.")
    candidates = config.resolved_candidates()
    if not candidates:
        raise ValueError("at least one candidate is required.")
    keys = [(candidate.family, candidate.name) for candidate in candidates]
    if len(set(keys)) != len(keys):
        raise ValueError("candidate family/name pairs must be unique.")

    def hour(value: str, name: str):
        timestamp = _utc(value)
        if timestamp != timestamp.floor("h"):
            raise ValueError(f"{name} must be aligned to a whole UTC hour.")
        return timestamp

    previous_end = previous_cutoff = None
    for fold in config.tune_folds:
        cutoff = hour(fold.train_label_cutoff, f"{fold.name}.train_label_cutoff")
        start = hour(fold.validation_start, f"{fold.name}.validation_start")
        end = hour(fold.validation_end, f"{fold.name}.validation_end")
        if cutoff > start or start >= end:
            raise ValueError(
                f"{fold.name} must satisfy train_label_cutoff <= validation_start < validation_end."
            )
        if previous_end is not None and start < previous_end:
            raise ValueError(
                "tune_folds must be chronological and have non-overlapping validation intervals."
            )
        if previous_cutoff is not None and cutoff < previous_cutoff:
            raise ValueError("tune_folds must have non-decreasing train_label_cutoff values.")
        previous_end, previous_cutoff = end, cutoff

    frozen_cutoff = hour(config.frozen_train_label_cutoff, "frozen_train_label_cutoff")
    holdout_start = hour(config.holdout_start, "holdout_start")
    holdout_end = hour(config.holdout_end, "holdout_end")
    if frozen_cutoff > holdout_start or holdout_start >= holdout_end:
        raise ValueError(
            "frozen holdout must satisfy train_label_cutoff <= holdout_start < holdout_end."
        )
    if previous_end is not None and previous_end > holdout_start:
        raise ValueError("frozen holdout may not overlap a tuning validation interval.")
    operational_origin = hour(config.operational_origin, "operational_origin")
    refit_cutoff = hour(config.operational_refit_label_cutoff, "operational_refit_label_cutoff")
    if refit_cutoff > operational_origin:
        raise ValueError("operational_refit_label_cutoff cannot be after operational_origin.")


def _prepare_dataset(dataset, config: EvaluationConfig):
    """Validate direct forecast weather rows and their point-in-time lineage."""
    import pandas as pd

    missing = [
        c for c in (*REQUIRED_COLUMNS[:-1], config.target_column) if c not in dataset.columns
    ]
    if missing:
        raise ValueError(f"Dataset is missing required columns: {missing}")
    df = dataset.copy()
    for column in ("forecast_origin", "target_time", "weather_run_time", "weather_available_at"):
        df[column] = _aware_timestamp_series(df[column], column)
    if not (
        (df.weather_run_time <= df.forecast_origin)
        & (df.weather_available_at <= df.forecast_origin)
    ).all():
        raise ValueError("Weather run/availability must be no later than each forecast_origin.")
    if not (df.weather_run_time <= df.weather_available_at).all():
        raise ValueError("weather_run_time must be no later than weather_available_at.")
    lead = pd.to_numeric(df.lead_hours, errors="raise")
    if not lead.between(1, 48).all():
        raise ValueError("lead_hours must be within 1..48.")
    implied = (df.target_time - df.forecast_origin).dt.total_seconds() / 3600
    if not (implied.round(8) == lead.round(8)).all():
        raise ValueError(
            "target_time - forecast_origin must equal lead_hours for direct forecasts."
        )
    if df.duplicated(["turbine_id", "forecast_origin", "lead_hours"]).any():
        raise ValueError(
            "Duplicate turbine_id/forecast_origin/lead_hours forecast cells are not allowed."
        )
    target = pd.to_numeric(df[config.target_column], errors="raise")
    if target.isna().any() or not target.between(0, 1).all():
        raise ValueError("power must be normalized and bounded in [0, 1].")
    df[config.target_column] = target
    if "weather_model" in df.columns:
        models = df["weather_model"]
        if models.isna().any() or models.astype(str).str.strip().eq("").any():
            raise ValueError("weather_model must be non-null and non-empty when supplied.")
    df["_label_end"] = df.target_time + pd.Timedelta(1, unit="h")
    try:
        from .features import CAT_FEATURES, FEATURE_COLUMNS, NUMERIC_FEATURES, build_features
    except ImportError as exc:
        raise RuntimeError(
            "wind_forecast.features is required to build evaluation features."
        ) from exc
    if tuple(FEATURE_COLUMNS) != tuple(config.feature_columns):
        raise ValueError(
            "EvaluationConfig.feature_columns must exactly match the installed "
            "FEATURE_COLUMNS contract."
        )
    if tuple(NUMERIC_FEATURES) != tuple(config.numeric_features) or tuple(CAT_FEATURES) != tuple(
        config.categorical_features
    ):
        raise ValueError(
            "EvaluationConfig numeric/categorical features must exactly match "
            "the installed feature contract."
        )
    engineered = build_features(df)
    for column in config.feature_columns:
        df[column] = engineered[column]
    return df


def _train_rows(df, cutoff):
    # Labels are only eligible if the complete hourly measurement was available.
    return df.loc[df._label_end <= cutoff].copy()


def _target_interval(df, start, end, minimum_origin):
    """Score only future forecasts: no origin may precede its training cutoff."""
    return df.loc[
        (df.target_time >= start) & (df.target_time < end) & (df.forecast_origin >= minimum_origin)
    ].copy()


def _assert_group_disjoint(left, right, left_name: str, right_name: str):
    import pandas as pd

    a = pd.MultiIndex.from_frame(left[["turbine_id", "target_time"]].drop_duplicates())
    b = pd.MultiIndex.from_frame(right[["turbine_id", "target_time"]].drop_duplicates())
    overlap = a.intersection(b)
    if len(overlap):
        raise ValueError(
            f"{left_name} and {right_name} share {len(overlap)} turbine/target-time "
            "groups; split leakage."
        )


def _fit(candidate: Candidate, train, config: EvaluationConfig):
    estimator = build_estimator(
        candidate,
        config.numeric_features,
        config.categorical_features,
        random_state=config.random_state,
        n_jobs=config.n_jobs,
    )
    X, y = train.loc[:, config.feature_columns], train[config.target_column]
    if candidate.family == "catboost":
        # CatBoost needs actual categorical values, not NaNs; preserve feature names.
        X = X.copy()
        for col in config.categorical_features:
            X[col] = X[col].fillna("__MISSING__").astype(str)
        estimator.fit(X, y, cat_features=list(config.categorical_features))
    else:
        if candidate.family == "hist_gradient_boosting":
            try:
                from threadpoolctl import threadpool_limits
            except ImportError as exc:
                raise RuntimeError(
                    "threadpoolctl is required to enforce n_jobs for HistGradientBoosting."
                ) from exc
            with threadpool_limits(limits=config.n_jobs):
                estimator.fit(X, y)
        else:
            estimator.fit(X, y)
    return estimator


def _predict(estimator, candidate: Candidate, rows, config: EvaluationConfig):
    import numpy as np

    X = rows.loc[:, config.feature_columns].copy()
    if candidate.family == "catboost":
        for col in config.categorical_features:
            X[col] = X[col].fillna("__MISSING__").astype(str)
    raw = np.asarray(estimator.predict(X), dtype=float)
    if raw.ndim != 1 or raw.shape[0] != len(rows):
        raise ValueError("Estimator returned a prediction count different from the requested rows.")
    if not np.isfinite(raw).all():
        raise ValueError("Estimator returned non-finite predictions.")
    return raw, np.clip(raw, 0.0, 1.0)


def _metric_block(
    rows, raw_prediction, prediction, target_column: str = "power", expected_leads: int = 48
):
    import numpy as np

    y = rows[target_column].to_numpy(dtype=float)
    # Each turbine/day forecast has total weight one, with its observed lead
    # cells equally weighted. This prevents a more-complete forecast day from
    # dominating a day with a partially missing horizon; coverage is reported.
    key_counts = (
        rows.groupby(["forecast_origin", "turbine_id"], dropna=False)[target_column]
        .transform("size")
        .to_numpy(dtype=float)
    )
    weights = 1.0 / key_counts
    errors = prediction - y
    mae = float(np.average(np.abs(errors), weights=weights))
    rmse = float(np.sqrt(np.average(errors**2, weights=weights)))
    bias = float(np.average(errors, weights=weights))
    report = {
        "rows": int(len(rows)),
        "unique_turbine_target_groups": int(
            rows[["turbine_id", "target_time"]].drop_duplicates().shape[0]
        ),
        "expected_forecast_cells": int(
            rows[["forecast_origin", "turbine_id"]].drop_duplicates().shape[0] * expected_leads
        ),
        "observed_forecast_cells": int(
            rows[["forecast_origin", "turbine_id", "lead_hours"]].drop_duplicates().shape[0]
        ),
        "mae": mae,
        "rmse": rmse,
        "bias": bias,
        "raw_prediction_out_of_range_count": int(
            ((raw_prediction < 0) | (raw_prediction > 1)).sum()
        ),
        "raw_prediction_out_of_range_rate": float(
            ((raw_prediction < 0) | (raw_prediction > 1)).mean()
        ),
    }
    return report


def _metrics(rows, raw_prediction, prediction, target_column: str = "power"):
    result = {"overall": _metric_block(rows, raw_prediction, prediction, target_column)}
    by_turbine = {}
    for turbine, idx in rows.groupby("turbine_id", sort=True).groups.items():
        indices = list(idx)
        # DataFrame index need not be contiguous; align by positional mask.
        mask = rows.index.isin(indices)
        by_turbine[str(turbine)] = _metric_block(
            rows.loc[mask], raw_prediction[mask], prediction[mask], target_column
        )
    result["by_turbine"] = by_turbine
    horizons = {}
    for name, low, high in (("lead_01_24", 1, 24), ("lead_25_48", 25, 48)):
        mask = rows.lead_hours.between(low, high).to_numpy()
        horizons[name] = (
            _metric_block(
                rows.loc[mask],
                raw_prediction[mask],
                prediction[mask],
                target_column,
                expected_leads=high - low + 1,
            )
            if mask.any()
            else {"rows": 0}
        )
    result["by_horizon"] = horizons
    return result


def _candidate_winners(tuning_rows: Sequence[Mapping[str, Any]]):
    winners = {}
    for row in tuning_rows:
        family = row["family"]
        if (
            family not in winners
            or row["mean_validation_mae"] < winners[family]["mean_validation_mae"]
        ):
            winners[family] = row
    return winners


def _progress(message: str) -> None:
    """Emit fit progress without buffering so long approved runs stay observable."""
    print(f"[evaluation] {message}", file=sys.stderr, flush=True)


def _tuning_table(tuning_rows: Sequence[Mapping[str, Any]]):
    """Flatten validation-only candidate/fold metrics for a human-auditable CSV."""
    import json

    import pandas as pd

    rows: list[dict[str, Any]] = []
    for candidate in tuning_rows:
        for fold in candidate["folds"]:
            overall = fold["overall"]
            rows.append(
                {
                    "model_family": candidate["family"],
                    "model_name": candidate["name"],
                    "params_json": json.dumps(candidate["params"], sort_keys=True),
                    "fold": fold["fold"],
                    "validation_rows": overall["rows"],
                    "validation_mae": overall["mae"],
                    "validation_rmse": overall["rmse"],
                    "validation_bias": overall["bias"],
                    "mean_validation_mae": candidate["mean_validation_mae"],
                    "fit_seconds": fold["fit_seconds"],
                    "predict_seconds": fold["predict_seconds"],
                }
            )
    return pd.DataFrame(rows)


def _leaderboard_table(
    tuning_rows: Sequence[Mapping[str, Any]],
    holdout_reports: Sequence[Mapping[str, Any]],
    selected: Mapping[str, Any],
):
    """Make holdout coverage explicit without using it for selection."""
    import pandas as pd

    holdout = {(row["family"], row["name"]): row["metrics"]["overall"] for row in holdout_reports}
    rows: list[dict[str, Any]] = []
    for candidate in tuning_rows:
        key = (candidate["family"], candidate["name"])
        metrics = holdout.get(key)
        rows.append(
            {
                "model_family": candidate["family"],
                "model_name": candidate["name"],
                "mean_validation_mae": candidate["mean_validation_mae"],
                "holdout_evaluated": metrics is not None,
                "holdout_mae": metrics["mae"] if metrics else None,
                "holdout_rmse": metrics["rmse"] if metrics else None,
                "holdout_bias": metrics["bias"] if metrics else None,
                "selected_by_validation_only": key == (selected["family"], selected["name"]),
            }
        )
    return pd.DataFrame(rows).sort_values(["mean_validation_mae", "model_name"], kind="stable")


def run_evaluation(
    dataset, config: EvaluationConfig, output_dir: str | Path | None = None
) -> dict[str, Any]:
    """Fit the predefined rolling comparison and frozen holdout.

    Call this only from an approved training command.

    Selection is based only on the mean MAE of October/November folds.  The
    December--January holdout is calculated *after* that decision and is report-only.
    If ``output_dir`` is supplied, a refit winner artifact and JSON report are saved.
    """
    import json

    import numpy as np

    _validate_config(config)
    run_started = perf_counter()
    df = _prepare_dataset(dataset, config)
    candidates = config.resolved_candidates()
    tuning_rows: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(candidates, start=1):
        _progress(
            f"candidate {candidate_index}/{len(candidates)} "
            f"{candidate.family}/{candidate.name}: starting"
        )
        candidate_started = perf_counter()
        fold_reports = []
        for fold in config.tune_folds:
            cutoff, start, end = (
                _utc(fold.train_label_cutoff),
                _utc(fold.validation_start),
                _utc(fold.validation_end),
            )
            train, valid = _train_rows(df, cutoff), _target_interval(df, start, end, cutoff)
            _assert_group_disjoint(train, valid, f"{fold.name} train", f"{fold.name} validation")
            if train.empty or valid.empty:
                raise ValueError(
                    f"{fold.name} has empty train or validation rows after point-in-time filtering."
                )
            _progress(
                f"candidate {candidate.family}/{candidate.name}, fold {fold.name}: "
                f"fitting {len(train):,} train / {len(valid):,} validation rows"
            )
            fit_started = perf_counter()
            estimator = _fit(candidate, train, config)
            fit_seconds = perf_counter() - fit_started
            predict_started = perf_counter()
            raw, pred = _predict(estimator, candidate, valid, config)
            predict_seconds = perf_counter() - predict_started
            metrics = _metrics(valid, raw, pred, config.target_column)
            fold_reports.append(
                {
                    "fold": fold.name,
                    "fit_seconds": fit_seconds,
                    "predict_seconds": predict_seconds,
                    **metrics,
                }
            )
            _progress(
                f"candidate {candidate.family}/{candidate.name}, fold {fold.name}: "
                f"MAE={metrics['overall']['mae']:.6f}, fit={fit_seconds:.1f}s"
            )
        tuning_rows.append(
            {
                "family": candidate.family,
                "name": candidate.name,
                "params": dict(candidate.params),
                "folds": fold_reports,
                "mean_validation_mae": float(np.mean([f["overall"]["mae"] for f in fold_reports])),
                "elapsed_seconds": perf_counter() - candidate_started,
            }
        )
        _progress(
            f"candidate {candidate.family}/{candidate.name}: mean validation "
            f"MAE={tuning_rows[-1]['mean_validation_mae']:.6f}"
        )
    family_winners = _candidate_winners(tuning_rows)
    selected = min(
        family_winners.values(), key=lambda row: (row["mean_validation_mae"], row["name"])
    )

    frozen_cutoff, h_start, h_end = (
        _utc(config.frozen_train_label_cutoff),
        _utc(config.holdout_start),
        _utc(config.holdout_end),
    )
    frozen_train, holdout = (
        _train_rows(df, frozen_cutoff),
        _target_interval(df, h_start, h_end, frozen_cutoff),
    )
    _assert_group_disjoint(frozen_train, holdout, "frozen train", "final holdout")
    if frozen_train.empty or holdout.empty:
        raise ValueError("Frozen final evaluation has empty train or holdout rows.")
    holdout_reports = []
    holdout_prediction_parts = []
    lookup = {(c.family, c.name): c for c in candidates}
    for winner in family_winners.values():
        candidate = lookup[(winner["family"], winner["name"])]
        _progress(f"holdout {candidate.family}/{candidate.name}: fitting family validation winner")
        fit_started = perf_counter()
        model = _fit(candidate, frozen_train, config)
        fit_seconds = perf_counter() - fit_started
        predict_started = perf_counter()
        raw, pred = _predict(model, candidate, holdout, config)
        predict_seconds = perf_counter() - predict_started
        metrics = _metrics(holdout, raw, pred, config.target_column)
        holdout_reports.append(
            {
                "family": candidate.family,
                "name": candidate.name,
                "params": dict(candidate.params),
                "fit_seconds": fit_seconds,
                "predict_seconds": predict_seconds,
                "metrics": metrics,
            }
        )
        predictions = holdout.loc[:, IDENTITY_COLUMNS].copy()
        predictions["power_actual"] = holdout[config.target_column].to_numpy(dtype=float)
        predictions["power_prediction"] = pred
        predictions["model_family"] = candidate.family
        predictions["model_name"] = candidate.name
        holdout_prediction_parts.append(predictions)
        _progress(
            f"holdout {candidate.family}/{candidate.name}: MAE={metrics['overall']['mae']:.6f}, "
            f"fit={fit_seconds:.1f}s"
        )

    chosen_candidate = lookup[(selected["family"], selected["name"])]
    refit_cutoff = _utc(config.operational_refit_label_cutoff)
    operational_train = _train_rows(df, refit_cutoff)
    if operational_train.empty:
        raise ValueError("Operational refit has no labels available by its cutoff.")
    _progress(
        f"operational refit {chosen_candidate.family}/{chosen_candidate.name}: "
        f"fitting {len(operational_train):,} rows"
    )
    refit_started = perf_counter()
    final_model = _fit(chosen_candidate, operational_train, config)
    refit_seconds = perf_counter() - refit_started
    _progress(f"operational refit complete in {refit_seconds:.1f}s")
    selected_holdout = next(
        row
        for row in holdout_reports
        if (row["family"], row["name"]) == (selected["family"], selected["name"])
    )
    artifact = ForecastArtifact(
        model=final_model,
        feature_schema=config.feature_columns,
        trained_until=refit_cutoff,
        model_config={
            "family": chosen_candidate.family,
            "name": chosen_candidate.name,
            "params": dict(chosen_candidate.params),
            "random_state": config.random_state,
            "n_jobs": config.n_jobs,
        },
        weather_contract={
            "required_identity_columns": list(REQUIRED_COLUMNS[:-1]),
            "lead_hours": [1, 48],
            "availability_rule": "weather_run_time <= weather_available_at <= forecast_origin",
            "categorical_features": list(config.categorical_features),
            **(
                {"trained_weather_models": sorted(set(df["weather_model"].dropna().astype(str)))}
                if "weather_model" in df.columns and df["weather_model"].notna().any()
                else {}
            ),
        },
        metrics={
            "selection": {
                "family": selected["family"],
                "name": selected["name"],
                "mean_validation_mae": selected["mean_validation_mae"],
            },
            "selected_frozen_holdout_overall": selected_holdout["metrics"]["overall"],
        },
    )
    report = {
        "plan": evaluation_plan(config),
        "tuning": tuning_rows,
        "selected_by_tuning_only": selected,
        "frozen_holdout_family_winners": holdout_reports,
        "operational_refit": {
            "trained_until": refit_cutoff.isoformat(),
            "rows": int(len(operational_train)),
            "fit_seconds": refit_seconds,
        },
        "elapsed_seconds": perf_counter() - run_started,
    }
    if output_dir is not None:
        import pandas as pd

        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        artifact_path, manifest_path = artifact.save(output / "best_model.pkl")
        tuning_path = output / "candidate_tuning.csv"
        leaderboard_path = output / "leaderboard.csv"
        holdout_path = output / "holdout_predictions.parquet"
        _tuning_table(tuning_rows).to_csv(tuning_path, index=False)
        _leaderboard_table(tuning_rows, holdout_reports, selected).to_csv(
            leaderboard_path, index=False
        )
        pd.concat(holdout_prediction_parts, ignore_index=True).to_parquet(holdout_path, index=False)
        report["artifacts"] = {
            "pickle": str(artifact_path),
            "manifest": str(manifest_path),
            "candidate_tuning_csv": str(tuning_path),
            "leaderboard_csv": str(leaderboard_path),
            "holdout_predictions_parquet": str(holdout_path),
            "report": str(output / "evaluation_report.json"),
        }
        (output / "evaluation_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
    _progress(f"evaluation complete in {perf_counter() - run_started:.1f}s")
    return report
