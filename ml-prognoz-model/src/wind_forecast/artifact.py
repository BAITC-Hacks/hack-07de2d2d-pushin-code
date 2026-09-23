"""Portable, point-in-time validated forecast artifact serialization."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import pickle
import platform
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

IDENTITY_COLUMNS = (
    "turbine_id",
    "forecast_origin",
    "target_time",
    "weather_run_time",
    "weather_available_at",
    "lead_hours",
)
ARTIFACT_VERSION = "1.0"


def _utc_timestamp(value: Any, name: str):
    import pandas as pd

    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware UTC.")
    return ts.tz_convert("UTC")


def _parse_aware_timestamp_series(values: Any, name: str):
    """Parse a raw timestamp column without silently assigning UTC to naive data."""
    import pandas as pd

    for index, value in values.items():
        if pd.isna(value):
            continue
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} contains an invalid timestamp at row {index!r}.") from exc
        if timestamp.tzinfo is None:
            raise ValueError(
                f"{name} must contain timezone-aware timestamps; row {index!r} is naive."
            )
    parsed = pd.to_datetime(values, utc=True, errors="raise")
    if parsed.isna().any():
        raise ValueError(f"{name} contains null timestamps.")
    return parsed


@dataclass
class ForecastArtifact:
    """A fitted preprocessing+model bundle. Only load pickle files you trust."""

    model: Any
    feature_schema: Sequence[str]
    trained_until: Any
    model_config: Mapping[str, Any]
    weather_contract: Mapping[str, Any]
    metrics: Mapping[str, Any] = field(default_factory=dict)
    version: str = ARTIFACT_VERSION

    def __post_init__(self):
        self.feature_schema = tuple(self.feature_schema)
        self.trained_until = _utc_timestamp(self.trained_until, "trained_until")

    def predict(self, weather_df):
        """Build persisted-schema features from raw weather and return clipped power.

        The caller supplies raw forecast weather, never hand-assembled model features.
        ``build_features`` is also the single source of point-in-time feature logic.
        """
        import numpy as np
        import pandas as pd

        missing = [c for c in IDENTITY_COLUMNS if c not in weather_df.columns]
        if missing:
            raise ValueError(f"Weather input is missing required columns: {missing}")
        frame = weather_df.copy()
        for column in (
            "forecast_origin",
            "target_time",
            "weather_run_time",
            "weather_available_at",
        ):
            frame[column] = _parse_aware_timestamp_series(frame[column], column)
        expected_models = self.weather_contract.get(
            "trained_weather_models", self.weather_contract.get("weather_model")
        )
        if expected_models is not None:
            if "weather_model" not in frame.columns:
                raise ValueError(
                    "Weather input is missing weather_model required by this artifact."
                )
            expected = (
                {str(value) for value in expected_models}
                if isinstance(expected_models, (list, tuple, set, frozenset))
                else {str(expected_models)}
            )
            supplied = set(frame["weather_model"].dropna().astype(str))
            if frame["weather_model"].isna().any() or supplied.difference(expected):
                raise ValueError(
                    "Weather input weather_model is incompatible with the artifact "
                    f"(expected one of {sorted(expected)}, got {sorted(supplied)})."
                )
        if not (
            (frame["weather_run_time"] <= frame["forecast_origin"])
            & (frame["weather_available_at"] <= frame["forecast_origin"])
        ).all():
            raise ValueError(
                "Weather lineage violation: run/available time is after forecast_origin."
            )
        if not (frame["weather_run_time"] <= frame["weather_available_at"]).all():
            raise ValueError(
                "Weather lineage violation: weather_run_time is after weather_available_at."
            )
        if not (frame["forecast_origin"] >= self.trained_until).all():
            raise ValueError(
                "Artifact was trained after at least one forecast_origin; "
                "refusing time-travel prediction."
            )
        lead = pd.to_numeric(frame["lead_hours"], errors="raise")
        if not lead.between(1, 48).all():
            raise ValueError("lead_hours must be in the direct-forecast horizon 1..48.")
        try:
            from .features import FEATURE_COLUMNS, build_features
        except ImportError as exc:
            raise RuntimeError(
                "wind_forecast.features is required to transform raw weather for prediction."
            ) from exc
        if tuple(FEATURE_COLUMNS) != self.feature_schema:
            raise ValueError(
                "Installed feature schema differs from the artifact schema; "
                "refusing incompatible prediction."
            )
        featured = build_features(frame)
        missing_features = [c for c in self.feature_schema if c not in featured.columns]
        if missing_features:
            raise ValueError(
                f"Feature builder did not produce artifact features: {missing_features}"
            )
        X = featured.loc[:, self.feature_schema].copy()
        if self.model_config.get("family") == "catboost":
            for column in self.weather_contract.get("categorical_features", []):
                X[column] = X[column].fillna("__MISSING__").astype(str)
        raw = np.asarray(self.model.predict(X), dtype=float)
        if raw.ndim != 1 or raw.shape[0] != len(frame):
            raise ValueError("Model returned a prediction count different from the weather input.")
        if not np.isfinite(raw).all():
            raise ValueError("Model returned non-finite predictions.")
        result = frame.loc[:, IDENTITY_COLUMNS].copy()
        result["power_prediction"] = np.clip(raw, 0.0, 1.0)
        result.attrs["raw_out_of_range_count"] = int(((raw < 0.0) | (raw > 1.0)).sum())
        return result

    def save(self, path: str | Path) -> tuple[Path, Path]:
        """Write a pickle and a human-readable SHA-256 manifest beside it."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix != ".pkl":
            path = path.with_suffix(".pkl")
        payload = pickle.dumps(self, protocol=pickle.HIGHEST_PROTOCOL)
        path.write_bytes(payload)
        manifest = path.with_suffix(path.suffix + ".manifest.json")
        manifest.write_text(
            json.dumps(
                {
                    "artifact_version": self.version,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "pickle": path.name,
                    "trained_until": self.trained_until.isoformat(),
                    "feature_schema": list(self.feature_schema),
                    "model_config": dict(self.model_config),
                    "weather_contract": dict(self.weather_contract),
                    "provenance": {
                        k: self.weather_contract[k]
                        for k in ("dataset_sha256", "observation_timezone", "weather_source")
                        if k in self.weather_contract
                    },
                    "runtime_versions": _runtime_versions(),
                    "metrics": dict(self.metrics),
                    "created_at": datetime.now().astimezone().isoformat(),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path, manifest

    @classmethod
    def load(cls, path: str | Path) -> "ForecastArtifact":
        """Load a trusted pickle after verifying its adjacent manifest if present."""
        path = Path(path)
        payload = path.read_bytes()
        manifest = path.with_suffix(path.suffix + ".manifest.json")
        if manifest.exists():
            expected = json.loads(manifest.read_text(encoding="utf-8")).get("sha256")
            if expected != hashlib.sha256(payload).hexdigest():
                raise ValueError("Artifact SHA-256 does not match its manifest.")
        obj = pickle.loads(payload)  # noqa: S301 - caller is explicitly warned about trusted files
        if not isinstance(obj, cls):
            raise TypeError("Pickle does not contain a ForecastArtifact.")
        return obj


def _runtime_versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for package in ("pandas", "numpy", "scikit-learn", "catboost"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions
