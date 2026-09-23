"""Leakage-safe replay of archived NOAA GFS weather forecasts.

The module deliberately works from *forecast runs*, never from a reanalysis or
an "as of now" weather API.  It downloads only the messages selected in the
GFS ``.idx`` sidecar using verified HTTP byte ranges.  Decoding is optional at
install time: install the ``eccodes`` Python bindings (and NumPy) before
calling :meth:`GfsArchiveClient.replay_daily_forecasts`.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from threading import Lock
from time import sleep
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UTC = timezone.utc
DEFAULT_BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
DEFAULT_MODEL = "gfs.0p25"
DEFAULT_AVAILABILITY_DELAY = timedelta(hours=6)
DEFAULT_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # two GiB per invocation
_ECCODES_DECODE_LOCK = Lock()

# The public coordinates supplied with the challenge.  Values are named so
# callers can replace them without needing to know the internal representation.
DEFAULT_TURBINES: dict[str, tuple[float, float]] = {
    "1": (43.645150, 78.535604),
    "2": (43.643198, 78.538828),
}


class WeatherArchiveError(RuntimeError):
    """Base error for a GFS archive or decoding failure."""


class WeatherAvailabilityError(WeatherArchiveError):
    """No archived run can be proven available at the simulated origin."""


class RangeDownloadError(WeatherArchiveError):
    """A server response could have downloaded more than a selected range."""


class CacheIntegrityError(WeatherArchiveError):
    """A cache object or its manifest is incomplete, stale, or has been altered."""


@dataclass(frozen=True)
class GfsIndexEntry:
    """One line of a GFS ``.idx`` inventory."""

    message_number: int
    offset: int
    run_time: datetime
    variable: str
    level: str
    forecast_description: str


@dataclass(frozen=True)
class GfsRun:
    """A run eligible to supply weather at a particular simulated origin."""

    run_time: datetime
    available_at: datetime
    file_last_modified: datetime | None
    source_url: str
    etag: str | None = None


@dataclass(frozen=True)
class WeatherRecord:
    """One turbine/hour weather feature row used by the forecasting model."""

    turbine_id: str
    forecast_origin: datetime
    target_time: datetime
    weather_run_time: datetime
    weather_available_at: datetime
    lead_hours: int
    wind_speed_10m: float
    wind_speed_100m: float
    wind_u_100m: float
    wind_v_100m: float
    temperature_2m: float
    surface_pressure: float
    gust_speed: float
    weather_model: str
    weather_source: str


@dataclass(frozen=True)
class DownloadPlan:
    """A no-download estimate for one origin and its requested lead times."""

    forecast_origin: datetime
    run_time: datetime
    weather_available_at: datetime
    lead_hours: tuple[int, ...]
    message_count: int
    estimated_bytes: int
    source_urls: tuple[str, ...]
    cached_leads: tuple[int, ...]


_INDEX_RE = re.compile(
    r"^(?P<number>\d+):(?P<offset>\d+):d=(?P<run>\d{10}):"
    r"(?P<variable>[^:]+):(?P<level>[^:]+):(?P<forecast>.*)$"
)
_INSTANT_FORECAST_RE = re.compile(r"^(?P<hour>\d+) hour fcst$")
_REQUIRED_FIELDS: Mapping[str, tuple[str, str]] = {
    "gust_speed": ("GUST", "surface"),
    "temperature_2m": ("TMP", "2 m above ground"),
    "wind_u_10m": ("UGRD", "10 m above ground"),
    "wind_v_10m": ("VGRD", "10 m above ground"),
    "wind_u_100m": ("UGRD", "100 m above ground"),
    "wind_v_100m": ("VGRD", "100 m above ground"),
    "surface_pressure": ("PRES", "surface"),
}


def _instantaneous_forecast_hour(description: str) -> int | None:
    """Return a point-forecast hour; reject analysis and min/max intervals."""
    match = _INSTANT_FORECAST_RE.fullmatch(description.strip().rstrip(":"))
    return int(match.group("hour")) if match else None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware UTC datetimes")
    return value.astimezone(UTC).replace(microsecond=0)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def parse_gfs_index(text: str) -> list[GfsIndexEntry]:
    """Parse an IDX sidecar and fail if it is malformed or unordered."""
    entries: list[GfsIndexEntry] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _INDEX_RE.match(line)
        if not match:
            raise WeatherArchiveError(f"unrecognised GFS IDX line: {line[:120]!r}")
        groups = match.groupdict()
        entries.append(
            GfsIndexEntry(
                message_number=int(groups["number"]),
                offset=int(groups["offset"]),
                run_time=datetime.strptime(groups["run"], "%Y%m%d%H").replace(tzinfo=UTC),
                variable=groups["variable"],
                level=groups["level"],
                forecast_description=groups["forecast"],
            )
        )
    if not entries:
        raise WeatherArchiveError("GFS IDX response was empty")
    if any(right.offset <= left.offset for left, right in zip(entries, entries[1:])):
        raise WeatherArchiveError("GFS IDX offsets are not strictly increasing")
    return entries


def gfs_file_url(run_time: datetime, lead_hours: int, bucket: str = DEFAULT_BUCKET) -> str:
    """Return the pgrb2.0p25 object URL for a run and integer lead hour."""
    run_time = _utc(run_time)
    if run_time.hour not in (0, 6, 12, 18):
        raise ValueError("GFS run hour must be one of 00, 06, 12, or 18 UTC")
    if not 0 <= lead_hours <= 384:
        raise ValueError("GFS lead_hours must be between 0 and 384")
    return (
        f"{bucket.rstrip('/')}/gfs.{run_time:%Y%m%d}/{run_time:%H}/atmos/"
        f"gfs.t{run_time:%H}z.pgrb2.0p25.f{lead_hours:03d}"
    )


def _floor_to_cycle(value: datetime) -> datetime:
    value = _utc(value)
    return value.replace(hour=(value.hour // 6) * 6, minute=0, second=0)


def _safe_filename(value: datetime) -> str:
    return _utc(value).strftime("%Y%m%dT%HZ")


def _forecast_leads(values: Iterable[int]) -> tuple[int, ...]:
    """Validate the project direct-output horizon rather than silently truncating."""
    leads: set[int] = set()
    for value in values:
        try:
            lead = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("lead_hours must contain integer hours") from exc
        if lead != value or not 1 <= lead <= 48:
            raise ValueError("lead_hours must contain only integer hours in 1..48")
        leads.add(lead)
    if not leads:
        raise ValueError("at least one lead hour is required")
    return tuple(sorted(leads))


class GfsArchiveClient:
    """Read archived GFS messages safely and create cached feature tables.

    ``http_open`` exists primarily for deterministic offline tests.  It has the
    same signature as ``urllib.request.urlopen``.  Production callers normally
    leave it as ``None``.
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        bucket: str = DEFAULT_BUCKET,
        model: str = DEFAULT_MODEL,
        availability_delay: timedelta = DEFAULT_AVAILABILITY_DELAY,
        timeout_seconds: int = 30,
        message_workers: int = 7,
        http_open: Callable[..., Any] | None = None,
    ) -> None:
        if availability_delay < timedelta(hours=6):
            raise ValueError("availability_delay must be at least six hours to avoid leakage")
        if not isinstance(message_workers, int) or not 1 <= message_workers <= 7:
            raise ValueError("message_workers must be an integer in 1..7")
        self.cache_dir = Path(cache_dir)
        self.bucket = bucket.rstrip("/")
        self.model = model
        self.availability_delay = availability_delay
        self.timeout_seconds = timeout_seconds
        self.message_workers = message_workers
        self._http_open = http_open or urlopen
        self.last_estimated_bytes = 0
        self._metadata_lock = Lock()
        self._run_metadata_cache: dict[tuple[datetime, int], GfsRun] = {}
        self._index_cache: dict[tuple[datetime, int], tuple[GfsIndexEntry, ...]] = {}

    def file_url(self, run_time: datetime, lead_hours: int) -> str:
        return gfs_file_url(run_time, lead_hours, self.bucket)

    def _open(self, request: Request):
        # Large archive sweeps occasionally encounter transient TLS handshakes
        # or throttling. Retry only transport/transient HTTP failures, never
        # a missing object or a failed range/provenance validation.
        for attempt in range(3):
            try:
                try:
                    return self._http_open(request, timeout=self.timeout_seconds)
                except TypeError:  # small fake openers in downstream tests
                    return self._http_open(request)
            except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
                retryable = not isinstance(exc, HTTPError) or exc.code in (
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                )
                if not retryable or attempt == 2:
                    raise WeatherArchiveError(
                        f"GFS request failed: {request.full_url}: {exc}"
                    ) from exc
                sleep(0.5 * 2**attempt)

    @staticmethod
    def _headers(response: Any) -> Mapping[str, str]:
        headers = response.headers
        if hasattr(headers, "items"):
            return {str(key).lower(): str(value) for key, value in headers.items()}
        return {str(key).lower(): str(value) for key, value in dict(headers).items()}

    def _range_get(self, url: str, start: int, end: int) -> tuple[bytes, Mapping[str, str]]:
        return self._retry_body_download(lambda: self._range_get_once(url, start, end))

    @staticmethod
    def _retry_body_download(operation: Callable[[], Any]):
        """Restart an interrupted selected-range transfer, never an invalid reply."""
        for attempt in range(3):
            try:
                return operation()
            except (TimeoutError, ConnectionError, IncompleteRead, URLError) as exc:
                if attempt == 2:
                    raise WeatherArchiveError(
                        f"GFS body transfer failed after three attempts: {exc}"
                    ) from exc
                sleep(0.5 * 2**attempt)

    def _range_get_once(self, url: str, start: int, end: int) -> tuple[bytes, Mapping[str, str]]:
        """Fetch exactly ``start..end`` and reject an unsafe full-object reply."""
        if start < 0 or end < start:
            raise ValueError("invalid inclusive byte range")
        request = Request(url, headers={"Range": f"bytes={start}-{end}"})
        response = self._open(request)
        try:
            status = getattr(response, "status", response.getcode())
            headers = self._headers(response)
            content_range = headers.get("content-range")
            expected = f"bytes {start}-{end}/"
            if status != 206 or not content_range or not content_range.startswith(expected):
                raise RangeDownloadError(
                    f"refusing non-exact range response for {url}: status={status}, "
                    f"Content-Range={content_range!r}"
                )
            payload = response.read()
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        if len(payload) != end - start + 1:
            raise RangeDownloadError(
                f"range response length {len(payload)} does not match requested {end - start + 1}"
            )
        return payload, headers

    def _idx_get(self, url: str) -> bytes:
        return self._retry_body_download(lambda: self._idx_get_once(url))

    def _idx_get_once(self, url: str) -> bytes:
        """Fetch an IDX sidecar with a capped range and validate S3's response.

        S3 correctly shortens an end offset beyond EOF.  That behaviour is safe
        for an IDX (unlike a GRIB message), so this accepts an EOF-capped range
        while retaining the mandatory 206/Content-Range validation.
        """
        maximum_end = 1_048_575
        request = Request(url, headers={"Range": f"bytes=0-{maximum_end}"})
        response = self._open(request)
        try:
            status = getattr(response, "status", response.getcode())
            headers = self._headers(response)
            content_range = headers.get("content-range", "")
            match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
            if status != 206 or not match:
                raise RangeDownloadError(
                    f"refusing invalid IDX range response for {url}: {content_range!r}"
                )
            actual_end, total = (int(item) for item in match.groups())
            if total > maximum_end + 1 or actual_end + 1 != total:
                raise RangeDownloadError(f"IDX exceeds one MiB safety limit: {content_range!r}")
            payload = response.read()
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        if len(payload) != actual_end + 1:
            raise RangeDownloadError("IDX body length does not match Content-Range")
        return payload

    def _head(self, url: str) -> Mapping[str, str]:
        request = Request(url, method="HEAD")
        response = self._open(request)
        try:
            status = getattr(response, "status", response.getcode())
            if not 200 <= status < 300:
                raise WeatherArchiveError(f"GFS HEAD returned HTTP {status} for {url}")
            return self._headers(response)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()

    @staticmethod
    def _last_modified(headers: Mapping[str, str]) -> datetime | None:
        value = headers.get("last-modified")
        if not value:
            return None
        return parsedate_to_datetime(value).astimezone(UTC).replace(microsecond=0)

    def _run_metadata(self, run_time: datetime, lead_hours: int = 24) -> GfsRun:
        run_time = _utc(run_time)
        key = (run_time, lead_hours)
        with self._metadata_lock:
            cached = self._run_metadata_cache.get(key)
        if cached is not None:
            return cached
        url = self.file_url(run_time, lead_hours)
        headers = self._head(url)
        modified = self._last_modified(headers)
        result = GfsRun(
            run_time=run_time,
            available_at=max(run_time + self.availability_delay, modified or run_time),
            file_last_modified=modified,
            source_url=url,
            etag=headers.get("etag"),
        )
        with self._metadata_lock:
            return self._run_metadata_cache.setdefault(key, result)

    def select_run_for_origin(
        self,
        forecast_origin: datetime,
        *,
        target_horizons: Iterable[int] | None = None,
        max_back_cycles: int = 8,
    ) -> GfsRun:
        """Select newest run demonstrably available before ``forecast_origin``.

        A run must pass both the fixed publication guard (six hours by default)
        and, where S3 provides it, the object ``Last-Modified`` time.  Missing
        objects are skipped so archive gaps never cause a newer forecast to be
        substituted silently.
        """
        origin = _utc(forecast_origin)
        horizons = _forecast_leads(target_horizons) if target_horizons is not None else ()
        candidate = _floor_to_cycle(origin - self.availability_delay)
        failures: list[str] = []
        for _ in range(max_back_cycles + 1):
            try:
                file_leads = (
                    tuple(
                        int((origin + timedelta(hours=horizon) - candidate).total_seconds() // 3600)
                        for horizon in horizons
                    )
                    if horizons
                    else (24,)
                )
                files = [self._run_metadata(candidate, file_lead) for file_lead in file_leads]
                run = GfsRun(
                    run_time=candidate,
                    available_at=max(file.available_at for file in files),
                    file_last_modified=max(
                        (file.file_last_modified for file in files if file.file_last_modified),
                        default=None,
                    ),
                    source_url=files[0].source_url,
                    etag=files[0].etag,
                )
            except WeatherArchiveError as exc:
                failures.append(str(exc))
                candidate -= timedelta(hours=6)
                continue
            if run.available_at <= origin:
                return run
            failures.append(f"{_stamp(candidate)} available at {_stamp(run.available_at)}")
            candidate -= timedelta(hours=6)
        detail = "; ".join(failures[-3:])
        raise WeatherAvailabilityError(
            f"no GFS run proven available at {_stamp(origin)} "
            f"after {max_back_cycles + 1} cycles: {detail}"
        )

    def fetch_index(self, run_time: datetime, lead_hours: int) -> list[GfsIndexEntry]:
        """Fetch a small IDX sidecar by range; never use it as a GRIB proxy."""
        run_time = _utc(run_time)
        key = (run_time, lead_hours)
        with self._metadata_lock:
            cached = self._index_cache.get(key)
        if cached is not None:
            return list(cached)
        url = self.file_url(run_time, lead_hours) + ".idx"
        # IDX files are normally tens of KiB.  A one-MiB cap catches an unexpected
        # server/object change while still fitting all known pgrb2 inventories.
        parsed = tuple(parse_gfs_index(self._idx_get(url).decode("utf-8")))
        with self._metadata_lock:
            cached = self._index_cache.setdefault(key, parsed)
        return list(cached)

    @staticmethod
    def required_entries(
        entries: Sequence[GfsIndexEntry],
        *,
        expected_run_time: datetime | None = None,
        expected_file_lead: int | None = None,
    ) -> dict[str, tuple[GfsIndexEntry, int]]:
        """Locate required GFS messages and their inclusive range endpoints.

        ``TMP`` has both instantaneous and min/max interval records at some
        forecast hours.  Callers that know the expected run/hour must pass
        them, so interval summaries cannot be mistaken for the instantaneous
        2-m temperature requested by this project.
        """
        if (expected_run_time is None) != (expected_file_lead is None):
            raise ValueError("expected_run_time and expected_file_lead must be supplied together")
        expected_run = _utc(expected_run_time) if expected_run_time is not None else None
        if expected_file_lead is not None and not 0 <= expected_file_lead <= 384:
            raise ValueError("expected_file_lead must be between 0 and 384")
        located: dict[str, tuple[GfsIndexEntry, int]] = {}
        for index, entry in enumerate(entries):
            for name, (variable, level) in _REQUIRED_FIELDS.items():
                if entry.variable == variable and entry.level == level:
                    if expected_run is not None:
                        if entry.run_time != expected_run:
                            raise WeatherArchiveError(
                                f"IDX run {entry.run_time.isoformat()} does not match requested "
                                f"{expected_run.isoformat()}"
                            )
                        if (
                            _instantaneous_forecast_hour(entry.forecast_description)
                            != expected_file_lead
                        ):
                            continue
                    if name in located:
                        raise WeatherArchiveError(f"ambiguous IDX messages for {name}")
                    if index + 1 >= len(entries):
                        raise WeatherArchiveError(f"cannot determine final byte for {name}")
                    located[name] = (entry, entries[index + 1].offset - 1)
        missing = sorted(set(_REQUIRED_FIELDS) - set(located))
        if missing:
            raise WeatherArchiveError(f"GFS IDX missing required fields: {', '.join(missing)}")
        return located

    def fetch_required_messages(self, run_time: datetime, lead_hours: int) -> dict[str, bytes]:
        """Download only seven selected GRIB messages, verifying every range."""
        entries = self.fetch_index(run_time, lead_hours)
        selected = self.required_entries(
            entries, expected_run_time=run_time, expected_file_lead=lead_hours
        )
        url = self.file_url(run_time, lead_hours)

        def fetch_one(item: tuple[str, tuple[GfsIndexEntry, int]]) -> tuple[str, bytes]:
            name, (entry, end) = item
            payload, _ = self._range_get(url, entry.offset, end)
            return name, payload

        # Seven independent selected ranges share neither bytes nor decoder state.
        # The outer origin pool is capped at eight, so this bounds network fan-out
        # to 56 requests while avoiding a seven-round-trip serial wait per file.
        with ThreadPoolExecutor(
            max_workers=min(self.message_workers, len(selected)), thread_name_prefix="gfs-range"
        ) as executor:
            return dict(executor.map(fetch_one, selected.items()))

    @staticmethod
    def _decode_messages(
        messages: Mapping[str, bytes],
        turbines: Mapping[str, tuple[float, float]],
        *,
        expected_run_time: datetime | None = None,
        expected_file_lead: int | None = None,
    ) -> dict[str, dict[str, float]]:
        """Decode nearest GFS grid values for each turbine using ecCodes.

        Values are converted here so downstream feature code never needs GRIB
        metadata: TMP K -> C; pressure remains Pa; wind and gust remain m/s.
        """
        try:
            import eccodes  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise WeatherArchiveError(
                "GRIB decoding requires eccodes and numpy; install project weather dependencies"
            ) from exc

        result = {str(turbine_id): {} for turbine_id in turbines}
        nearest: dict[str, int] | None = None
        grid_signature: tuple[Any, ...] | None = None
        if (expected_run_time is None) != (expected_file_lead is None):
            raise ValueError("expected_run_time and expected_file_lead must be supplied together")
        for field, message in messages.items():
            handle = eccodes.codes_new_from_message(message)
            if handle is None:
                raise WeatherArchiveError(f"ecCodes could not read GFS message {field}")
            try:
                if expected_run_time is not None and expected_file_lead is not None:
                    GfsArchiveClient._validate_grib_metadata(
                        eccodes, handle, field, expected_run_time, expected_file_lead
                    )
                signature = GfsArchiveClient._grid_signature(eccodes, handle, field)
                if grid_signature is None:
                    grid_signature = signature
                elif signature != grid_signature:
                    raise WeatherArchiveError(
                        f"GRIB {field} grid metadata differs from the first selected message"
                    )
                missing_value = float(eccodes.codes_get(handle, "missingValue"))
                if nearest is None:
                    nearest = {}
                    selected_values: list[float] = []
                    for turbine_id, (latitude, longitude) in turbines.items():
                        try:
                            points = eccodes.codes_grib_find_nearest(
                                handle, latitude, longitude % 360, npoints=1
                            )
                            point = points[0]
                            if len(points) != 1:
                                raise ValueError(f"expected one point, got {len(points)}")
                            grid_index = int(point["index"])
                            value = float(point["value"])
                        except (IndexError, KeyError, TypeError, ValueError) as exc:
                            raise WeatherArchiveError(
                                "ecCodes could not find one nearest grid point for turbine "
                                f"{turbine_id}"
                            ) from exc
                        nearest[str(turbine_id)] = grid_index
                        selected_values.append(value)
                else:
                    ordered_turbines = list(nearest)
                    try:
                        selected_values = [
                            float(value)
                            for value in eccodes.codes_get_elements(
                                handle,
                                "values",
                                [nearest[turbine_id] for turbine_id in ordered_turbines],
                            )
                        ]
                    except Exception as exc:
                        raise WeatherArchiveError(
                            f"ecCodes could not read selected grid values for {field}"
                        ) from exc
                    if len(selected_values) != len(ordered_turbines):
                        raise WeatherArchiveError(
                            f"ecCodes returned {len(selected_values)} values for "
                            f"{len(ordered_turbines)} turbine sites"
                        )
                for turbine_id, value in zip(nearest, selected_values):
                    if not math.isfinite(value) or math.isclose(value, missing_value):
                        raise WeatherArchiveError(
                            f"GFS message {field} contains a missing/non-finite value "
                            f"at turbine {turbine_id}"
                        )
                    result[turbine_id][field] = value
            finally:
                eccodes.codes_release(handle)
        for values in result.values():
            values["temperature_2m"] -= 273.15
            if not all(math.isfinite(value) for value in values.values()):
                raise WeatherArchiveError("GFS message contains a non-finite nearest-grid value")
        return result

    @staticmethod
    def _grid_signature(eccodes: Any, handle: Any, field: str) -> tuple[Any, ...]:
        """Return the grid properties that make nearest-point indices portable.

        A GRIB index is meaningful only for the same grid/scanning layout. This
        is checked for all seven independently ranged messages before reusing
        the first message's ecCodes nearest-point indices.
        """

        keys = (
            "gridType",
            "numberOfDataPoints",
            "Ni",
            "Nj",
            "latitudeOfFirstGridPointInDegrees",
            "longitudeOfFirstGridPointInDegrees",
            "latitudeOfLastGridPointInDegrees",
            "longitudeOfLastGridPointInDegrees",
            "iDirectionIncrementInDegrees",
            "jDirectionIncrementInDegrees",
            "iScansNegatively",
            "jScansPositively",
            "jPointsAreConsecutive",
            "alternativeRowScanning",
        )
        try:
            return tuple(eccodes.codes_get(handle, key) for key in keys)
        except Exception as exc:
            raise WeatherArchiveError(f"GRIB {field} is missing required grid metadata") from exc

    @staticmethod
    def _validate_grib_metadata(
        eccodes: Any,
        handle: Any,
        field: str,
        run_time: datetime,
        file_lead: int,
    ) -> None:
        """Reject a byte-range message whose GRIB identity is not requested one."""
        expected = {
            # ecCodes 2.49 normalizes height-based wind shortName to u/v;
            # discipline/category/number plus level are the canonical identity.
            "gust_speed": ({"gust"}, "surface", 0, "m s**-1", 2, 22),
            "temperature_2m": ({"2t", "t"}, "heightAboveGround", 2, "K", 0, 0),
            "wind_u_10m": ({"10u", "u"}, "heightAboveGround", 10, "m s**-1", 2, 2),
            "wind_v_10m": ({"10v", "v"}, "heightAboveGround", 10, "m s**-1", 2, 3),
            "wind_u_100m": ({"100u", "u"}, "heightAboveGround", 100, "m s**-1", 2, 2),
            "wind_v_100m": ({"100v", "v"}, "heightAboveGround", 100, "m s**-1", 2, 3),
            "surface_pressure": ({"sp"}, "surface", 0, "Pa", 3, 0),
        }
        try:
            short_names, type_of_level, level, units, category, number = expected[field]
        except KeyError as exc:
            raise WeatherArchiveError(f"unexpected decoded GFS field {field}") from exc
        required = {
            "typeOfLevel": type_of_level,
            "level": level,
            "units": units,
            "discipline": 0,
            "parameterCategory": category,
            "parameterNumber": number,
            "stepType": "instant",
            "forecastTime": file_lead,
            "dataDate": int(_utc(run_time).strftime("%Y%m%d")),
            "dataTime": int(_utc(run_time).strftime("%H%M")),
            "validityDate": int((run_time + timedelta(hours=file_lead)).strftime("%Y%m%d")),
            "validityTime": int((run_time + timedelta(hours=file_lead)).strftime("%H%M")),
        }
        for key, wanted in required.items():
            try:
                actual = eccodes.codes_get(handle, key)
            except Exception as exc:
                raise WeatherArchiveError(f"GRIB {field} is missing metadata key {key}") from exc
            if actual != wanted:
                raise WeatherArchiveError(
                    f"GRIB {field} metadata mismatch for {key}: got {actual!r}, expected {wanted!r}"
                )
        actual_short_name = eccodes.codes_get(handle, "shortName")
        if actual_short_name not in short_names:
            raise WeatherArchiveError(
                f"GRIB {field} metadata mismatch for shortName: got {actual_short_name!r}, "
                f"expected one of {sorted(short_names)!r}"
            )

    def _cache_paths(
        self,
        forecast_origin: datetime,
        run_time: datetime,
        turbines: Mapping[str, tuple[float, float]],
        lead_hours: Sequence[int],
        cache_format: str,
    ) -> tuple[Path, Path]:
        # The digest makes coordinate, source/run and horizon changes cache
        # misses instead of accidental reuse of an incompatible feature table.
        import hashlib

        identity = json.dumps(
            {
                "bucket": self.bucket,
                "model": self.model,
                "availability_delay_seconds": int(self.availability_delay.total_seconds()),
                "run": _stamp(run_time),
                "origin": _stamp(forecast_origin),
                "sites": sorted((str(key), *value) for key, value in turbines.items()),
                "lead_hours": list(lead_hours),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()[:16]
        root = self.cache_dir / "gfs" / f"run={_safe_filename(run_time)}" / f"key={digest}"
        return root / f"weather.{cache_format}", root / "metadata.json"

    @staticmethod
    def _record_row(record: WeatherRecord) -> dict[str, str | int | float]:
        row = asdict(record)
        for key in ("forecast_origin", "target_time", "weather_run_time", "weather_available_at"):
            row[key] = _stamp(row[key])  # type: ignore[arg-type]
        return row

    @staticmethod
    def _record_from_mapping(row: Mapping[str, Any]) -> WeatherRecord:
        return WeatherRecord(
            turbine_id=str(row["turbine_id"]),
            forecast_origin=_parse_stamp(str(row["forecast_origin"])),
            target_time=_parse_stamp(str(row["target_time"])),
            weather_run_time=_parse_stamp(str(row["weather_run_time"])),
            weather_available_at=_parse_stamp(str(row["weather_available_at"])),
            lead_hours=int(row["lead_hours"]),
            wind_speed_10m=float(row["wind_speed_10m"]),
            wind_speed_100m=float(row["wind_speed_100m"]),
            wind_u_100m=float(row["wind_u_100m"]),
            wind_v_100m=float(row["wind_v_100m"]),
            temperature_2m=float(row["temperature_2m"]),
            surface_pressure=float(row["surface_pressure"]),
            gust_speed=float(row["gust_speed"]),
            weather_model=str(row["weather_model"]),
            weather_source=str(row["weather_source"]),
        )

    @classmethod
    def _records_from_csv(cls, path: Path) -> list[WeatherRecord]:
        records: list[WeatherRecord] = []
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                records.append(cls._record_from_mapping(row))
        return records

    def _records_from_cache(self, path: Path, cache_format: str) -> list[WeatherRecord]:
        if cache_format == "csv":
            return self._records_from_csv(path)
        if cache_format == "parquet":
            try:
                import pandas as pd  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise WeatherArchiveError("reading parquet cache requires pandas") from exc
            return [
                self._record_from_mapping(row) for row in pd.read_parquet(path).to_dict("records")
            ]
        raise ValueError("cache_format must be 'csv' or 'parquet'")

    @staticmethod
    def _file_sha256(path: Path) -> str:
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _load_cache(
        self,
        path: Path,
        metadata_path: Path,
        *,
        forecast_origin: datetime,
        run_time: datetime,
        turbines: Mapping[str, tuple[float, float]],
        lead_hours: Sequence[int],
        cache_format: str,
    ) -> list[WeatherRecord] | None:
        """Read a verified complete *or partial* origin cache, never bare CSV."""
        if not path.exists() and not metadata_path.exists():
            return None
        if not path.exists() or not metadata_path.exists():
            raise CacheIntegrityError(
                "weather cache and metadata sidecar must either both exist or neither"
            )
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheIntegrityError(
                f"weather cache metadata is unreadable: {metadata_path}"
            ) from exc
        expected_turbines = {str(key): list(value) for key, value in turbines.items()}
        expected = {
            "cache_version": 2,
            "cache_format": cache_format,
            "bucket": self.bucket,
            "weather_model": self.model,
            "forecast_origin": _stamp(forecast_origin),
            "weather_run_time": _stamp(run_time),
            "availability_delay_seconds": int(self.availability_delay.total_seconds()),
            "turbines": expected_turbines,
            "requested_lead_hours": list(lead_hours),
        }
        for key, wanted in expected.items():
            if metadata.get(key) != wanted:
                raise CacheIntegrityError(
                    f"weather cache metadata mismatch for {key}; do not reuse this cache"
                )
        if metadata.get("cache_sha256") != self._file_sha256(path):
            raise CacheIntegrityError(
                "weather cache SHA-256 does not match metadata; cache may be altered"
            )
        records = self._records_from_cache(path, cache_format)
        completed = sorted({record.lead_hours for record in records})
        if metadata.get("completed_lead_hours") != completed:
            raise CacheIntegrityError("weather cache completed_lead_hours does not match its rows")
        requested = set(lead_hours)
        turbines_expected = {str(key) for key in turbines}
        for lead in completed:
            lead_rows = [record for record in records if record.lead_hours == lead]
            if {record.turbine_id for record in lead_rows} != turbines_expected or len(
                lead_rows
            ) != len(turbines_expected):
                raise CacheIntegrityError(
                    "weather cache has incomplete or duplicate turbine rows for a lead"
                )
        for record in records:
            if record.lead_hours not in requested:
                raise CacheIntegrityError("weather cache contains a lead outside this request")
            if record.forecast_origin != _utc(forecast_origin) or record.weather_run_time != _utc(
                run_time
            ):
                raise CacheIntegrityError(
                    "weather cache row provenance does not match its metadata"
                )
            if record.target_time != record.forecast_origin + timedelta(hours=record.lead_hours):
                raise CacheIntegrityError("weather cache row target time does not match its lead")
            numbers = (
                record.wind_speed_10m,
                record.wind_speed_100m,
                record.wind_u_100m,
                record.wind_v_100m,
                record.temperature_2m,
                record.surface_pressure,
                record.gust_speed,
            )
            if not all(math.isfinite(value) for value in numbers):
                raise CacheIntegrityError("weather cache contains a non-finite weather value")
        return records

    def _write_cache(
        self,
        records: Sequence[WeatherRecord],
        metadata: Mapping[str, Any],
        forecast_origin: datetime,
        cache_format: str,
        requested_lead_hours: Sequence[int],
    ) -> Path:
        run_time = records[0].weather_run_time
        turbines = metadata["turbines"]
        coordinates = {
            str(key): (float(value[0]), float(value[1])) for key, value in turbines.items()
        }
        path, metadata_path = self._cache_paths(
            forecast_origin, run_time, coordinates, requested_lead_hours, cache_format
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.stem + ".tmp" + path.suffix)
        if cache_format == "csv":
            rows = [self._record_row(record) for record in records]
            with temporary.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        elif cache_format == "parquet":
            try:
                import pandas as pd  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise WeatherArchiveError(
                    "Parquet cache requires pandas and a parquet engine"
                ) from exc
            pd.DataFrame([self._record_row(record) for record in records]).to_parquet(
                temporary, index=False
            )
        else:
            raise ValueError("cache_format must be 'csv' or 'parquet'")
        os.replace(temporary, path)
        manifest = dict(metadata)
        manifest.update(
            {
                "cache_version": 2,
                "cache_format": cache_format,
                "bucket": self.bucket,
                "availability_delay_seconds": int(self.availability_delay.total_seconds()),
                "requested_lead_hours": list(requested_lead_hours),
                "completed_lead_hours": sorted({record.lead_hours for record in records}),
                "cache_sha256": self._file_sha256(path),
            }
        )
        metadata_temporary = metadata_path.with_suffix(".json.tmp")
        metadata_temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(metadata_temporary, metadata_path)
        return path

    def preflight(
        self,
        forecast_origin: datetime,
        *,
        lead_hours: Iterable[int] = range(1, 49),
        turbines: Mapping[str, tuple[float, float]] = DEFAULT_TURBINES,
        cache_format: str = "csv",
        max_download_bytes: int | None = DEFAULT_MAX_DOWNLOAD_BYTES,
    ) -> DownloadPlan:
        """Estimate selected-byte transfer.  This downloads only IDX sidecars."""
        origin = _utc(forecast_origin)
        leads = _forecast_leads(lead_hours)
        run = self.select_run_for_origin(origin, target_horizons=leads)
        cached: list[int] = []
        estimated = 0
        urls: list[str] = []
        cache_path, metadata_path = self._cache_paths(
            origin, run.run_time, turbines, leads, cache_format
        )
        cached_records = self._load_cache(
            cache_path,
            metadata_path,
            forecast_origin=origin,
            run_time=run.run_time,
            turbines=turbines,
            lead_hours=leads,
            cache_format=cache_format,
        )
        cached_set = {record.lead_hours for record in cached_records or []}
        for lead in leads:
            if lead in cached_set:
                cached.append(lead)
                continue
            file_lead = int((origin + timedelta(hours=lead) - run.run_time).total_seconds() // 3600)
            file_run = self._run_metadata(run.run_time, file_lead)
            if file_run.available_at > origin:
                raise WeatherAvailabilityError(
                    f"{file_run.source_url} was not available by {_stamp(origin)} "
                    f"(available {_stamp(file_run.available_at)})"
                )
            entries = self.fetch_index(run.run_time, file_lead)
            for entry, end in self.required_entries(
                entries, expected_run_time=run.run_time, expected_file_lead=file_lead
            ).values():
                estimated += end - entry.offset + 1
            urls.append(self.file_url(run.run_time, file_lead))
        plan = DownloadPlan(
            origin,
            run.run_time,
            run.available_at,
            leads,
            7 * (len(leads) - len(cached)),
            estimated,
            tuple(urls),
            tuple(cached),
        )
        if max_download_bytes is not None and estimated > max_download_bytes:
            raise WeatherArchiveError(
                f"preflight estimates {estimated:,} bytes for {_stamp(origin)}, "
                f"above configured budget {max_download_bytes:,}; use a smaller date range, "
                "cache, or explicitly raise the budget"
            )
        return plan

    def replay_daily_forecasts(
        self,
        forecast_origins: Iterable[datetime],
        *,
        turbines: Mapping[str, tuple[float, float]] = DEFAULT_TURBINES,
        lead_hours: Iterable[int] = range(1, 49),
        cache_format: str = "csv",
        dry_run: bool = False,
        max_download_bytes: int | None = DEFAULT_MAX_DOWNLOAD_BYTES,
        max_workers: int = 1,
        progress_callback: Callable[[datetime], None] | None = None,
        _precomputed_plans: Sequence[DownloadPlan] | None = None,
    ) -> list[WeatherRecord] | list[DownloadPlan]:
        """Replay daily origin-relative forecast horizons with cache resume.

        In ``dry_run`` mode no GRIB messages are fetched or decoded; only small
        IDX sidecars and object headers are read to return transfer estimates.
        """
        origins = [_utc(origin) for origin in forecast_origins]
        if not isinstance(max_workers, int) or not 1 <= max_workers <= 8:
            raise ValueError("max_workers must be an integer in 1..8")
        if not origins:
            return []
        if dry_run:
            plans = [
                self.preflight(
                    origin,
                    lead_hours=lead_hours,
                    turbines=turbines,
                    cache_format=cache_format,
                    max_download_bytes=max_download_bytes,
                )
                for origin in origins
            ]
            self.last_estimated_bytes = sum(plan.estimated_bytes for plan in plans)
            return plans
        all_records: list[WeatherRecord] = []
        leads = _forecast_leads(lead_hours)
        # The per-origin preflight is also the transfer budget gate.  We do it
        # before any GRIB body request, so a multi-day replay cannot quietly
        # turn into a multi-hundred-GB transfer.
        if _precomputed_plans is None:

            def preflight_one(origin: datetime) -> DownloadPlan:
                return self.preflight(
                    origin,
                    lead_hours=leads,
                    turbines=turbines,
                    cache_format=cache_format,
                    max_download_bytes=None,
                )

            if max_workers > 1 and len(origins) > 1:
                with ThreadPoolExecutor(
                    max_workers=max_workers, thread_name_prefix="gfs-preflight"
                ) as executor:
                    plans = list(executor.map(preflight_one, origins))
            else:
                plans = [preflight_one(origin) for origin in origins]
        else:
            plans = list(_precomputed_plans)
            if len(plans) != len(origins):
                raise ValueError("_precomputed_plans must match forecast_origins")
        total_estimated = sum(plan.estimated_bytes for plan in plans)
        self.last_estimated_bytes = total_estimated
        if max_download_bytes is not None and total_estimated > max_download_bytes:
            raise WeatherArchiveError(
                f"preflight estimates {total_estimated:,} bytes across {len(origins)} origins, "
                f"above configured budget {max_download_bytes:,}; use a smaller range/cache "
                "or explicitly raise the budget"
            )
        if max_workers > 1 and len(origins) > 1:

            def replay_one(pair: tuple[datetime, DownloadPlan]) -> list[WeatherRecord]:
                origin, plan = pair
                result = self.replay_daily_forecasts(
                    [origin],
                    turbines=turbines,
                    lead_hours=leads,
                    cache_format=cache_format,
                    max_download_bytes=None,
                    max_workers=1,
                    progress_callback=progress_callback,
                    _precomputed_plans=(plan,),
                )
                return list(result)  # result is WeatherRecord list when dry_run is false

            with ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="gfs-fetch"
            ) as executor:
                output = [
                    record
                    for batch in executor.map(replay_one, zip(origins, plans))
                    for record in batch
                ]
            self.last_estimated_bytes = total_estimated
            return output
        for origin, plan in zip(origins, plans):
            # The all-origin preflight already selected and proved this run. Reusing
            # it avoids a second header sweep before parallel byte-range fetches.
            run = GfsRun(
                plan.run_time,
                plan.weather_available_at,
                None,
                self.file_url(plan.run_time, 0),
            )
            cache_path, metadata_path = self._cache_paths(
                origin, run.run_time, turbines, leads, cache_format
            )
            records = (
                self._load_cache(
                    cache_path,
                    metadata_path,
                    forecast_origin=origin,
                    run_time=run.run_time,
                    turbines=turbines,
                    lead_hours=leads,
                    cache_format=cache_format,
                )
                or []
            )
            completed_leads = {record.lead_hours for record in records}
            source_urls = sorted({record.weather_source for record in records})
            for lead in leads:
                if lead in completed_leads:
                    continue
                target_time = origin + timedelta(hours=lead)
                file_lead = int((target_time - run.run_time).total_seconds() // 3600)
                # Check every requested forecast-hour object.  Later lead files
                # can be posted after the run's f024 object; using one would leak.
                file_run = self._run_metadata(run.run_time, file_lead)
                if file_run.available_at > origin:
                    raise WeatherAvailabilityError(
                        f"{file_run.source_url} was not available by {_stamp(origin)} "
                        f"(available {_stamp(file_run.available_at)})"
                    )
                messages = self.fetch_required_messages(run.run_time, file_lead)
                # ecCodes Python bindings are not documented as thread-safe.
                # Only decoding is serialized; byte-range network I/O overlaps.
                with _ECCODES_DECODE_LOCK:
                    values_by_turbine = self._decode_messages(
                        messages,
                        turbines,
                        expected_run_time=run.run_time,
                        expected_file_lead=file_lead,
                    )
                source_url = file_run.source_url
                source_urls.append(source_url)
                for turbine_id, values in values_by_turbine.items():
                    records.append(
                        WeatherRecord(
                            turbine_id=str(turbine_id),
                            forecast_origin=origin,
                            target_time=target_time,
                            weather_run_time=run.run_time,
                            weather_available_at=file_run.available_at,
                            lead_hours=lead,
                            wind_speed_10m=math.hypot(values["wind_u_10m"], values["wind_v_10m"]),
                            wind_speed_100m=math.hypot(
                                values["wind_u_100m"], values["wind_v_100m"]
                            ),
                            wind_u_100m=values["wind_u_100m"],
                            wind_v_100m=values["wind_v_100m"],
                            temperature_2m=values["temperature_2m"],
                            surface_pressure=values["surface_pressure"],
                            gust_speed=values["gust_speed"],
                            weather_model=self.model,
                            weather_source=source_url,
                        )
                    )
                completed_leads.add(lead)
                records.sort(key=lambda row: (row.target_time, row.turbine_id))
                # Commit after each completed lead.  An interrupted origin can
                # resume only its missing lead files on the next invocation.
                self._write_cache(
                    records,
                    {
                        "forecast_origin": _stamp(origin),
                        "weather_run_time": _stamp(run.run_time),
                        "weather_available_at": _stamp(run.available_at),
                        "weather_model": self.model,
                        "weather_source_urls": sorted(set(source_urls)),
                        "availability_delay_hours": self.availability_delay.total_seconds() / 3600,
                        "schema": list(self._record_row(records[0]).keys()),
                        "turbines": {str(key): list(value) for key, value in turbines.items()},
                    },
                    origin,
                    cache_format,
                    leads,
                )
            all_records.extend(records)
            if progress_callback is not None:
                progress_callback(origin)
        return all_records


def daily_origins(start: date, end: date, *, hour_utc: int = 0) -> list[datetime]:
    """Return inclusive daily UTC origins; typically the February 2026 replay."""
    if not 0 <= hour_utc <= 23:
        raise ValueError("hour_utc must be in 0..23")
    if end < start:
        raise ValueError("end must not precede start")
    count = (end - start).days + 1
    return [
        datetime.combine(start + timedelta(days=offset), datetime.min.time(), UTC).replace(
            hour=hour_utc
        )
        for offset in range(count)
    ]
