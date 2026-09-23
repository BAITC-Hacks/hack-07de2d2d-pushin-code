"""Trusted, self-contained artifact for 48-hour CSV-history forecasts."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import pickle
import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ARTIFACT_VERSION = "1.0"


def _utc(value: Any, name: str):
    import pandas as pd

    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware UTC.")
    return stamp.tz_convert("UTC")


@dataclass
class HistoryForecastArtifact:
    """Fitted 48-output model plus the exact past-only feature contract.

    Pickle loading can execute arbitrary code; use :meth:`load` only on files
    from a trusted source.
    """

    model: Any
    feature_schema: Sequence[str]
    trained_until: Any
    model_config: Mapping[str, Any]
    provenance: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    version: str = ARTIFACT_VERSION

    def __post_init__(self):
        self.feature_schema = tuple(self.feature_schema)
        self.trained_until = _utc(self.trained_until, "trained_until")

    def predict(self, hourly, origin):
        """Create one complete 48-hour, two-turbine forecast from past CSV data."""
        import numpy as np
        import pandas as pd

        forecast_origin = _utc(origin, "origin")
        if forecast_origin < self.trained_until:
            raise ValueError(
                "Artifact was trained after the requested origin; refusing time travel."
            )
        try:
            from .history import FEATURE_COLUMNS, build_history_features
        except ImportError as exc:
            raise RuntimeError(
                "wind_forecast.history is required for CSV-history prediction."
            ) from exc
        if tuple(FEATURE_COLUMNS) != self.feature_schema:
            raise ValueError("Installed history feature schema differs from the artifact schema.")
        featured = build_history_features(hourly, [forecast_origin])
        needed = {"forecast_origin", "turbine_id", *self.feature_schema}
        missing = sorted(needed.difference(featured.columns))
        if missing:
            raise ValueError(f"History feature builder did not produce required columns: {missing}")
        expected_turbines = tuple(
            str(item) for item in self.provenance.get("trained_turbines", ("1", "2"))
        )
        rows = featured.loc[featured["forecast_origin"] == forecast_origin].copy()
        actual_turbines = tuple(sorted(rows["turbine_id"].astype(str).unique()))
        if set(actual_turbines) != set(expected_turbines) or len(rows) != len(expected_turbines):
            raise ValueError(
                "History input cannot provide the required complete past window "
                "for every trained turbine."
            )
        X = rows.loc[:, self.feature_schema]
        raw = np.asarray(self.model.predict(X), dtype=float)
        if raw.shape != (len(rows), 48):
            raise ValueError(f"Model must return shape ({len(rows)}, 48), got {raw.shape}.")
        if not np.isfinite(raw).all():
            raise ValueError("Model returned non-finite history predictions.")
        output = []
        for row_index, (_, row) in enumerate(rows.iterrows()):
            for horizon in range(48):
                output.append(
                    {
                        "turbine_id": str(row["turbine_id"]),
                        "forecast_origin": forecast_origin,
                        "target_time": forecast_origin + pd.Timedelta(horizon, unit="h"),
                        "lead_hours": horizon + 1,
                        "power_prediction": float(np.clip(raw[row_index, horizon], 0.0, 1.0)),
                    }
                )
        result = pd.DataFrame(output)
        result.attrs["raw_out_of_range_count"] = int(((raw < 0) | (raw > 1)).sum())
        return result

    def save(self, path: str | Path) -> tuple[Path, Path]:
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
                    "pickle": path.name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "trained_until": self.trained_until.isoformat(),
                    "feature_schema": list(self.feature_schema),
                    "model_config": dict(self.model_config),
                    "provenance": dict(self.provenance),
                    "metrics": dict(self.metrics),
                    "runtime_versions": _runtime_versions(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path, manifest

    @classmethod
    def load(cls, path: str | Path) -> "HistoryForecastArtifact":
        path = Path(path)
        payload = path.read_bytes()
        manifest = path.with_suffix(path.suffix + ".manifest.json")
        if manifest.exists():
            expected = json.loads(manifest.read_text(encoding="utf-8")).get("sha256")
            if expected != hashlib.sha256(payload).hexdigest():
                raise ValueError("Artifact SHA-256 does not match its manifest.")
        artifact = pickle.loads(payload)  # noqa: S301 - trusted artifacts only
        if not isinstance(artifact, cls):
            raise TypeError("Pickle does not contain a HistoryForecastArtifact.")
        return artifact


def _runtime_versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for package in ("pandas", "numpy", "scikit-learn", "catboost"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions
