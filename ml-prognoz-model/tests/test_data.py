from __future__ import annotations

import pandas as pd
import pytest

from wind_forecast.data import RAW_COLUMNS, read_observations


def _write_raw(path, rows):
    pd.DataFrame(
        {
            "ID": range(1, len(rows) + 1),
            RAW_COLUMNS["timestamp"]: [row[0] for row in rows],
            RAW_COLUMNS["wind"]: [row[1] for row in rows],
            RAW_COLUMNS["power"]: [row[2] for row in rows],
            RAW_COLUMNS["temperature"]: [row[3] for row in rows],
        }
    ).to_csv(path, index=False)


def test_only_complete_six_slot_hours_become_labels(tmp_path):
    rows = [(f"2025-01-01 00:{minute:02d}:00", 5.0, 0.5, 10.0) for minute in range(0, 60, 10)] + [
        (f"2025-01-01 01:{minute:02d}:00", 6.0, 0.4, 9.0) for minute in range(0, 50, 10)
    ]
    path = tmp_path / "one.csv"
    _write_raw(path, rows)

    hourly, report = read_observations({"1": path}, timezone="UTC")

    assert list(hourly["n_samples"]) == [6]
    assert hourly.iloc[0]["power"] == pytest.approx(0.5)
    assert str(hourly.iloc[0]["target_time"]) == "2025-01-01 00:00:00+00:00"
    assert report.partial_hours.iloc[0]["missing_slots"] == 1
    assert report.summary.iloc[0]["complete_hours"] == 1


def test_invalid_values_and_duplicate_slots_are_not_silently_averaged(tmp_path):
    rows = [(f"2025-01-01 00:{minute:02d}:00", 5.0, 0.5, 10.0) for minute in range(0, 60, 10)]
    rows[1] = ("2025-01-01 00:10:00", 5.0, 1.2, 10.0)
    rows.append(("2025-01-01 00:20:00", 5.0, 0.5, 10.0))
    path = tmp_path / "one.csv"
    _write_raw(path, rows)

    hourly, report = read_observations({"1": path}, timezone="UTC")

    assert hourly.empty
    assert set(report.invalid_rows["reason"]) == {"duplicate_timestamp", "invalid_measurement"}
    assert report.partial_hours.iloc[0]["n_samples"] == 4


def test_ambiguous_local_time_requires_an_explicit_policy(tmp_path):
    # Asia/Almaty repeated 23:xx on the 2024 offset transition.
    path = tmp_path / "one.csv"
    _write_raw(path, [("2024-02-29 23:00:00", 5.0, 0.5, 10.0)])

    with pytest.raises(ValueError, match="Cannot localize raw observation timestamps"):
        read_observations({"1": path})
