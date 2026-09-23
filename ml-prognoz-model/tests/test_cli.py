"""Small end-to-end CLI tests using local synthetic data and fake archive clients."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from wind_forecast import cli
from wind_forecast.data import RAW_COLUMNS
from wind_forecast.io import file_sha256, read_table, write_table
from wind_forecast.weather import WeatherRecord


def _write_config(tmp_path: Path, turbine_ids: tuple[str, ...] = ("1",)) -> Path:
    inputs = {turbine_id: f"raw_{turbine_id}.csv" for turbine_id in turbine_ids}
    sites = [
        {"turbine_id": turbine_id, "latitude": 43.6 + int(turbine_id) / 100, "longitude": 78.5}
        for turbine_id in turbine_ids
    ]
    config = {
        "project_root": ".",
        "data": {
            "inputs": inputs,
            "timezone": "UTC",
            "timezone_note": "synthetic test convention",
            "hourly_output": "data/hourly.parquet",
            "report_output": "reports/quality.json",
        },
        "weather": {
            "source": "noaa_gfs",
            "sites": sites,
            "start_origin": "2025-01-01T18:00:00Z",
            "end_origin": "2025-02-06T18:00:00Z",
            "horizon_hours": 48,
            "publication_delay_hours": 6,
            "sampling_stride_days": 3,
            "cache_dir": "data/cache",
            "sampled_output": "data/weather/training_sampled.parquet",
            "output": "data/weather/training.parquet",
        },
        "evaluation": {
            "random_seed": 7,
            "n_jobs": 1,
            "output_dir": "artifacts/evaluation",
            "final_train_cutoff": "2026-01-31T18:00:00Z",
        },
        "replay": {
            "start_origin": "2026-01-31T18:00:00Z",
            "end_origin": "2026-02-27T18:00:00Z",
            "target_start": "2026-01-31T19:00:00Z",
            "target_end_exclusive": "2026-02-28T19:00:00Z",
            "output_dir": "artifacts/replay",
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _write_complete_raw(path: Path, hour: str = "2025-01-01 00") -> None:
    minutes = [f"{hour}:{minute:02d}:00" for minute in range(0, 60, 10)]
    pd.DataFrame(
        {
            "ID": range(1, 7),
            RAW_COLUMNS["timestamp"]: minutes,
            RAW_COLUMNS["wind"]: [6.0] * 6,
            RAW_COLUMNS["power"]: [0.5] * 6,
            RAW_COLUMNS["temperature"]: [5.0] * 6,
        }
    ).to_csv(path, index=False)


def _weather_rows(origins: pd.DatetimeIndex, turbine_ids: tuple[str, ...], leads) -> pd.DataFrame:
    rows = []
    for origin in origins:
        for turbine_id in turbine_ids:
            for lead in leads:
                lead_delta = timedelta(hours=int(lead))
                rows.append(
                    {
                        "turbine_id": turbine_id,
                        "forecast_origin": origin,
                        "target_time": origin + lead_delta,
                        "weather_run_time": origin - timedelta(hours=6),
                        "weather_available_at": origin - timedelta(hours=6),
                        "lead_hours": lead,
                        "wind_speed_10m": 4.0,
                        "wind_speed_100m": 7.0,
                        "wind_u_100m": 3.0,
                        "wind_v_100m": -2.0,
                        "temperature_2m": 8.0,
                        "surface_pressure": 100000.0,
                        "gust_speed": 9.0,
                        "weather_model": "gfs.0p25",
                        "weather_source": "https://example.test/gfs",
                    }
                )
    return pd.DataFrame(rows)


@pytest.fixture
def prepared_dataset(tmp_path: Path) -> tuple[Path, Path]:
    config = _write_config(tmp_path)
    _write_complete_raw(tmp_path / "raw_1.csv")
    assert cli.main(["--config", str(config), "prepare"]) == 0
    weather = _weather_rows(pd.DatetimeIndex([pd.Timestamp("2024-12-31T18:00:00Z")]), ("1",), [6])
    weather_path = tmp_path / "data/weather/training.parquet"
    write_table(weather, weather_path)
    dataset = tmp_path / "data/training.parquet"
    assert (
        cli.main(
            [
                "--config",
                str(config),
                "build-dataset",
                "--weather",
                "data/weather/training.parquet",
                "--output",
                "data/training.parquet",
            ]
        )
        == 0
    )
    return config, dataset


def test_prepare_then_build_dataset_joins_target_and_writes_integrity_manifest(
    prepared_dataset: tuple[Path, Path],
) -> None:
    config, dataset = prepared_dataset
    table = read_table(dataset)
    manifest = json.loads(dataset.with_suffix(".manifest.json").read_text(encoding="utf-8"))

    assert table[["turbine_id", "lead_hours", "power"]].to_dict("records") == [
        {"turbine_id": "1", "lead_hours": 6, "power": 0.5}
    ]
    assert str(table.loc[0, "target_time"]) == "2025-01-01 00:00:00+00:00"
    assert manifest["dataset_sha256"] == file_sha256(dataset)
    assert manifest["weather_rows"] == manifest["rows"] == 1
    assert manifest["sampling_policies"] == ["all_requested_leads"]
    assert manifest["configured_weather_sites"][0]["turbine_id"] == "1"
    assert manifest["configured_publication_delay_hours"] == 6
    assert (
        json.loads((config.parent / "reports/quality.json").read_text())["complete_hourly_rows"]
        == 1
    )


def test_train_rejects_tampered_dataset_before_any_fit(
    prepared_dataset: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, dataset = prepared_dataset
    dataset.write_bytes(dataset.read_bytes() + b"tampered")
    import wind_forecast.evaluation as evaluation

    monkeypatch.setattr(
        evaluation,
        "run_evaluation",
        lambda *_args, **_kwargs: pytest.fail("training must not begin after a checksum mismatch"),
    )

    assert cli.main(["--config", str(config), "train", "--dataset", "data/training.parquet"]) == 2
    assert "does not match its manifest" in capsys.readouterr().err


def test_plan_never_constructs_or_fits_an_estimator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    import wind_forecast.evaluation as evaluation

    monkeypatch.setattr(
        evaluation,
        "_fit",
        lambda *_args, **_kwargs: pytest.fail("plan must not fit an estimator"),
    )

    assert cli.main(["--config", str(config), "plan"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "plan_only_no_models_fitted"
    assert result["tuning_fit_count"] == 36


def test_replay_exports_1344_day_ahead_rows_and_keeps_overlapping_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path, ("1", "2"))
    origins = pd.date_range("2026-01-31T18:00:00Z", "2026-02-27T18:00:00Z", freq="D")
    weather_path = tmp_path / "data/weather/february.parquet"
    write_table(_weather_rows(origins, ("1", "2"), range(1, 49)), weather_path)

    class FakeArtifact:
        def predict(self, weather: pd.DataFrame) -> pd.DataFrame:
            result = weather[
                [
                    "turbine_id",
                    "forecast_origin",
                    "target_time",
                    "weather_run_time",
                    "weather_available_at",
                    "lead_hours",
                ]
            ].copy()
            result["power_prediction"] = 0.5
            return result

    import wind_forecast.artifact as artifact

    monkeypatch.setattr(artifact.ForecastArtifact, "load", lambda _path: FakeArtifact())
    assert (
        cli.main(
            [
                "--config",
                str(config),
                "replay",
                "--weather",
                "data/weather/february.parquet",
                "--output",
                "artifacts/replay",
            ]
        )
        == 0
    )

    output = tmp_path / "artifacts/replay"
    day_ahead = read_table(output / "february_day_ahead.csv")
    all_horizons = read_table(output / "february_all_horizons.csv")
    assert len(day_ahead) == 672 * 2 == 1344
    assert not day_ahead.duplicated(["turbine_id", "target_time"]).any()
    assert all_horizons.duplicated(["turbine_id", "target_time"]).any()


def test_sampled_fetch_rotates_all_48_leads_and_deducts_one_shared_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    requested_budgets: list[int] = []

    class FakeClient:
        last_estimated_bytes = 100

        def replay_daily_forecasts(
            self, origins, *, turbines, lead_hours, max_download_bytes, **_kwargs
        ):
            requested_budgets.append(max_download_bytes)
            records = []
            for origin in origins:
                for turbine_id in turbines:
                    for lead in lead_hours:
                        lead_delta = timedelta(hours=int(lead))
                        records.append(
                            WeatherRecord(
                                turbine_id=turbine_id,
                                forecast_origin=origin,
                                target_time=origin + lead_delta,
                                weather_run_time=origin - timedelta(hours=6),
                                weather_available_at=origin - timedelta(hours=6),
                                lead_hours=lead,
                                wind_speed_10m=4.0,
                                wind_speed_100m=7.0,
                                wind_u_100m=3.0,
                                wind_v_100m=-2.0,
                                temperature_2m=8.0,
                                surface_pressure=100000.0,
                                gust_speed=9.0,
                                weather_model="gfs.0p25",
                                weather_source="https://example.test/gfs",
                            )
                        )
            return records

    fake_client = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda _config: fake_client)
    assert (
        cli.main(
            [
                "--config",
                str(config),
                "fetch-weather",
                "--sampled",
                "--max-download-gib",
                "1",
            ]
        )
        == 0
    )

    table = read_table(tmp_path / "data/weather/training_sampled.parquet")
    assert set(table["lead_hours"]) == set(range(1, 49))
    assert table["sampling_policy"].unique().tolist() == [
        "origin_stride_3_days_four_rotating_leads"
    ]
    initial_budget = 1024**3
    assert requested_budgets == [initial_budget - 100 * phase for phase in range(12)]
