import json
from pathlib import Path

import pytest

from wind_forecast.config import daily_origins, load_config, utc_timestamp


def test_default_config_resolves_dataset_paths():
    path = Path(__file__).parents[1] / "configs/default.json"
    config = load_config(path)
    assert config.root == path.parent.parent
    assert config.values["data"]["timezone"] == "Etc/GMT-5"
    assert len(config.values["weather"]["sites"]) == 2


def test_replay_daily_origins_cover_28_issuances():
    origins = daily_origins("2026-01-31T18:00:00Z", "2026-02-27T18:00:00Z")
    assert len(origins) == 28


@pytest.mark.parametrize("value", ["2026-01-31 18:00", "2026-01-31T18:30:00Z", "NaT"])
def test_configuration_rejects_ambiguous_or_unaligned_origin(value):
    with pytest.raises(ValueError):
        utc_timestamp(value)


def test_configuration_rejects_missing_turbine_weather_site(tmp_path):
    config = json.loads((Path(__file__).parents[1] / "configs/default.json").read_text())
    config["weather"]["sites"].pop()
    location = tmp_path / "config.json"
    location.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="must match"):
        load_config(location)


def test_daily_origins_do_not_silently_truncate_end():
    with pytest.raises(ValueError, match="same UTC hour"):
        daily_origins("2026-01-31T18:00:00Z", "2026-02-27T19:00:00Z")
