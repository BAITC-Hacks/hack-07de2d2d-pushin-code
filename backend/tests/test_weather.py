from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from windcast import weather
from windcast.timeline import issue_time_utc, published_before_issue


def _payload(start: str = "2026-02-13T19:00") -> dict:
    times = (
        pd.date_range(start, periods=48, freq="h", tz="UTC")
        .strftime("%Y-%m-%dT%H:%M")
        .tolist()
    )
    hourly = {"time": times}
    for name, base in (
        ("wind_speed_100m", 7.0),
        ("wind_speed_10m", 4.0),
        ("wind_direction_100m", 180.0),
        ("temperature_2m", -2.0),
    ):
        for lag in weather.ISSUE_LAGS:
            hourly[f"{name}_previous_day{lag}"] = [base + lag] * 48
    site = {
        "latitude": 43.62,
        "longitude": 78.47,
        "hourly_units": {"wind_speed_100m_previous_day1": "m/s"},
        "hourly": hourly,
    }
    return {
        "url": weather.previous_runs_url("2026-02-13", "2026-02-15"),
        "data": [site, site],
    }


@pytest.fixture
def weather_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("WINDCAST_ROOT", str(tmp_path))
    return tmp_path


def test_previous_lags_bounds_and_cache_fallback(
    weather_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload()
    monkeypatch.setattr(
        weather.requests, "get", lambda *args, **kwargs: _Response(payload)
    )
    latest = weather.fetch_weather("2026-02-13")
    assert latest["source"] == "api" and len(latest["hourly"]) == 48
    issue = issue_time_utc("2026-02-13")
    # contract v0.4 §2: h 17 still uses previous_day1 (run D 06:00Z), h 18 already day2
    assert latest["hourly"].loc[16, "init_time_utc"] == issue - pd.Timedelta(hours=13)
    assert latest["hourly"].loc[17, "init_time_utc"] == issue - pd.Timedelta(hours=31)
    assert all(
        published_before_issue(init.to_pydatetime(), "2026-02-13")
        for init in latest["hourly"]["init_time_utc"]
    )
    monkeypatch.setattr(weather.requests, "get", _offline)
    cached = weather.fetch_weather("2026-02-13")
    assert cached["source"] == "cache"
    pd.testing.assert_frame_equal(latest["hourly"], cached["hourly"])


def test_previous_keeps_three_run_groups_and_rejects_bad_input(
    weather_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        weather.requests, "get", lambda *args, **kwargs: _Response(_payload())
    )
    previous = weather.fetch_weather("2026-02-13", run="previous")
    assert [run["hours"] for run in previous["runs"]] == ["1-17", "18-41", "42-48"]
    latest = weather.fetch_weather("2026-02-13", run="latest")
    assert [run["hours"] for run in latest["runs"]] == ["1-17", "18-41", "42-48"]
    assert all(
        a["init_utc"] > b["init_utc"]
        for a, b in zip(latest["runs"], previous["runs"], strict=True)
    )
    with pytest.raises(ValueError):
        weather.fetch_weather("13.02.2026")
    with pytest.raises(ValueError):
        weather.fetch_weather("2026-02-13", run="bad")


def test_truncated_api_never_overwrites_valid_cache(
    weather_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _payload()
    monkeypatch.setattr(
        weather.requests, "get", lambda *args, **kwargs: _Response(good)
    )
    cached = weather.fetch_weather("2026-02-13")
    bad = _payload()
    for site in bad["data"]:
        for key, values in site["hourly"].items():
            if isinstance(values, list):
                site["hourly"][key] = values[:20]
    monkeypatch.setattr(weather.requests, "get", lambda *args, **kwargs: _Response(bad))
    recovered = weather.fetch_weather("2026-02-13")
    assert recovered["source"] == "cache"
    pd.testing.assert_frame_equal(cached["hourly"], recovered["hourly"])


def test_live_offline_without_snapshot_is_domain_error(
    weather_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(weather.requests, "get", _offline)
    with pytest.raises(weather.WeatherUnavailable):
        weather.fetch_weather("live")


def test_malformed_live_api_never_writes_snapshot(
    weather_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda *args, **kwargs: _Response([{"hourly": {}}, {"hourly": {}}]),
    )
    with pytest.raises(weather.WeatherUnavailable):
        weather.fetch_weather("live")
    assert not (weather_root / "data" / "weather_cache" / "live_latest.json").exists()


def test_test_only_fastapi_adapter_and_corrupt_cache(
    weather_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        weather.requests, "get", lambda *args, **kwargs: _Response(_payload())
    )
    app = FastAPI()

    @app.get("/_test/weather")
    def get_weather() -> dict:
        data = weather.fetch_weather("2026-02-13")
        return {"source": data["source"], "rows": len(data["hourly"])}

    assert TestClient(app).get("/_test/weather").json() == {"source": "api", "rows": 48}
    for path in (weather_root / "data" / "weather_cache").glob("*.json"):
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(weather.requests, "get", _offline)
    with pytest.raises(weather.WeatherUnavailable):
        weather.fetch_weather("2026-02-13")


def test_training_helper_uses_both_lags_from_valid_bulk(weather_root: Path) -> None:
    cache = weather_root / "data" / "weather_cache"
    cache.mkdir(parents=True)
    (cache / "previous_runs_bulk.json").write_text(
        json.dumps(_payload()), encoding="utf-8"
    )
    training = weather.fetch_training_weather(
        "2026-02-13T19:00:00Z", "2026-02-14T18:00:00Z"
    )
    assert len(training) == 48
    assert set(training["lag_days"]) == {1, 2}
    assert set(training["source"]) == {"cache"}


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def _offline(*args: object, **kwargs: object) -> None:
    raise weather.requests.RequestException("offline")
