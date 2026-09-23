"""Integration checks for the default, CSV-only command workflow."""

import json
from pathlib import Path

import pandas as pd
import pytest

from wind_forecast import offline
from wind_forecast.io import file_sha256, read_table, write_json, write_table


def _config(tmp_path):
    values = json.loads(Path("configs/history.json").read_text())
    values["project_root"] = "."
    values["data"]["timezone"] = "UTC"
    values["data"]["timezone_note"] = "synthetic UTC fixture"
    config_path = tmp_path / "config.json"
    write_json(values, config_path)
    return config_path, values


def test_csv_only_plan_has_no_fit_or_weather_command(tmp_path, monkeypatch, capsys):
    config_path, _ = _config(tmp_path)
    from wind_forecast import history_models

    monkeypatch.setattr(
        history_models,
        "build_estimator",
        lambda *args, **kwargs: pytest.fail("Plan cannot construct models"),
    )
    assert offline.main(["--config", str(config_path), "plan"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "csv_only_no_network"
    assert len(report["candidates"]) == 14
    assert report["tuning_fit_count"] == 28
    with pytest.raises(SystemExit):
        offline.main(["fetch-weather"])


def test_csv_build_dataset_and_tamper_guard(tmp_path, monkeypatch):
    config_path, values = _config(tmp_path)
    times = pd.date_range("2025-01-01", periods=192, freq="h", tz="UTC")
    hourly = pd.concat(
        [
            pd.DataFrame(
                {
                    "target_time": times,
                    "turbine_id": turbine,
                    "power": 0.25,
                    "observed_wind": 4.0,
                    "observed_temperature": 10.0,
                    "n_samples": 6,
                    "complete_hour": True,
                }
            )
            for turbine in ("1", "2")
        ],
        ignore_index=True,
    )
    hourly_path = tmp_path / values["data"]["hourly_output"]
    write_table(hourly, hourly_path)
    write_json(
        {
            "hourly_sha256": file_sha256(hourly_path),
            "observation_timezone": "UTC",
            "timezone_note": "synthetic",
            "source_files": {},
        },
        tmp_path / values["data"]["report_output"],
    )
    assert offline.main(["--config", str(config_path), "build-dataset"]) == 0
    dataset_path = tmp_path / values["history"]["dataset_output"]
    frame = read_table(dataset_path)
    manifest = json.loads(dataset_path.with_suffix(".manifest.json").read_text())
    assert len(frame) > 0
    assert frame.y_01.eq(0.25).all() and frame.y_48.eq(0.25).all()
    assert manifest["external_weather_used"] is False
    assert manifest["dataset_sha256"] == file_sha256(dataset_path)

    from wind_forecast import history_evaluation

    monkeypatch.setattr(
        history_evaluation,
        "run_evaluation",
        lambda *args, **kwargs: pytest.fail("Must reject changed dataset before fitting"),
    )
    dataset_path.write_bytes(dataset_path.read_bytes() + b"changed")
    assert offline.main(["--config", str(config_path), "train"]) == 2


def test_run_orders_local_preparation_build_and_training(tmp_path, monkeypatch):
    config_path, _ = _config(tmp_path)
    calls = []
    for name in ("prepare", "build_dataset", "train"):
        monkeypatch.setattr(
            offline, name, lambda *args, _name=name, **kwargs: calls.append(_name) or {}
        )
    assert offline.main(["--config", str(config_path), "run"]) == 0
    assert calls == ["prepare", "build_dataset", "train"]
