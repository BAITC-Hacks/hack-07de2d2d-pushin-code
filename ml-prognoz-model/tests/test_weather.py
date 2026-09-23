from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from wind_forecast.weather import (
    CacheIntegrityError,
    DownloadPlan,
    GfsArchiveClient,
    GfsRun,
    RangeDownloadError,
    WeatherArchiveError,
    daily_origins,
    parse_gfs_index,
)

UTC = timezone.utc
RUN = datetime(2026, 1, 31, 18, tzinfo=UTC)


def _index_text(run: datetime = RUN, forecast_hour: int = 30) -> str:
    fields = [
        ("GUST", "surface"),
        ("TMP", "2 m above ground"),
        ("UGRD", "10 m above ground"),
        ("VGRD", "10 m above ground"),
        ("UGRD", "100 m above ground"),
        ("VGRD", "100 m above ground"),
        ("PRES", "surface"),
        ("END", "surface"),
    ]
    return (
        "\n".join(
            f"{i}:{i * 100}:d={run:%Y%m%d%H}:{variable}:{level}:{forecast_hour} hour fcst:"
            for i, (variable, level) in enumerate(fields, 1)
        )
        + "\n"
    )


class _Response:
    def __init__(
        self, body: bytes, status: int = 206, headers: dict[str, str] | None = None
    ) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.body

    def close(self) -> None:
        pass


def test_parse_gfs_index_and_find_seven_required_ranges() -> None:
    entries = parse_gfs_index(_index_text())
    assert entries[0].run_time == RUN
    selected = GfsArchiveClient.required_entries(entries)
    assert set(selected) == {
        "gust_speed",
        "temperature_2m",
        "wind_u_10m",
        "wind_v_10m",
        "wind_u_100m",
        "wind_v_100m",
        "surface_pressure",
    }
    assert selected["gust_speed"][0].offset == 100
    assert selected["gust_speed"][1] == 199


def test_index_requires_all_weather_fields() -> None:
    entries = parse_gfs_index(_index_text().replace("PRES:surface", "HGT:surface"))
    with pytest.raises(WeatherArchiveError, match="surface_pressure"):
        GfsArchiveClient.required_entries(entries)


