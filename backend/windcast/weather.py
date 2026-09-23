"""API-first Open-Meteo Previous Runs weather with validated offline fallback."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

from windcast.paths import weather_cache_dir
from windcast.timeline import issue_time_utc, live_times, target_times_utc

PREVIOUS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
LIVE_URL = "https://api.open-meteo.com/v1/forecast"
LATITUDES = "43.645150,43.643198"
LONGITUDES = "78.535604,78.538828"
MODEL = "best_match"
BASE_FIELDS = (
    "wind_speed_100m",
    "wind_speed_10m",
    "wind_direction_100m",
    "temperature_2m",
)


class WeatherUnavailable(RuntimeError):
    """Raised only after both an operational request and valid cache fail."""


def previous_runs_url(start_date: str, end_date: str) -> str:
    hourly = ",".join(
        f"{field}_previous_day{lag}" for field in BASE_FIELDS for lag in (1, 2)
    )
    return (
        f"{PREVIOUS_URL}?latitude={LATITUDES}&longitude={LONGITUDES}&hourly={hourly}"
        f"&start_date={start_date}&end_date={end_date}&timezone=UTC&wind_speed_unit=ms"
    )


def _cache_path(name: str) -> Path:
    return weather_cache_dir() / name


def _validate(payload: object) -> dict:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("data"), list)
        or len(payload["data"]) != 2
    ):
        raise ValueError("Некорректный ответ Open-Meteo")
    parsed = urlparse(str(payload.get("url", "")))
    query = parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "previous-runs-api.open-meteo.com"
        or parsed.path != "/v1/forecast"
        or query.get("latitude") != [LATITUDES]
        or query.get("longitude") != [LONGITUDES]
        or query.get("timezone") != ["UTC"]
        or query.get("wind_speed_unit") != ["ms"]
        or query.get("models", [MODEL]) != [MODEL]
    ):
        raise ValueError("Кэш Open-Meteo не совпадает с заданным источником")
    for site in payload["data"]:
        hourly = site.get("hourly") if isinstance(site, dict) else None
        if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
            raise ValueError("В ответе Open-Meteo нет почасовых данных")  # noqa: TRY004
        size = len(hourly["time"])
        for field in BASE_FIELDS:
            for lag in (1, 2):
                values = hourly.get(f"{field}_previous_day{lag}")
                if (
                    not isinstance(values, list)
                    or len(values) != size
                    or any(
                        not isinstance(value, (int, float)) or not math.isfinite(value)
                        for value in values
                    )
                ):
                    raise ValueError("В кэше Open-Meteo есть неполные признаки")
    return payload


def _request_previous(start: str, end: str) -> dict:
    response = requests.get(previous_runs_url(start, end), timeout=20)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        payload = {"url": str(response.url), "data": payload}
    return _validate(payload)


def _cached_previous(targets: pd.DatetimeIndex) -> dict:
    candidates = [
        _cache_path(f"previous_{targets[0]:%Y-%m-%d}_{targets[-1]:%Y-%m-%d}.json"),
        _cache_path("previous_runs_bulk.json"),
    ]
    for path in candidates:
        try:
            payload = _validate(json.loads(path.read_text(encoding="utf-8")))
            available = [
                pd.to_datetime(site["hourly"]["time"], utc=True)
                for site in payload["data"]
            ]
            if all(
                index.is_unique and targets.isin(index).all() for index in available
            ):
                return payload
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    raise WeatherUnavailable("Нет валидного кэша Previous Runs для требуемого окна")


def _rows(payload: dict, targets: pd.DatetimeIndex, lags: list[int]) -> pd.DataFrame:
    records: list[dict] = []
    sites = payload["data"]
    indexes = [
        pd.Series(
            range(len(site["hourly"]["time"])),
            index=pd.to_datetime(site["hourly"]["time"], utc=True),
        )
        for site in sites
    ]
    for h, (target, lag) in enumerate(zip(targets, lags, strict=True), start=1):
        values: dict[str, float] = {}
        for source, output in (
            ("wind_speed_100m", "wind_100m_ms"),
            ("wind_speed_10m", "wind_10m_ms"),
            ("wind_direction_100m", "wind_dir_deg"),
            ("temperature_2m", "temp_c"),
        ):
            if any(not index.is_unique or target not in index for index in indexes):
                raise ValueError(
                    "Ответ Open-Meteo не покрывает требуемый почасовой горизонт"
                )
            values[output] = sum(
                float(
                    site["hourly"][f"{source}_previous_day{lag}"][
                        int(indexes[i][target])
                    ]
                )
                for i, site in enumerate(sites)
            ) / len(sites)
        records.append(
            {
                "h": h,
                "target_time_utc": target,
                **values,
                "init_time_utc": target - pd.Timedelta(days=lag),
            }
        )
    return pd.DataFrame(records)


def _runs(frame: pd.DataFrame) -> list[dict]:
    return [
        {
            "hours": "1-24",
            "model": MODEL,
            "init_utc": frame.iloc[:24]["init_time_utc"]
            .max()
            .isoformat()
            .replace("+00:00", "Z"),
            "provenance": "upper_bound",
        },
        {
            "hours": "25-48",
            "model": MODEL,
            "init_utc": frame.iloc[24:]["init_time_utc"]
            .max()
            .isoformat()
            .replace("+00:00", "Z"),
            "provenance": "upper_bound",
        },
    ]


def fetch_weather(issue_date: str, run: str = "latest") -> dict:
    if run not in {"latest", "previous"}:
        raise ValueError("run должен быть latest или previous")
    if issue_date == "live":
        if run == "previous":
            raise ValueError("Для live нет подтверждённого предыдущего снимка погоды")
        return _fetch_live()
    issue = issue_time_utc(issue_date)
    targets = pd.DatetimeIndex(target_times_utc(issue_date))
    try:
        payload = _request_previous(str(targets[0].date()), str(targets[-1].date()))
        lags = [2] * 48 if run == "previous" else [1] * 24 + [2] * 24
        hourly = _rows(payload, targets, lags)
        cache = _cache_path(
            f"previous_{targets[0]:%Y-%m-%d}_{targets[-1]:%Y-%m-%d}.json"
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload), encoding="utf-8")
        source = "api"
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        payload = _cached_previous(targets)
        source = "cache"
        lags = [2] * 48 if run == "previous" else [1] * 24 + [2] * 24
        hourly = _rows(payload, targets, lags)
    if not (hourly["init_time_utc"] <= issue).all():
        raise WeatherUnavailable("Прогон погоды новее момента выпуска")
    return {
        "issue_date": issue_date,
        "issue_time_utc": issue,
        "hourly": hourly,
        "runs": _runs(hourly),
        "source": source,
    }


def fetch_training_weather(start_utc: str, end_utc: str) -> pd.DataFrame:
    targets = pd.date_range(
        pd.Timestamp(start_utc), pd.Timestamp(end_utc), freq="h", tz="UTC"
    )
    payload = _cached_previous(targets)
    frames = []
    for lag in (1, 2):
        frame = _rows(payload, targets, [lag] * len(targets))
        frame["lag_days"] = lag
        frame["source"] = "cache"
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _validate_live(data: list[dict], issue_time: datetime) -> None:
    targets = pd.DatetimeIndex(live_times(issue_time)[1])
    if len(data) != 2:
        raise ValueError("Некорректный live-ответ Open-Meteo")
    for site in data:
        hourly = site.get("hourly") if isinstance(site, dict) else None
        if not isinstance(hourly, dict) or any(
            field not in hourly for field in (*BASE_FIELDS, "time")
        ):
            raise ValueError("Некорректный live-ответ Open-Meteo")
        times = pd.to_datetime(hourly["time"], utc=True, errors="coerce")
        if not times.is_unique or times.isna().any() or not targets.isin(times).all():
            raise ValueError("Live-ответ не покрывает следующие 48 часов")
        for field in BASE_FIELDS:
            values = hourly[field]
            if (
                not isinstance(values, list)
                or len(values) != len(times)
                or any(
                    not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in values
                )
            ):
                raise ValueError("Live-ответ содержит неполные признаки")


def _fetch_live() -> dict:
    cache = _cache_path("live_latest.json")
    issue_time: datetime
    try:
        response = requests.get(
            LIVE_URL,
            params={
                "latitude": LATITUDES,
                "longitude": LONGITUDES,
                "hourly": ",".join(BASE_FIELDS),
                "timezone": "UTC",
                "wind_speed_unit": "ms",
            },
            timeout=20,
        )
        response.raise_for_status()
        fetched_at = datetime.now(timezone.utc)
        issue_time = fetched_at
        payload = response.json()
        data = payload if isinstance(payload, list) else [payload]
        _validate_live(data, issue_time)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"fetched_at": fetched_at.isoformat(), "data": data}),
            encoding="utf-8",
        )
        source = "api"
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        try:
            stored = json.loads(cache.read_text(encoding="utf-8"))
            fetched_at = pd.to_datetime(stored["fetched_at"], utc=True).to_pydatetime()
            issue_time = datetime.now(timezone.utc)
            data = stored["data"]
            _validate_live(data, issue_time)
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise WeatherUnavailable("Нет валидного live-кэша Open-Meteo") from error
        source = "cache"
    targets = pd.DatetimeIndex(live_times(issue_time)[1])
    sites = [
        {
            "hourly": {
                "time": item["hourly"]["time"],
                **{
                    f"{field}_previous_day1": item["hourly"][field]
                    for field in BASE_FIELDS
                },
                **{
                    f"{field}_previous_day2": item["hourly"][field]
                    for field in BASE_FIELDS
                },
            }
        }
        for item in data[:2]
    ]
    try:
        hourly = _rows({"data": sites}, targets, [1] * 48)
    except (KeyError, ValueError) as error:
        raise WeatherUnavailable("Live-кэш не покрывает следующие 48 часов") from error
    hourly["init_time_utc"] = fetched_at
    return {
        "issue_date": "live",
        "issue_time_utc": issue_time,
        "hourly": hourly,
        "runs": _runs(hourly),
        "source": source,
    }


def fetch_historical_diagnostic(start_date: str, end_date: str) -> dict:
    """Fetch Historical Forecast only for diagnostics; callers must not model on it."""
    response = requests.get(
        "https://historical-forecast-api.open-meteo.com/v1/forecast",
        params={
            "latitude": LATITUDES,
            "longitude": LONGITUDES,
            "start_date": start_date,
            "end_date": end_date,
            "hourly": ",".join(BASE_FIELDS),
            "timezone": "UTC",
            "wind_speed_unit": "ms",
        },
        timeout=20,
    )
    response.raise_for_status()
    return {"source": "api", "kind": "diagnostic_only", "data": response.json()}
