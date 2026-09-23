"""Small explicit JSON configuration; paths are relative to the project root."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    values: dict[str, Any]

    def path(self, value: str) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()


def utc_timestamp(value: str, name: str = "timestamp") -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError(f"{name} must have an explicit timezone, for example 2026-01-31T18:00:00Z")
    timestamp = timestamp.tz_convert("UTC")
    if timestamp != timestamp.floor("h"):
        raise ValueError(f"{name} must be aligned to a whole hour")
    return timestamp


def daily_origins(start: str, end: str) -> pd.DatetimeIndex:
    """Return inclusive daily issue times, without silently dropping an end time."""
    first, last = utc_timestamp(start, "start_origin"), utc_timestamp(end, "end_origin")
    if first > last:
        raise ValueError("start_origin must not be after end_origin")
    if first.hour != last.hour:
        raise ValueError("start_origin and end_origin must use the same UTC hour")
    return pd.date_range(first, last, freq="D")


def load_config(path: str | Path) -> ProjectConfig:
    location = Path(path).resolve()
    values = json.loads(location.read_text(encoding="utf-8"))
    for section in ("data", "weather", "evaluation", "replay"):
        if not isinstance(values.get(section), dict):
            raise ValueError(f"Configuration requires a {section!r} object")
    root = (location.parent / values.get("project_root", "..")).resolve()
    data, weather = values["data"], values["weather"]
    if not data.get("timezone"):
        raise ValueError("An explicit data.timezone is required; the CSV timezone is undocumented")
    if not isinstance(data.get("inputs"), dict) or not data["inputs"]:
        raise ValueError("data.inputs must map turbine IDs to CSV paths")
    if weather.get("source") != "noaa_gfs":
        raise ValueError("The supported replay weather source is noaa_gfs")
    if weather.get("horizon_hours") not in (24, 48):
        raise ValueError("weather.horizon_hours must be 24 or 48")
    if weather.get("publication_delay_hours", 0) < 6:
        raise ValueError("Use at least a 6-hour weather publication allowance")
    if values["evaluation"].get("n_jobs", 0) < 1:
        raise ValueError("evaluation.n_jobs must be positive")
    sites = weather.get("sites", [])
    ids = [str(site["turbine_id"]) for site in sites]
    if set(ids) != set(data["inputs"]) or len(ids) != len(set(ids)):
        raise ValueError("Weather sites must match the distinct turbine IDs in data.inputs")
    for site in sites:
        if not (-90 <= site["latitude"] <= 90 and -180 <= site["longitude"] <= 180):
            raise ValueError("Site coordinates are outside valid latitude/longitude ranges")
    daily_origins(weather["start_origin"], weather["end_origin"])
    daily_origins(values["replay"]["start_origin"], values["replay"]["end_origin"])
    utc_timestamp(values["evaluation"]["final_train_cutoff"], "final_train_cutoff")
    return ProjectConfig(root=root, values=values)