def test_transient_open_retries_are_bounded_and_missing_objects_do_not_retry(tmp_path, monkeypatch):
    attempts = []
    pauses = []
    monkeypatch.setattr("wind_forecast.weather.sleep", pauses.append)
    response = _Response(b"ok")

    def transient_then_success(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise URLError(TimeoutError("TLS handshake timed out"))
        return response

    client = GfsArchiveClient(tmp_path, http_open=transient_then_success)
    assert client._open(Request("https://example.test/archive")) is response
    assert len(attempts) == 3
    assert pauses == [0.5, 1.0]

    attempts.clear()
    pauses.clear()

    def unavailable(request, timeout):
        attempts.append(request.full_url)
        raise HTTPError(request.full_url, 404, "Not Found", {}, None)

    client = GfsArchiveClient(tmp_path, http_open=unavailable)
    with pytest.raises(WeatherArchiveError, match="404"):
        client._open(Request("https://example.test/archive"))
    assert len(attempts) == 1
    assert pauses == []

    attempts.clear()

    def always_timeout(request, timeout):
        attempts.append(request.full_url)
        raise TimeoutError("TLS handshake timed out")

    client = GfsArchiveClient(tmp_path, http_open=always_timeout)
    with pytest.raises(WeatherArchiveError, match="timed out"):
        client._open(Request("https://example.test/archive"))
    assert len(attempts) == 3


def test_interrupted_body_retries_exact_range_and_closes_responses(tmp_path, monkeypatch):
    pauses = []
    monkeypatch.setattr("wind_forecast.weather.sleep", pauses.append)
    responses = []

    class InterruptedResponse(_Response):
        def __init__(self, interrupted):
            super().__init__(b"abc", headers={"Content-Range": "bytes 10-12/100"})
            self.interrupted = interrupted
            self.closed = False

        def read(self):
            if self.interrupted:
                raise TimeoutError("The read operation timed out")
            return super().read()

        def close(self):
            self.closed = True

    def open_response(request, timeout):
        assert request.get_header("Range") == "bytes=10-12"
        response = InterruptedResponse(interrupted=not responses)
        responses.append(response)
        return response

    client = GfsArchiveClient(tmp_path, http_open=open_response)
    payload, _ = client._range_get("https://example.test/archive", 10, 12)
    assert payload == b"abc"
    assert len(responses) == 2
    assert all(response.closed for response in responses)
    assert pauses == [0.5]


def test_index_selects_only_requested_instantaneous_step() -> None:
    lines = _index_text().splitlines()
    # A 6-hour maximum TMP shares the same variable/level but is not point TMP.
    lines.insert(2, "99:250:d=2026013118:TMP:2 m above ground:24-30 hour max fcst:")
    entries = parse_gfs_index("\n".join(lines))
    selected = GfsArchiveClient.required_entries(
        entries, expected_run_time=RUN, expected_file_lead=30
    )
    assert selected["temperature_2m"][0].forecast_description.startswith("30 hour fcst")


def test_index_rejects_wrong_run_or_forecast_step() -> None:
    entries = parse_gfs_index(_index_text())
    with pytest.raises(WeatherArchiveError, match="IDX run"):
        GfsArchiveClient.required_entries(
            entries, expected_run_time=RUN + timedelta(hours=6), expected_file_lead=30
        )
    with pytest.raises(WeatherArchiveError, match="temperature_2m"):
        GfsArchiveClient.required_entries(entries, expected_run_time=RUN, expected_file_lead=31)


def test_grib_metadata_accepts_eccodes_wind_alias_with_canonical_identity() -> None:
    class FakeEcCodes:
        @staticmethod
        def codes_get(_handle: object, key: str) -> object:
            values = {
                "shortName": "u",
                "typeOfLevel": "heightAboveGround",
                "level": 100,
                "units": "m s**-1",
                "discipline": 0,
                "parameterCategory": 2,
                "parameterNumber": 2,
                "stepType": "instant",
                "forecastTime": 24,
                "dataDate": 20260131,
                "dataTime": 1800,
                "validityDate": 20260201,
                "validityTime": 1800,
            }
            return values[key]

    GfsArchiveClient._validate_grib_metadata(FakeEcCodes(), object(), "wind_u_100m", RUN, 24)


def test_decode_uses_nearest_once_then_selected_elements_and_checks_grid_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEcCodes:
        signature_keys = {
            "gridType": "regular_ll",
            "numberOfDataPoints": 100,
            "Ni": 10,
            "Nj": 10,
            "latitudeOfFirstGridPointInDegrees": 90.0,
            "longitudeOfFirstGridPointInDegrees": 0.0,
            "latitudeOfLastGridPointInDegrees": -90.0,
            "longitudeOfLastGridPointInDegrees": 359.0,
            "iDirectionIncrementInDegrees": 1.0,
            "jDirectionIncrementInDegrees": 1.0,
            "iScansNegatively": 0,
            "jScansPositively": 1,
            "jPointsAreConsecutive": 0,
            "alternativeRowScanning": 0,
        }

        def __init__(self) -> None:
            self.nearest_calls = 0
            self.element_calls: list[tuple[str, tuple[int, ...]]] = []
            self.mismatched_grid = False

        @staticmethod
        def codes_new_from_message(message: bytes) -> str:
            return message.decode()

        @staticmethod
        def codes_release(_handle: str) -> None:
            pass

        def codes_get(self, handle: str, key: str):
            if key == "missingValue":
                return 9999.0
            if self.mismatched_grid and handle == "wind_u_10m" and key == "Ni":
                return 11
            return self.signature_keys[key]

        def codes_grib_find_nearest(
            self, _handle: str, latitude: float, _longitude: float, **_kwargs
        ):
            self.nearest_calls += 1
            index = 4 if latitude == 43.6 else 5
            return ({"index": index, "value": 273.15 + index},)

        def codes_get_elements(self, handle: str, _key: str, indexes: list[int]):
            self.element_calls.append((handle, tuple(indexes)))
            return [10.0 + index for index in indexes]

    fake = FakeEcCodes()
    monkeypatch.setitem(sys.modules, "eccodes", fake)
    messages = {"temperature_2m": b"temperature_2m", "wind_u_10m": b"wind_u_10m"}
    turbines = {"1": (43.6, 78.5), "2": (43.7, 78.6)}

    decoded = GfsArchiveClient._decode_messages(messages, turbines)

    assert decoded["1"] == {"temperature_2m": 4.0, "wind_u_10m": 14.0}
    assert decoded["2"] == {"temperature_2m": 5.0, "wind_u_10m": 15.0}
    assert fake.nearest_calls == 2
    assert fake.element_calls == [("wind_u_10m", (4, 5))]

    fake.mismatched_grid = True
    with pytest.raises(WeatherArchiveError, match="grid metadata differs"):
        GfsArchiveClient._decode_messages(messages, turbines)


def test_range_get_rejects_full_object_response(tmp_path: Path) -> None:
    def opener(_request: Any, timeout: int = 0) -> _Response:
        return _Response(b"x" * 10, status=200, headers={})

    client = GfsArchiveClient(tmp_path, http_open=opener)
    with pytest.raises(RangeDownloadError, match="non-exact"):
        client._range_get("https://example.invalid/file", 0, 9)


def test_range_get_enforces_exact_content_range(tmp_path: Path) -> None:
    def opener(_request: Any, timeout: int = 0) -> _Response:
        return _Response(b"x" * 10, headers={"Content-Range": "bytes 1-10/100"})

    with pytest.raises(RangeDownloadError, match="non-exact"):
        GfsArchiveClient(tmp_path, http_open=opener)._range_get(
            "https://example.invalid/file", 0, 9
        )


def test_select_run_uses_fixed_guard_and_last_modified(tmp_path: Path) -> None:
    origin = datetime(2026, 2, 1, 18, tzinfo=UTC)
    calls: list[str] = []

    def opener(request: Any, timeout: int = 0) -> _Response:
        calls.append(request.full_url)
        # Candidate 12Z is six hours old, and its archive timestamp is safe.
        assert "t12z" in request.full_url
        return _Response(
            b"",
            status=200,
            headers={"Last-Modified": format_datetime(datetime(2026, 1, 31, 23, tzinfo=UTC))},
        )

    selected = GfsArchiveClient(tmp_path, http_open=opener).select_run_for_origin(origin)
    assert selected.run_time == datetime(2026, 2, 1, 12, tzinfo=UTC)
    assert selected.available_at == datetime(2026, 2, 1, 18, tzinfo=UTC)
    assert len(calls) == 1


def test_select_run_steps_back_if_archive_timestamp_would_leak(tmp_path: Path) -> None:
    origin = datetime(2026, 2, 1, 18, tzinfo=UTC)

    def opener(request: Any, timeout: int = 0) -> _Response:
        if "t12z" in request.full_url:
            modified = datetime(2026, 2, 1, 19, tzinfo=UTC)
        else:
            assert "t06z" in request.full_url
            modified = datetime(2026, 2, 1, 12, tzinfo=UTC)
        return _Response(b"", status=200, headers={"Last-Modified": format_datetime(modified)})

    selected = GfsArchiveClient(tmp_path, http_open=opener).select_run_for_origin(origin)
    assert selected.run_time == datetime(2026, 2, 1, 6, tzinfo=UTC)


def test_preflight_only_fetches_headers_and_indices(tmp_path: Path) -> None:
    requests: list[tuple[str, str | None]] = []

    def opener(request: Any, timeout: int = 0) -> _Response:
        range_header = request.headers.get("Range")
        requests.append((request.full_url, range_header))
        if request.get_method() == "HEAD":
            return _Response(
                b"",
                status=200,
                headers={"Last-Modified": format_datetime(datetime(2026, 1, 31, 18, tzinfo=UTC))},
            )
        assert request.full_url.endswith(".idx")
        forecast_hour = int(request.full_url.rsplit(".f", 1)[1].split(".idx", 1)[0])
        body = _index_text(datetime(2026, 2, 1, 0, tzinfo=UTC), forecast_hour).encode()
        return _Response(body, headers={"Content-Range": f"bytes 0-{len(body) - 1}/{len(body)}"})

    client = GfsArchiveClient(tmp_path, http_open=opener)
    plan = client.preflight(datetime(2026, 2, 1, 6, tzinfo=UTC), lead_hours=[24, 25])
    assert plan.message_count == 14
    assert plan.estimated_bytes == 1400
    # The selected run's f030 metadata is reused instead of a second HEAD.
    assert len(requests) == 4
    assert all(url.endswith(".idx") or byte_range is None for url, byte_range in requests)


def test_daily_origins_are_inclusive_utc() -> None:
    origins = daily_origins(datetime(2026, 2, 1).date(), datetime(2026, 2, 2).date(), hour_utc=3)
    assert origins == [datetime(2026, 2, 1, 3, tzinfo=UTC), datetime(2026, 2, 2, 3, tzinfo=UTC)]


def test_replay_uses_run_relative_gfs_file_lead_and_resumes_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = datetime(2026, 2, 1, 18, tzinfo=UTC)
    run = GfsRun(
        datetime(2026, 2, 1, 12, tzinfo=UTC), origin, origin, "https://example.invalid/f024"
    )
    client = GfsArchiveClient(tmp_path)
    decoded = {
        "1": {
            "wind_u_10m": 3.0,
            "wind_v_10m": 4.0,
            "wind_u_100m": 5.0,
            "wind_v_100m": 12.0,
            "temperature_2m": 10.0,
            "surface_pressure": 90000.0,
            "gust_speed": 15.0,
        }
    }
    calls: list[int] = []
    monkeypatch.setattr(
        client,
        "preflight",
        lambda *args, **kwargs: DownloadPlan(origin, run.run_time, origin, (1, 48), 0, 0, (), ()),
    )
    monkeypatch.setattr(client, "select_run_for_origin", lambda *args, **kwargs: run)
    monkeypatch.setattr(
        client,
        "_run_metadata",
        lambda _run, lead: GfsRun(
            run.run_time, origin, origin, client.file_url(run.run_time, lead)
        ),
    )
    monkeypatch.setattr(
        client, "fetch_required_messages", lambda _run, lead: calls.append(lead) or {}
    )
    monkeypatch.setattr(client, "_decode_messages", lambda _messages, _turbines, **_kwargs: decoded)

    rows = client.replay_daily_forecasts([origin], turbines={"1": (43.6, 78.5)}, lead_hours=[1, 48])
    assert calls == [7, 54]
    assert [row.lead_hours for row in rows] == [1, 48]
    assert rows[0].wind_speed_10m == 5.0
    assert rows[0].wind_speed_100m == 13.0
    assert rows[0].weather_source.endswith("f007")
    assert rows[1].weather_source.endswith("f054")

    monkeypatch.setattr(
        client, "fetch_required_messages", lambda *_args: pytest.fail("cache should resume")
    )
    resumed = client.replay_daily_forecasts(
        [origin], turbines={"1": (43.6, 78.5)}, lead_hours=[1, 48]
    )
    assert resumed == rows

    cache_path, _ = client._cache_paths(origin, run.run_time, {"1": (43.6, 78.5)}, (1, 48), "csv")
    cache_path.write_text(cache_path.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
    with pytest.raises(CacheIntegrityError, match="SHA-256"):
        client.replay_daily_forecasts([origin], turbines={"1": (43.6, 78.5)}, lead_hours=[1, 48])


def test_replay_validates_model_horizon_guard(tmp_path: Path) -> None:
    client = GfsArchiveClient(tmp_path)
    with pytest.raises(ValueError, match="1..48"):
        client.replay_daily_forecasts([RUN], lead_hours=[0])
    with pytest.raises(ValueError, match="1..48"):
        client.preflight(RUN, lead_hours=[49])


def test_parallel_replay_preflights_all_origins_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origins = [
        datetime(2026, 2, 1, 18, tzinfo=UTC),
        datetime(2026, 2, 2, 18, tzinfo=UTC),
    ]
    client = GfsArchiveClient(tmp_path)
    preflighted: list[datetime] = []
    fetched: list[int] = []
    completed: list[datetime] = []

    def preflight(origin: datetime, **_kwargs: Any) -> DownloadPlan:
        preflighted.append(origin)
        run = origin - timedelta(hours=6)
        return DownloadPlan(origin, run, origin, (1,), 7, 10, (), ())

    def metadata(run: datetime, lead: int) -> GfsRun:
        return GfsRun(run, run + timedelta(hours=6), run, client.file_url(run, lead))

    decoded = {
        "1": {
            "wind_u_10m": 3.0,
            "wind_v_10m": 4.0,
            "wind_u_100m": 5.0,
            "wind_v_100m": 12.0,
            "temperature_2m": 10.0,
            "surface_pressure": 90000.0,
            "gust_speed": 15.0,
        }
    }
    monkeypatch.setattr(client, "preflight", preflight)
    monkeypatch.setattr(client, "_run_metadata", metadata)
    monkeypatch.setattr(
        client, "fetch_required_messages", lambda _run, lead: fetched.append(lead) or {}
    )
    monkeypatch.setattr(client, "_decode_messages", lambda _messages, _sites, **_kwargs: decoded)
    rows = client.replay_daily_forecasts(
        origins,
        turbines={"1": (43.6, 78.5)},
        lead_hours=[1],
        max_workers=2,
        progress_callback=completed.append,
    )
    assert len(preflighted) == 2
    assert len(rows) == 2 and len(fetched) == 2
    assert set(completed) == set(origins)
