"""Explicit offline commands. Only ``train`` invokes hyperparameter evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import timedelta
from typing import Any

import pandas as pd

from .config import ProjectConfig, daily_origins, load_config, utc_timestamp
from .io import file_sha256, read_table, write_json, write_table


def _evaluation_config(config: ProjectConfig):
    from .evaluation import config_from_dict

    settings = dict(config.values["evaluation"])
    settings.pop("output_dir", None)
    settings["random_state"] = settings.pop("random_seed", 42)
    cutoff = settings.pop("final_train_cutoff")
    settings["operational_origin"] = cutoff
    settings["operational_refit_label_cutoff"] = cutoff
    return config_from_dict(settings)


def _client(config: ProjectConfig):
    from .weather import GfsArchiveClient

    settings = config.values["weather"]
    return GfsArchiveClient(
        config.path(settings["cache_dir"]),
        availability_delay=timedelta(hours=settings["publication_delay_hours"]),
    )


def _sites(config: ProjectConfig) -> dict[str, tuple[float, float]]:
    return {
        str(site["turbine_id"]): (site["latitude"], site["longitude"])
        for site in config.values["weather"]["sites"]
    }


def prepare(config: ProjectConfig) -> dict[str, Any]:
    from .data import read_observations

    settings = config.values["data"]
    paths = {key: config.path(value) for key, value in settings["inputs"].items()}
    hourly, diagnostics = read_observations(paths, timezone=settings["timezone"])
    output = config.path(settings["hourly_output"])
    report_path = config.path(settings["report_output"])
    write_table(hourly, output)
    write_table(diagnostics.incomplete_hours, report_path.with_name("incomplete_hours.csv"))
    write_table(diagnostics.invalid_rows, report_path.with_name("invalid_rows.csv"))
    report = {
        "source_files": {
            key: {"file": str(path), "sha256": file_sha256(path)} for key, path in paths.items()
        },
        "observation_timezone": settings["timezone"],
        "timezone_note": settings.get("timezone_note", "User-specified source timezone"),
        "label_convention": "UTC hour start, mean of six complete 10-minute readings",
        "target_units": "normalized active power, not MW or MWh",
        "summary": diagnostics.summary.to_dict(orient="records"),
        "complete_hourly_rows": len(hourly),
        "hourly_file": str(output),
        "hourly_sha256": file_sha256(output),
        "utc_start": hourly.target_time.min(),
        "utc_end": hourly.target_time.max(),
    }
    write_json(report, report_path)
    return report


def plan(config: ProjectConfig) -> dict[str, Any]:
    from .evaluation import evaluation_plan

    result = evaluation_plan(_evaluation_config(config))
    result["status"] = "plan_only_no_models_fitted"
    result["data_timezone"] = config.values["data"]["timezone"]
    result["timezone_note"] = config.values["data"].get("timezone_note")
    result["weather_source"] = config.values["weather"]["source"]
    result["output_directory"] = str(config.path(config.values["evaluation"]["output_dir"]))
    result["tuning_fit_count"] = len(result["candidates"]) * len(result["tune_folds"])
    return result


def weather_plan(config: ProjectConfig, args: argparse.Namespace) -> dict[str, Any]:
    settings = config.values["weather"]
    origins = daily_origins(
        args.start or settings["start_origin"], args.end or settings["end_origin"]
    )
    horizon = settings["horizon_hours"]
    result: dict[str, Any] = {
        "status": "no_grib_download",
        "origins": len(origins),
        "start_origin": origins[0],
        "end_origin": origins[-1],
        "hourly_targets_per_turbine_per_origin": horizon,
        "turbines": list(_sites(config)),
        "forecast_rows": len(origins) * horizon * len(_sites(config)),
        "global_grib_files": len(origins) * horizon,
        "selected_grib_messages_before_cache": len(origins) * horizon * 7,
        "note": "Downloads select seven global GRIB fields per file and extract site points. "
        "Transfer can be large; use --probe to estimate it from one forecast hour.",
    }
    if args.probe:
        sample = _client(config).preflight(
            origins[0].to_pydatetime(),
            lead_hours=[24],
            turbines=_sites(config),
            max_download_bytes=None,
        )
        result["sample_plan"] = asdict(sample)
        result["estimated_uncached_total_gib"] = (
            sample.estimated_bytes * len(origins) * horizon / 1024**3
        )
        result["estimate_note"] = "Extrapolated from one hour; field compression varies by run."
    return result


def _fetch(config: ProjectConfig, origins: pd.DatetimeIndex, max_gib: float) -> pd.DataFrame:
    if max_gib <= 0:
        raise ValueError("--max-download-gib must be positive")
    records = _client(config).replay_daily_forecasts(
        [origin.to_pydatetime() for origin in origins],
        turbines=_sites(config),
        lead_hours=range(1, config.values["weather"]["horizon_hours"] + 1),
        max_download_bytes=int(max_gib * 1024**3),
        max_workers=config.values["weather"].get("max_workers", 4),
    )
    frame = pd.DataFrame([asdict(record) for record in records])
    for column in ("forecast_origin", "target_time", "weather_run_time", "weather_available_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    from .features import validate_weather

    validate_weather(frame)
    return frame


def _fetch_sampled(
    config: ProjectConfig, origins: pd.DatetimeIndex, max_gib: float
) -> pd.DataFrame:
    """A deterministic four-lead sample, rotating through every forecast lead.

    Sampling changes evaluation coverage; it never substitutes observed weather
    or a forecast from a later issue time. The manifest records this restriction.
    """
    settings = config.values["weather"]
    if settings["horizon_hours"] != 48:
        raise ValueError("The rotating sampled benchmark requires a 48-hour horizon")
    stride = settings.get("sampling_stride_days", 3)
    if stride < 1 or max_gib <= 0:
        raise ValueError("Sampling stride and transfer budget must be positive")
    selected = origins[::stride]
    client = _client(config)
    budget = int(max_gib * 1024**3)
    frames = []
    for phase in range(12):
        phase_origins = selected[phase::12]
        if len(phase_origins) == 0:
            continue
        leads = [phase + offset for offset in (1, 13, 25, 37)]
        print(
            f"Weather phase {phase + 1}/12: {len(phase_origins)} origins, leads {leads}",
            file=sys.stderr,
            flush=True,
        )
        records = client.replay_daily_forecasts(
            [origin.to_pydatetime() for origin in phase_origins],
            turbines=_sites(config),
            lead_hours=leads,
            max_download_bytes=budget,
            max_workers=settings.get("max_workers", 4),
        )
        budget -= client.last_estimated_bytes
        frames.append(pd.DataFrame([asdict(record) for record in records]))
    frame = pd.concat(frames, ignore_index=True)
    for column in ("forecast_origin", "target_time", "weather_run_time", "weather_available_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame["sampling_policy"] = f"origin_stride_{stride}_days_four_rotating_leads"
    from .features import validate_weather

    validate_weather(frame)
    return frame.sort_values(["forecast_origin", "turbine_id", "lead_hours"]).reset_index(drop=True)


def fetch_weather(config: ProjectConfig, args: argparse.Namespace) -> dict[str, Any]:
    settings = config.values["weather"]
    origins = daily_origins(
        args.start or settings["start_origin"], args.end or settings["end_origin"]
    )
    frame = (
        _fetch_sampled(config, origins, args.max_download_gib)
        if args.sampled
        else _fetch(config, origins, args.max_download_gib)
    )
    output = config.path(args.output or settings["sampled_output" if args.sampled else "output"])
    write_table(frame, output)
    summary = {
        "rows": len(frame),
        "origins": frame.forecast_origin.nunique(),
        "sampled": args.sampled,
        "output": str(output),
        "sha256": file_sha256(output),
    }
    write_json(summary, output.with_suffix(".manifest.json"))
    return summary


def build_dataset(config: ProjectConfig, args: argparse.Namespace) -> dict[str, Any]:
    from .features import build_training_table

    hourly_path = config.path(config.values["data"]["hourly_output"])
    quality_path = config.path(config.values["data"]["report_output"])
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    if quality["observation_timezone"] != config.values["data"]["timezone"]:
        raise ValueError(
            "Timezone changed since prepare; rerun prepare before building the dataset"
        )
    if quality["hourly_sha256"] != file_sha256(hourly_path):
        raise ValueError("Hourly observations changed since prepare; regenerate them")
    weather_path = config.path(args.weather or config.values["weather"]["output"])
    hourly, weather = read_table(hourly_path), read_table(weather_path)
    table = build_training_table(hourly, weather)
    if table.empty:
        raise ValueError("No complete observed hours overlap the supplied weather forecasts")
    output = config.path(args.output)
    write_table(table, output)
    manifest = {
        "rows": len(table),
        "weather_rows": len(weather),
        "rows_without_complete_label": len(weather) - len(table),
        "dataset": str(output),
        "dataset_sha256": file_sha256(output),
        "hourly_sha256": quality["hourly_sha256"],
        "weather_sha256": file_sha256(weather_path),
        "observation_timezone": quality["observation_timezone"],
        "timezone_note": quality["timezone_note"],
        "source_files": quality["source_files"],
        "weather_source": config.values["weather"]["source"],
        "configured_weather_sites": config.values["weather"]["sites"],
        "configured_publication_delay_hours": config.values["weather"]["publication_delay_hours"],
        "sampling_policies": weather.sampling_policy.unique().tolist()
        if "sampling_policy" in weather
        else ["all_requested_leads"],
    }
    write_json(manifest, output.with_suffix(".manifest.json"))
    return manifest


def train(config: ProjectConfig, args: argparse.Namespace) -> dict[str, Any]:
    from .artifact import ForecastArtifact
    from .evaluation import run_evaluation

    dataset_path = config.path(args.dataset)
    manifest_path = dataset_path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["dataset_sha256"] != file_sha256(dataset_path):
        raise ValueError("Training dataset does not match its manifest; rerun build-dataset")
    if manifest["observation_timezone"] != config.values["data"]["timezone"]:
        raise ValueError("Dataset timezone differs from configuration; regenerate preprocessing")
    output = config.path(args.output or config.values["evaluation"]["output_dir"])
    report = run_evaluation(read_table(dataset_path), _evaluation_config(config), output)
    artifact = ForecastArtifact.load(output / "best_model.pkl")
    artifact.weather_contract = {**dict(artifact.weather_contract), **manifest}
    coverage = {
        "benchmark": "sampled_archived_weather"
        if any(policy != "all_requested_leads" for policy in manifest["sampling_policies"])
        else "all_requested_forecasts",
        "sampling_policies": manifest["sampling_policies"],
        "training_dataset_rows": manifest["rows"],
        "weather_rows": manifest["weather_rows"],
        "observation_timezone": manifest["observation_timezone"],
        "timezone_note": manifest["timezone_note"],
    }
    artifact.metrics = {**dict(artifact.metrics), "data_coverage": coverage}
    artifact.save(output / "best_model.pkl")
    report["data_coverage"] = coverage
    write_json(report, output / "evaluation_report.json")
    write_json(config.values, output / "run_config.json")
    write_json(manifest, output / "dataset_manifest.json")
    return report


def predict(config: ProjectConfig, args: argparse.Namespace) -> dict[str, Any]:
    from .artifact import ForecastArtifact

    artifact = ForecastArtifact.load(config.path(args.model))
    weather = read_table(config.path(args.weather))
    predictions = artifact.predict(weather)
    output = config.path(args.output)
    write_table(predictions, output)
    return {"rows": len(predictions), "output": str(output), "model": artifact.model_config}


def replay(config: ProjectConfig, args: argparse.Namespace) -> dict[str, Any]:
    from .artifact import ForecastArtifact

    settings = config.values["replay"]
    origins = daily_origins(settings["start_origin"], settings["end_origin"])
    artifact = ForecastArtifact.load(config.path(args.model))
    weather = (
        read_table(config.path(args.weather))
        if args.weather
        else _fetch(config, origins, args.max_download_gib)
    )
    weather = weather.loc[weather.forecast_origin.isin(origins)].copy()
    expected_rows = len(origins) * config.values["weather"]["horizon_hours"] * len(_sites(config))
    if len(weather) != expected_rows:
        raise ValueError(f"Replay requires {expected_rows} weather rows, received {len(weather)}")
    expected = pd.MultiIndex.from_product(
        [origins, list(_sites(config)), range(1, config.values["weather"]["horizon_hours"] + 1)],
        names=["forecast_origin", "turbine_id", "lead_hours"],
    )
    actual = pd.MultiIndex.from_frame(weather[["forecast_origin", "turbine_id", "lead_hours"]])
    if actual.has_duplicates or not expected.difference(actual).empty:
        raise ValueError("Replay has missing or duplicate turbine/origin/horizon combinations")
    predictions = artifact.predict(weather)
    start = utc_timestamp(settings["target_start"], "target_start")
    end = utc_timestamp(settings["target_end_exclusive"], "target_end_exclusive")
    scored_period = predictions.loc[predictions.target_time.between(start, end, inclusive="left")]
    # Keep all overlapping forecasts for separate 24/48h scoring; the daily export
    # uses a fixed predeclared horizon policy rather than hindsight-based selection.
    day_ahead = scored_period.loc[scored_period.lead_hours.le(24)].copy()
    expected_day_ahead = len(_sites(config)) * int((end - start).total_seconds() / 3600)
    if (
        len(day_ahead) != expected_day_ahead
        or day_ahead.duplicated(["turbine_id", "target_time"]).any()
    ):
        raise ValueError(
            "Replay issue times do not give exactly one day-ahead forecast per target hour"
        )
    output = config.path(args.output or settings["output_dir"])
    write_table(predictions, output / "all_forecasts.parquet")
    write_table(scored_period, output / "february_all_horizons.csv")
    write_table(day_ahead, output / "february_day_ahead.csv")
    result = {
        "origins": len(origins),
        "all_forecast_rows": len(predictions),
        "february_day_ahead_rows": len(day_ahead),
        "output_directory": str(output),
        "accuracy": "unavailable: February observed power is not supplied",
    }
    write_json(result, output / "replay_manifest.json")
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--config", default="configs/default.json")
    sub = command.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="Aggregate complete hourly labels and write diagnostics; no fit")
    sub.add_parser("plan", help="Print candidate configurations and temporal splits; no fit")
    weather = sub.add_parser("weather-plan", help="Estimate archive workload; no GRIB downloads")
    weather.add_argument("--start")
    weather.add_argument("--end")
    weather.add_argument(
        "--probe", action="store_true", help="Fetch one small index for a byte estimate"
    )
    fetch = sub.add_parser(
        "fetch-weather", help="Download verified archived weather, with a transfer cap"
    )
    fetch.add_argument("--start")
    fetch.add_argument("--end")
    fetch.add_argument("--output")
    fetch.add_argument(
        "--sampled",
        action="store_true",
        help="Every third origin, four rotating leads; documented sample coverage",
    )
    fetch.add_argument("--max-download-gib", type=float, default=2.0)
    build = sub.add_parser(
        "build-dataset", help="Join complete hourly labels to archived forecasts; no fit"
    )
    build.add_argument("--weather")
    build.add_argument("--output", default="data/processed/training.parquet")
    fit = sub.add_parser(
        "train", help="RUN real tuning, holdout evaluation, and final model fitting"
    )
    fit.add_argument("--dataset", default="data/processed/training.parquet")
    fit.add_argument("--output")
    forecast = sub.add_parser(
        "predict", help="Predict from a trusted fitted pickle and weather table"
    )
    forecast.add_argument("--model", default="artifacts/evaluation/best_model.pkl")
    forecast.add_argument("--weather", required=True)
    forecast.add_argument("--output", default="artifacts/predictions.csv")
    backtest = sub.add_parser("replay", help="Generate the February daily 24/48h forecasts")
    backtest.add_argument("--model", default="artifacts/evaluation/best_model.pkl")
    backtest.add_argument(
        "--weather", help="Use already-downloaded weather instead of network access"
    )
    backtest.add_argument("--output")
    backtest.add_argument("--max-download-gib", type=float, default=2.0)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "prepare":
            result = prepare(config)
        elif args.command == "plan":
            result = plan(config)
        elif args.command == "weather-plan":
            result = weather_plan(config, args)
        else:
            handler = {
                "fetch-weather": fetch_weather,
                "build-dataset": build_dataset,
                "train": train,
                "predict": predict,
                "replay": replay,
            }[args.command]
            result = handler(config, args)
        print(json.dumps(result, indent=2, default=str, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
