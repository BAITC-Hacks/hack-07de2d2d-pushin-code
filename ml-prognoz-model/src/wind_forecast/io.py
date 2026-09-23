"""Artifact I/O and content fingerprints shared by the command-line workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def write_json(value: Any, path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(value, default=_json_default, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def file_sha256(path: str | Path) -> str:
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def write_table(frame: pd.DataFrame, path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix == ".parquet":
        frame.to_parquet(output, index=False)
    elif output.suffix == ".csv":
        frame.to_csv(output, index=False)
    else:
        raise ValueError("Table output must end in .parquet or .csv")


def read_table(path: str | Path) -> pd.DataFrame:
    source = Path(path)
    if source.suffix == ".parquet":
        frame = pd.read_parquet(source)
    elif source.suffix == ".csv":
        frame = pd.read_csv(source, dtype={"turbine_id": "string"})
        for column in (
            "forecast_origin",
            "target_time",
            "weather_run_time",
            "weather_available_at",
        ):
            if column in frame:
                # A CSV import must not silently interpret naive times as UTC.
                parsed = pd.to_datetime(frame[column], errors="raise")
                if not isinstance(parsed.dtype, pd.DatetimeTZDtype):
                    raise ValueError(f"{column} must contain explicit timezone offsets")
                frame[column] = parsed.dt.tz_convert("UTC")
    else:
        raise ValueError("Table input must end in .parquet or .csv")
    if "turbine_id" in frame:
        frame["turbine_id"] = frame["turbine_id"].astype("string")
    return frame
