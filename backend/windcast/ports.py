"""Lazy ports to Kuba's ML functions (contract §5.1) with a stub fallback.

Each call resolves the real function at call time, so the agent picks up windcast.weather /
data / model as soon as they land, without a restart of anything that imported this module.
A port falls back to windcast._stubs only when the module itself is missing or lacks the
attributes the contract names; any other import error propagates (Kuba's bug stays visible).
WINDCAST_FORCE_STUBS=1 forces every port onto the stubs (hermetic tests, no network).
"""

from __future__ import annotations

import importlib
import os
from types import ModuleType

import pandas as pd

from windcast import _stubs

FORCE_STUBS_ENV = "WINDCAST_FORCE_STUBS"

# port -> (module, attributes it must export for the port to count as real)
PORTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "weather": ("windcast.weather", ("fetch_weather",)),
    "data": ("windcast.data", ("check_data",)),
    "model": ("windcast.model", ("predict", "MODEL_VERSION")),
}


def _forced() -> bool:
    return os.environ.get(FORCE_STUBS_ENV, "").strip().lower() not in ("", "0", "false")


def _module(port: str) -> ModuleType | None:
    """Kuba's module for this port, or None when it is missing or incomplete."""
    if _forced():
        return None
    name, attrs = PORTS[port]
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        raise
    if all(hasattr(module, attr) for attr in attrs):
        return module
    return None


def source(port: str) -> str:
    return "real" if _module(port) is not None else "stub"


def status() -> dict[str, str]:
    """{"weather": "real" | "stub", "data": ..., "model": ...} — for /health and events."""
    return {port: source(port) for port in PORTS}


def fetch_weather(issue_date: str, run: str = "latest") -> dict:
    module = _module("weather")
    impl = module.fetch_weather if module is not None else _stubs.fetch_weather
    return impl(issue_date, run=run)


def check_data(issue_date: str, weather: dict) -> dict:
    module = _module("data")
    impl = module.check_data if module is not None else _stubs.check_data
    return impl(issue_date, weather)


def predict(issue_date: str, weather: dict) -> pd.DataFrame:
    module = _module("model")
    impl = module.predict if module is not None else _stubs.predict
    return impl(issue_date, weather)


def model_version() -> str:
    module = _module("model")
    return str(module.MODEL_VERSION if module is not None else _stubs.MODEL_VERSION)
