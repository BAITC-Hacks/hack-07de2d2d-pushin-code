"""Default, network-free CLI: turbine measurement history -> next 24/48 hours."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from .config import ProjectConfig, utc_timestamp
from .io import file_sha256, read_table, write_json, write_table


def load_history_config(path: str | Path) -> ProjectConfig:
    location = Path(path).resolve()
    values = json.loads(location.read_text(encoding="utf-8"))
    for section in ("data", "history", "evaluation"):
        if not isinstance(values.get(section), dict):
            raise ValueError(f"Configuration requires a {section!r} object")
    data, history = values["data"], values["history"]
    if not data.get("timezone") or not isinstance(data.get("inputs"), dict):
        raise ValueError("Explicit observation timezone and turbine CSV inputs are required")
    if not data["inputs"] or not set(data["inputs"]).issubset({"1", "2"}):
        raise ValueError("Only the supplied turbine IDs 1 and 2 are supported")
    stride = history.get("origin_stride_hours")
    if isinstance(stride, bool) or not isinstance(stride, int) or stride < 1 or 24 % stride:
        raise ValueError("origin_stride_hours must be a positive integer dividing 24")
    if history.get("issue_hour_utc") != 19:
        raise ValueError("The declared evaluation protocol uses issue_hour_utc=19")
    if (
        history.get("horizon_hours") != 48
        or history.get("max_lookback_hours") != 168
        or history.get("required_recent_hours") != 24
    ):
        raise ValueError("This schema requires horizon=48, lookback=168, and recent_hours=24")
    jobs = values["evaluation"].get("n_jobs")
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ValueError("evaluation.n_jobs must be a positive integer")
    utc_timestamp(values["evaluation"]["final_train_cutoff"], "final_train_cutoff")
    return ProjectConfig((location.parent / values.get("project_root", "..")).resolve(), values)


def prepare(config: ProjectConfig) -> dict[str, Any]:
    # This audited helper only reads local observations; it does not fetch weather.
    from .cli import prepare as prepare_observations

    return prepare_observations(config)


def build_dataset(config: ProjectConfig) -> dict[str, Any]:
    from .history import FEATURE_COLUMNS, TARGET_COLUMNS, build_supervised_history

    settings = config.values["data"]
    history = config.values["history"]
    hourly_path = config.path(settings["hourly_output"])
    quality = json.loads(config.path(settings["report_output"]).read_text(encoding="utf-8"))
    if quality["hourly_sha256"] != file_sha256(hourly_path):
        raise ValueError("Hourly data changed since prepare; run prepare again")
    if quality["observation_timezone"] != settings["timezone"]:
        raise ValueError("Timezone changed since prepare; regenerate hourly observations")
    hourly = read_table(hourly_path)
    stride = history["origin_stride_hours"]
    start = hourly.target_time.min() + pd.Timedelta(24, unit="h")
    start += pd.Timedelta((history["issue_hour_utc"] - start.hour) % stride, unit="h")
    end = hourly.target_time.max() + pd.Timedelta(1 - 48, unit="h")
    origins = pd.date_range(start, end, freq=f"{stride}h")
    print(f"[history] Building past-only features for {len(origins):,} origins", file=sys.stderr)
    table = build_supervised_history(hourly, origins)
    if table.empty:
        raise ValueError("No eligible history windows with complete 48-hour targets")
    output = config.path(history["dataset_output"])
    write_table(table, output)
    manifest = {
        "mode": "csv_only_measurement_history",
        "external_weather_used": False,
        "rows": len(table),
        "candidate_origins": len(origins),
        "candidate_origin_turbine_pairs": len(origins) * hourly.turbine_id.nunique(),
        "origin_stride_hours": stride,
        "target_horizon_hours": 48,
        "feature_schema": list(FEATURE_COLUMNS),
        "target_columns": list(TARGET_COLUMNS),
        "origin_definition": (
            "first forecast hour start; only observations ending by origin are known"
        ),
        "dataset": str(output),
        "dataset_sha256": file_sha256(output),
        "hourly_sha256": quality["hourly_sha256"],
        "source_files": quality["source_files"],
        "observation_timezone": quality["observation_timezone"],
        "timezone_note": quality["timezone_note"],
        "first_origin": table.forecast_origin.min(),
        "last_origin": table.forecast_origin.max(),
    }
    write_json(manifest, output.with_suffix(".manifest.json"))
    return manifest


def plan(config: ProjectConfig) -> dict[str, Any]:
    from .history_models import candidate_catalogue

    candidates = candidate_catalogue()
    return {
        "mode": "csv_only_no_network",
        "status": "plan_only_no_models_fitted",
        "candidates": [
            {"family": c.family, "name": c.name, "params": dict(c.params)} for c in candidates
        ],
        "tuning_fit_count": 2 * len(candidates),
        "selection": "mean October/November 2025 validation MAE",
        "holdout": "December 2025–January 2026, frozen report-only",
        "history": config.values["history"],
        "timezone": config.values["data"]["timezone"],
    }


def train(config: ProjectConfig, output_override: str | None = None) -> dict[str, Any]:
    from .history_evaluation import run_evaluation

    dataset_path = config.path(config.values["history"]["dataset_output"])
    manifest = json.loads(dataset_path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if manifest["dataset_sha256"] != file_sha256(dataset_path):
        raise ValueError("Dataset SHA-256 mismatch; regenerate it with build-dataset")
    if manifest["observation_timezone"] != config.values["data"]["timezone"]:
        raise ValueError("Dataset timezone differs from the active configuration")
    if manifest.get("external_weather_used") is not False:
        raise ValueError("Expected a CSV-only history dataset")
    settings = config.values["evaluation"]
    output = config.path(output_override or settings["output_dir"])
    report = run_evaluation(
        read_table(dataset_path),
        output_dir=output,
        random_state=settings["random_seed"],
        n_jobs=settings["n_jobs"],
        final_refit_cutoff=settings["final_train_cutoff"],
        provenance=manifest,
    )
    write_json(config.values, output / "run_config.json")
    write_json(manifest, output / "dataset_manifest.json")
    return report


def predict(config: ProjectConfig, args: argparse.Namespace) -> dict[str, Any]:
    from .history_artifact import HistoryForecastArtifact

    hourly = read_table(config.path(args.history or config.values["data"]["hourly_output"]))
    origin = (
        utc_timestamp(args.origin, "origin")
        if args.origin
        else hourly.groupby("turbine_id").target_time.max().min() + pd.Timedelta(1, unit="h")
    )
    model_path = config.path(
        args.model or f"{config.values['evaluation']['output_dir']}/best_model.pkl"
    )
    artifact = HistoryForecastArtifact.load(model_path)
    predictions = artifact.predict(hourly, origin)
    predictions = predictions.loc[predictions.lead_hours <= args.hours].copy()
    output = config.path(args.output)
    write_table(predictions, output)
    return {
        "mode": "csv_only",
        "origin": origin,
        "hours": args.hours,
        "rows": len(predictions),
        "output": str(output),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/history.json")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "build-dataset", "plan"):
        sub.add_parser(name)
    for name in ("train", "run"):
        sub.add_parser(name).add_argument("--output")
    inference = sub.add_parser("predict")
    inference.add_argument("--model")
    inference.add_argument("--history", help="Prepared hourly measurements, never future weather")
    inference.add_argument("--origin", help="First future-hour start, explicitly timezone-aware")
    inference.add_argument("--hours", type=int, choices=(24, 48), default=48)
    inference.add_argument("--output", default="artifacts/csv_only/forecast_48h.csv")
    args = parser.parse_args(argv)
    try:
        config = load_history_config(args.config)
        if args.command == "prepare":
            result = prepare(config)
        elif args.command == "build-dataset":
            result = build_dataset(config)
        elif args.command == "plan":
            result = plan(config)
        elif args.command == "predict":
            result = predict(config, args)
        else:
            if args.command == "run":
                prepare(config)
                build_dataset(config)
            result = train(config, args.output)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
