import pandas as pd
import pytest

from wind_forecast.io import file_sha256, read_table, write_table


def test_weather_csv_roundtrip_preserves_id_and_utc(tmp_path):
    frame = pd.DataFrame(
        {
            "turbine_id": ["1"],
            "target_time": pd.to_datetime(["2026-01-31T19:00:00Z"]),
            "wind_speed_100m": [7.5],
        }
    )
    location = tmp_path / "weather.csv"
    write_table(frame, location)
    result = read_table(location)
    assert result.turbine_id.tolist() == ["1"]
    assert result.target_time.iloc[0] == frame.target_time.iloc[0]
    assert len(file_sha256(location)) == 64


def test_csv_with_naive_time_is_rejected(tmp_path):
    location = tmp_path / "weather.csv"
    location.write_text("turbine_id,target_time\n1,2026-01-31 19:00:00\n")
    with pytest.raises(ValueError, match="explicit timezone"):
        read_table(location)
