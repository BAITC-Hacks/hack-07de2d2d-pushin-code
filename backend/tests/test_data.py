from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from windcast.config import scada_utc_offset_hours
from windcast.data import (
    HOURLY_COLUMNS,
    build_hourly_dataset,
    check_data,
    hourly_quality_summary,
    load_hourly_dataset,
)
from windcast.timeline import live_times, target_times_utc

RAW_HEADER = (
    "ID,Статистическое время,Средняя скорость ветра(m/s),"
    "Нормализованная активная мощность,Средняя температура окружающей среды(°C)\n"
)


def _rows(hour: int, *, count: int = 6, wind: float = 7.0, power: float = 0.4) -> str:
    return "".join(
        f"{index},2024-05-17 {hour}:{index * 10:02}:00,{wind},{power},{12 + index}\n"
        for index in range(count)
    )


@pytest.fixture
def scada_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    # T1 has a complete hour, an incomplete hour, an idle hour, an entirely
    # absent grid hour, and the documented blackout date. T2 proves both files
    # are loaded.
    (raw / "turbine_1.csv").write_text(
        RAW_HEADER
        + _rows(0)
        + _rows(1, count=5)
        + _rows(2, wind=7.5, power=0.01)
        + _rows(4)
        + "".join(
            f"{index},2024-05-18 0:{index * 10:02}:00,7,0.4,{12 + index}\n"
            for index in range(6)
        ),
        encoding="utf-8",
    )
    (raw / "turbine_2.csv").write_text(
        RAW_HEADER + _rows(0, power=0.5) + _rows(1, power=0.6), encoding="utf-8"
    )
    monkeypatch.setenv("WINDCAST_ROOT", str(tmp_path))
    yield tmp_path


def test_scada_offset_is_limited_to_documented_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCADA_UTC_OFFSET_H", raising=False)
    assert scada_utc_offset_hours() == 5
    monkeypatch.setenv("SCADA_UTC_OFFSET_H", "6")
    assert scada_utc_offset_hours() == 6
    monkeypatch.setenv("SCADA_UTC_OFFSET_H", "4")
    with pytest.raises(ValueError, match="5 или 6"):
        scada_utc_offset_hours()


def test_builds_explicit_hourly_grid_and_marks_invalid_scada(
    scada_root: Path,
) -> None:
    report = build_hourly_dataset(offset_hours=5)
    hourly = load_hourly_dataset()

    assert list(hourly.columns) == HOURLY_COLUMNS
    assert report.output_path == scada_root / "data" / "processed" / "hourly.parquet"
    assert report.output_path.is_file()
    assert set(hourly["turbine"]) == {"1", "2"}
    assert str(hourly["ts_utc"].dtype).startswith("datetime64[ns, UTC]")

    t1 = hourly.loc[hourly["turbine"] == "1"].set_index("ts_utc")
    assert len(t1) == 25
    assert t1.loc[pd.Timestamp("2024-05-16T19:00:00Z"), "valid"]
    assert not t1.loc[pd.Timestamp("2024-05-16T20:00:00Z"), "valid"]
    assert not t1.loc[pd.Timestamp("2024-05-16T21:00:00Z"), "valid"]
    assert not t1.loc[pd.Timestamp("2024-05-16T22:00:00Z"), "valid"]
    assert not t1.loc[pd.Timestamp("2024-05-17T19:00:00Z"), "valid"]
    assert t1.loc[pd.Timestamp("2024-05-16T19:00:00Z"), "wind_ms"] == pytest.approx(7.0)
    assert t1.loc[pd.Timestamp("2024-05-16T19:00:00Z"), "power"] == pytest.approx(0.4)

    t2 = hourly.loc[hourly["turbine"] == "2"].set_index("ts_utc")
    assert len(t2) == 2
    assert t2["valid"].all()
    assert report.rows_by_turbine == {"1": 25, "2": 2}


def test_test_only_fastapi_harness_exposes_pipeline_quality(scada_root: Path) -> None:
    build_hourly_dataset(offset_hours=5)
    app = FastAPI()

    @app.get("/_test/hourly-quality")
    def hourly_quality() -> list[dict[str, object]]:
        return hourly_quality_summary(load_hourly_dataset()).to_dict(orient="records")

    response = TestClient(app).get("/_test/hourly-quality")

    assert response.status_code == 200
    assert response.json() == [
        {"turbine": "1", "hours": 25, "valid_hours": 2, "valid_fraction": 0.08},
        {"turbine": "2", "hours": 2, "valid_hours": 2, "valid_fraction": 1.0},
    ]


