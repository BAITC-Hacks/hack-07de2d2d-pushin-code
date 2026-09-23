"""scripts/score.py on tiny synthetic files: organizer SCADA format, tidy mode, time offset."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "score.py"
HEADER = (
    "ID,Статистическое время,Средняя скорость ветра(m/s),"
    "Нормализованная активная мощность,Средняя температура окружающей среды(°C)"
)
FORECAST_HEADER = (
    "target_time_utc,target_time_local,turbine,p10,p50,p90,issue_date,horizon_h"
)


def _scada(path: Path, hours: dict[str, tuple[float, float, int]]) -> None:
    """hours: raw local 'YYYY-MM-DD HH' -> (wind, power, number of 10-minute rows)."""
    lines, n = [HEADER], 0
    for hour, (wind, power, rows) in hours.items():
        day, hh = hour.split()
        for minute in range(0, rows * 10, 10):
            n += 1
            lines.append(f"{n},{day} {int(hh)}:{minute:02d}:00,{wind},{power},-5.0")
    path.write_bytes(("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8"))


def _forecast(
    path: Path, rows: list[tuple[str, str, float, float, float, int]]
) -> None:
    lines = [FORECAST_HEADER]
    for utc, turbine, p10, p50, p90, h in rows:
        lines.append(f"{utc},,{turbine},{p10},{p50},{p90},2026-01-31,{h}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(*args: str) -> dict:
    out = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), *args, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


@pytest.fixture()
def files(tmp_path):
    # Raw time is UTC+6: raw 01:00 -> 19:00Z previous day -> target 00:00+05:00.
    _scada(
        tmp_path / "turbine_1.csv",
        {
            "2026-02-01 01": (8.0, 0.50, 6),
            "2026-02-01 02": (8.0, 0.40, 4),  # 4 of 6 rows: kept
            "2026-02-01 03": (8.0, 0.40, 3),  # 3 of 6 rows: dropped (incomplete)
            "2026-02-01 04": (9.0, 0.00, 6),  # zero power at 9 m/s: dropped (downtime)
        },
    )
    _scada(
        tmp_path / "turbine_2.csv",
        {"2026-02-01 01": (8.0, 0.30, 6), "2026-02-01 02": (8.0, 0.20, 6)},
    )
    _forecast(
        tmp_path / "fc.csv",
        [
            ("2026-01-31T19:00:00Z", "1", 0.3, 0.60, 0.7, 1),  # |0.60-0.50| = 0.10
            ("2026-01-31T20:00:00Z", "1", 0.3, 0.30, 0.5, 2),  # |0.30-0.40| = 0.10
            ("2026-01-31T21:00:00Z", "1", 0.3, 0.30, 0.5, 3),  # no actual
            ("2026-01-31T22:00:00Z", "1", 0.3, 0.30, 0.5, 4),  # downtime
            ("2026-01-31T19:00:00Z", "plant", 0.1, 0.20, 0.3, 1),  # actual 0.40 -> 0.20
            (
                "2026-01-31T20:00:00Z",
                "plant",
                0.1,
                0.30,
                0.4,
                30,
            ),  # actual 0.30 -> 0.00
        ],
    )
    return tmp_path


def test_organizer_format_known_nmae(files):
    result = _run("--actual", str(files), "--forecast", str(files / "fc.csv"))
    t1 = result["by_turbine"]["1"]
    assert t1["hours"] == 2 and t1["nmae"] == pytest.approx(0.10)
    assert t1["coverage_p10_p90"] == pytest.approx(
        1.0
    )  # 0.50 in [0.3, 0.7], 0.40 in [0.3, 0.5]
    plant = result["by_turbine"]["plant"]
    assert plant["hours"] == 2 and plant["nmae"] == pytest.approx(0.10)
    assert plant["bias"] == pytest.approx(-0.10)
    assert result["by_horizon"]["h25-48"]["plant"]["nmae"] == pytest.approx(0.0)
    assert result["actual"]["dropped_hours"]["1"] == {"incomplete": 1, "downtime": 1}


def test_explicit_files_equal_directory(files):
    by_dir = _run("--actual", str(files), "--forecast", str(files / "fc.csv"))
    by_files = _run(
        "--actual",
        str(files / "turbine_1.csv"),
        str(files / "turbine_2.csv"),
        "--forecast",
        str(files / "fc.csv"),
    )
    assert by_dir["by_turbine"] == by_files["by_turbine"]


def test_offset_shifts_hours(files):
    result = _run(
        "--actual",
        str(files),
        "--forecast",
        str(files / "fc.csv"),
        "--scada-utc-offset",
        "5",
    )
    # With UTC+5 every actual moves one hour later; turbine 1 now matches 20:00Z (0.50)
    # and 21:00Z (0.40): |0.30-0.50| and |0.30-0.40|.
    t1 = result["by_turbine"]["1"]
    assert t1["hours"] == 2 and t1["nmae"] == pytest.approx(0.15)


def test_tidy_mode_local_time(tmp_path):
    (tmp_path / "actual.csv").write_text(
        "target_time_local,turbine,actual\n"
        "2026-02-01T00:00+05:00,plant,0.5\n"
        "2026-02-01T01:00+05:00,plant,0.1\n",
        encoding="utf-8",
    )
    _forecast(
        tmp_path / "fc.csv",
        [
            ("2026-01-31T19:00:00Z", "plant", 0.1, 0.3, 0.9, 1),
            ("2026-01-31T20:00:00Z", "plant", 0.2, 0.4, 0.9, 2),
        ],
    )
    result = _run(
        "--actual", str(tmp_path / "actual.csv"), "--forecast", str(tmp_path / "fc.csv")
    )
    plant = result["by_turbine"]["plant"]
    assert plant["hours"] == 2
    assert plant["nmae"] == pytest.approx(0.25)
    assert plant["nrmse"] == pytest.approx(((0.04 + 0.09) / 2) ** 0.5)
    assert plant["coverage_p10_p90"] == pytest.approx(0.5)
    # pinball p50 = 0.5 * mean |e| = 0.125
    assert plant["pinball"]["p50"] == pytest.approx(0.125)
