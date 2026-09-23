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
    hourly_quality_summary,
    load_hourly_dataset,
)


RAW_HEADER = (
    "ID,Статистическое время,Средняя скорость ветра(m/s),"
    "Нормализованная активная мощность,Средняя температура окружающей среды(°C)\n"
)


def _rows(hour: int, *, count: int = 6, wind: float = 7.0, power: float = 0.4) -> str:
    return "".join(
        f"{index},2024-05-18 {hour}:{index * 10:02}:00,{wind},{power},{12 + index}\n"
        for index in range(count)
    )


@pytest.fixture
def scada_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    # T1 has one complete hour, one incomplete hour, one idle hour, and the
    # documented blackout date. T2 proves that both source files are loaded.
    (raw / "turbine_1.csv").write_text(
        RAW_HEADER
        + _rows(0)
        + _rows(1, count=5)
        + _rows(2, wind=7.5, power=0.01),
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
    assert len(t1) == 3
    assert not t1["valid"].any(), "the fixture uses the documented T1 blackout"
    assert t1.loc[pd.Timestamp("2024-05-17T20:00:00Z"), "wind_ms"] == pytest.approx(7.0)
    assert t1.loc[pd.Timestamp("2024-05-17T20:00:00Z"), "power"] == pytest.approx(0.4)

    t2 = hourly.loc[hourly["turbine"] == "2"].set_index("ts_utc")
    assert len(t2) == 2
    assert t2["valid"].all()
    assert report.rows_by_turbine == {"1": 3, "2": 2}


def test_test_only_fastapi_harness_exposes_pipeline_quality(scada_root: Path) -> None:
    build_hourly_dataset(offset_hours=5)
    app = FastAPI()

    @app.get("/_test/hourly-quality")
    def hourly_quality() -> list[dict[str, object]]:
        return hourly_quality_summary(load_hourly_dataset()).to_dict(orient="records")

    response = TestClient(app).get("/_test/hourly-quality")

    assert response.status_code == 200
    assert response.json() == [
        {"turbine": "1", "hours": 3, "valid_hours": 0, "valid_fraction": 0.0},
        {"turbine": "2", "hours": 2, "valid_hours": 2, "valid_fraction": 1.0},
    ]


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