def test_offset_candidate_rebuilds_existing_parquet(scada_root: Path) -> None:
    build_hourly_dataset(offset_hours=5)
    five = (
        load_hourly_dataset().loc[lambda frame: frame["turbine"] == "2", "ts_utc"].min()
    )

    six = (
        load_hourly_dataset(offset_hours=6)
        .loc[lambda frame: frame["turbine"] == "2", "ts_utc"]
        .min()
    )

    assert six == five - pd.Timedelta(hours=1)


def test_check_data_requires_exact_weather_contract() -> None:
    targets = target_times_utc("2026-02-13")
    weather = {
        "hourly": pd.DataFrame(
            {
                "h": range(1, 49),
                "target_time_utc": targets,
                "wind_100m_ms": [7.0] * 48,
                "temp_c": [-2.0] * 48,
                "init_time_utc": ["2026-02-12T19:00:00Z"] * 48,
            }
        )
    }

    assert check_data("2026-02-13", weather)["ok"]
    weather["hourly"].loc[47, "h"] = 47
    result = check_data("2026-02-13", weather)
    assert not result["ok"]
    assert result["missing_hours"] == 1


@pytest.mark.parametrize(
    ("init_time", "expected_ok"),
    [
        ("2026-02-13T06:00:00Z", True),
        ("2026-02-13T11:00:00Z", True),
        ("2026-02-13T12:00:00Z", False),
        ("2026-02-13T18:00:00Z", False),
    ],
)
def test_check_data_enforces_run_publication_delay_for_archive_issues(
    init_time: str, expected_ok: bool
) -> None:
    issue_date = "2026-02-13"
    weather = {
        "hourly": pd.DataFrame(
            {
                "h": range(1, 49),
                "target_time_utc": target_times_utc(issue_date),
                "wind_100m_ms": [7.0] * 48,
                "temp_c": [-2.0] * 48,
                "init_time_utc": [init_time] * 48,
            }
        )
    }

    result = check_data(issue_date, weather)

    assert result["ok"] is expected_ok
    assert result["runs_before_issue"] is expected_ok
    if not expected_ok:
        assert "опубликованный позже момента выпуска" in result["notes"][0]


@pytest.mark.parametrize(
    ("init_time", "expected_ok"),
    [
        ("2026-02-13T06:00:00Z", True),
        ("2026-02-13T12:00:00Z", True),
        ("2026-02-13T15:00:00Z", True),
        ("2026-02-13T16:00:00Z", False),
    ],
)
def test_live_snapshot_must_not_postdate_issue_moment(
    init_time: str, expected_ok: bool
) -> None:
    issue_time = pd.Timestamp("2026-02-13T15:00:00Z")
    weather = {
        "issue_time_utc": issue_time,
        "hourly": pd.DataFrame(
            {
                "h": range(1, 49),
                "target_time_utc": live_times(issue_time.to_pydatetime())[1],
                "wind_100m_ms": [7.0] * 48,
                "temp_c": [-2.0] * 48,
                "init_time_utc": [init_time] * 48,
            }
        ),
    }

    result = check_data("live", weather)

    assert result["ok"] is expected_ok
    assert result["runs_before_issue"] is expected_ok


def test_real_raw_csv_build_reports_quality() -> None:
    report = build_hourly_dataset(offset_hours=5)
    hourly = load_hourly_dataset()
    quality = hourly_quality_summary(hourly).set_index("turbine")

    assert set(hourly.columns) == set(HOURLY_COLUMNS)
    assert report.rows_by_turbine["1"] > 20_000
    assert report.rows_by_turbine["2"] > 20_000
    assert 0.0 < quality.loc["1", "valid_fraction"] < 1.0
    assert 0.0 < quality.loc["2", "valid_fraction"] < 1.0
    blackout = hourly.loc[
        (hourly["turbine"] == "1")
        & (hourly["ts_utc"] >= pd.Timestamp("2024-05-17T19:00:00Z"))
        & (hourly["ts_utc"] <= pd.Timestamp("2024-07-17T18:00:00Z"))
    ]
    assert not blackout["valid"].any()


def test_live_snapshot_is_accepted_at_fetch_time():
    from datetime import datetime, timezone

    import pandas as pd

    from windcast import data
    from windcast.timeline import live_times

    now = datetime(2026, 9, 23, 11, 38, tzinfo=timezone.utc)
    _, targets = live_times(now)
    hourly = pd.DataFrame(
        {
            "h": range(1, 49),
            "target_time_utc": targets,
            "wind_100m_ms": [7.0] * 48,
            "wind_10m_ms": [5.0] * 48,
            "wind_dir_deg": [180.0] * 48,
            "temp_c": [3.0] * 48,
            "init_time_utc": [now] * 48,
        }
    )
    result = data.check_data("live", {"issue_time_utc": now, "hourly": hourly})
    assert result["runs_before_issue"] is True
