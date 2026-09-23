"""Repository paths, shared by every module.

Everything resolves from WINDCAST_ROOT (default: the repository root), so the same code works
from a clone, in tests with a temporary root and inside the container (WINDCAST_ROOT=/app).
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def root() -> Path:
    return Path(os.environ.get("WINDCAST_ROOT", _DEFAULT_ROOT))


def raw_dir() -> Path:
    return root() / "data" / "raw"


def processed_dir() -> Path:
    return root() / "data" / "processed"


def weather_cache_dir() -> Path:
    return root() / "data" / "weather_cache"


def models_dir() -> Path:
    return root() / "models"


def outputs_dir() -> Path:
    return root() / "outputs"


def forecasts_dir() -> Path:
    return outputs_dir() / "forecasts"


def traces_dir() -> Path:
    return outputs_dir() / "traces"


def live_dir() -> Path:
    return outputs_dir() / "live"


def metrics_file() -> Path:
    return outputs_dir() / "metrics_jan.json"
