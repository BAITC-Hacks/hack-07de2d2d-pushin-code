"""Deterministic synthetic stand-ins for Kuba's functions, same shapes as contract §5.1.

ports.py falls back to these while windcast.weather / data / model are missing, and every
weather result is marked source "stub" (visible in events and /health). Numbers are synthetic
and seeded by the issue date: they exercise the agent, store and API end to end, they are
not a forecast.

Runs (contract v0.4 §2): hour h of "latest" comes from the run started at the target hour
minus timeline.lead_days(h) days, floored to 00/06/12/18 UTC; "previous" (v1) is one day older.
Every such run is published (init + 8 h) by T = D 19:00 UTC. "latest" moves the wind by a
date-seeded shift, sometimes above the 0.5 m/s recalculation threshold, sometimes not.
Live (T = now): one run for the whole window — the newest published one ("latest") or the one
before it ("previous").
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from windcast import timeline

MODEL_VERSION = "stub-powercurve-0"
WEATHER_MODEL = "stub_synthetic"
LIVE = "live"
RUNS = ("previous", "latest")
RUN_STEP_H = 6


def _seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:4], "little")


def power_curve(wind_ms) -> np.ndarray:
    """Normalised power of one turbine: 0 below 3 m/s, ~0.6 at 8.5 m/s, rated from 11.5."""
    w = np.asarray(wind_ms, dtype=float)
    p = np.clip((w - 3.0) / 8.5, 0.0, 1.0) ** 1.2
    return np.where(w < 3.0, 0.0, np.where(w >= 11.5, 1.0, p))


def _floor_run(moment: datetime, step_h: int) -> datetime:
    moment = moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return moment - timedelta(hours=moment.hour % step_h)


def hour_groups() -> list[tuple[int, int]]:
    """Consecutive horizons sharing one Previous Runs bucket: (1, 17), (18, 41), (42, 48)."""
    groups: list[list[int]] = []
    for h in range(1, timeline.HORIZON + 1):
        if groups and timeline.lead_days(h) == timeline.lead_days(groups[-1][0]):
            groups[-1][1] = h
        else:
            groups.append([h, h])
    return [(lo, hi) for lo, hi in groups]


def _window(issue_date: str):
    """T, target hours, per-hour init of both runs and the seeds for this issue."""
    if issue_date == LIVE:
        now, targets = timeline.live_times()
        latest = _floor_run(now - timeline.RUN_AVAILABILITY_DELAY, RUN_STEP_H)
        previous = latest - timedelta(hours=RUN_STEP_H)
        n = len(targets)
        return (
            now,
            targets,
            [previous] * n,
            [latest] * n,
            ("live", targets[0].isoformat()),
            ("live-shift", latest.isoformat()),
        )
    issue = timeline.issue_time_utc(issue_date)
    targets = timeline.target_times_utc(issue_date)
    previous_init, latest_init = [], []
    for h, target in enumerate(targets, start=1):
        for out, older in ((previous_init, True), (latest_init, False)):
            days = timeline.lead_days(h, previous=older)
            out.append(_floor_run(target - timedelta(days=days), RUN_STEP_H))
    key = timeline.parse_issue_date(issue_date).isoformat()
    return (
        issue,
        targets,
        previous_init,
        latest_init,
        ("archive", key),
        ("archive-shift", key),
    )


def _base_weather(seed: int, targets: list[datetime]):
    rng = np.random.default_rng(seed)
    n = len(targets)
    hours = np.arange(n)
    local_hour = np.array([t.astimezone(timeline.LOCAL_TZ).hour for t in targets])
    base = rng.uniform(4.5, 9.5)
    amp = rng.uniform(2.0, 5.5)
    period = rng.uniform(18.0, 40.0)
    phase = rng.uniform(0.0, 40.0)
    noise = np.convolve(rng.normal(0.0, 0.9, n + 2), np.ones(3) / 3, mode="valid")
    wind = (
        base
        + amp * np.sin(2 * np.pi * (hours + phase) / period)
        + np.sin(2 * np.pi * (local_hour - 14) / 24)
        + noise
    )
    temp = (
        rng.uniform(-11.0, 1.0)
        + 4.5 * np.sin(2 * np.pi * (local_hour - 9) / 24)
        + 0.6 * noise
    )
    direction = (rng.uniform(0.0, 360.0) + 25.0 * np.sin(2 * np.pi * hours / 30)) % 360
    return np.clip(wind, 0.3, 24.0), temp, direction


def _run_entry(hours: str, inits: list[datetime]) -> dict:
    return {
        "hours": hours,
        "model": WEATHER_MODEL,
        "init_utc": timeline.iso_utc(max(inits)),
    }


def fetch_weather(issue_date: str, run: str = "latest") -> dict:
    if run not in RUNS:
        raise ValueError(f'run должен быть "previous" или "latest", а не «{run}»')
    if issue_date != LIVE:
        issue_date = timeline.parse_issue_date(issue_date).isoformat()
    issue, targets, previous_init, latest_init, base_key, shift_key = _window(
        issue_date
    )
    wind, temp, direction = _base_weather(_seed(*base_key), targets)
    inits = previous_init
    if run == "latest":
        rng = np.random.default_rng(_seed(*shift_key))
        shift = rng.choice([-1.0, 1.0]) * rng.uniform(0.05, 1.05)
        jitter = rng.normal(0.0, 0.15, len(targets))
        changed = np.array([a > b for a, b in zip(latest_init, previous_init)])
        wind = np.where(changed, np.clip(wind + shift + jitter, 0.3, 24.0), wind)
        temp = np.where(changed, temp + 0.3 * shift, temp)
        inits = latest_init
    hourly = pd.DataFrame(
        {
            "h": np.arange(1, len(targets) + 1),
            "target_time_utc": pd.to_datetime(targets, utc=True),
            "wind_100m_ms": np.round(wind, 2),
            "wind_10m_ms": np.round(wind * 0.75, 2),
            "wind_dir_deg": np.round(direction, 1),
            "temp_c": np.round(temp, 2),
            "init_time_utc": pd.to_datetime(inits, utc=True),
        }
    )
    return {
        "issue_date": issue_date,
        "issue_time_utc": issue,
        "hourly": hourly,
        "runs": [
            _run_entry(f"{lo}-{hi}", inits[lo - 1 : hi]) for lo, hi in hour_groups()
        ],
        "source": "stub",
    }


def check_data(issue_date: str, weather: dict) -> dict:
    hourly = weather["hourly"]
    issue = pd.Timestamp(weather["issue_time_utc"])
    issue = issue.tz_localize("UTC") if issue.tzinfo is None else issue
    missing = int(hourly[["wind_100m_ms", "temp_c"]].isna().any(axis=1).sum())
    missing += max(0, timeline.HORIZON - len(hourly))
    inits = pd.to_datetime(hourly["init_time_utc"], utc=True)
    published = inits + timeline.RUN_AVAILABILITY_DELAY
    before = bool((published <= issue).all())
    lag_h = (issue - published.max()).total_seconds() / 3600
    notes = [
        f"{timeline.HORIZON - missing} из {timeline.HORIZON} ч без пропусков",
        f"последний прогон опубликован за {lag_h:.0f} ч до момента выпуска",
        "погода синтетическая (заглушка)",
    ]
    if not before:
        notes.append("есть прогоны, опубликованные после момента выпуска")
    return {
        "ok": missing == 0 and before,
        "missing_hours": missing,
        "runs_before_issue": before,
        "notes": notes,
    }


def predict(issue_date: str, weather: dict) -> pd.DataFrame:
    hourly = weather["hourly"].sort_values("h").reset_index(drop=True)
    h = hourly["h"].to_numpy(dtype=int)
    wind = hourly["wind_100m_ms"].to_numpy(dtype=float)
    temp = hourly["temp_c"].to_numpy(dtype=float)
    times = pd.to_datetime(hourly["target_time_utc"], utc=True)
    widen = 0.05 + 0.10 * (h - 1) / (timeline.HORIZON - 1)
    quantiles = {}
    for turbine, gain in (("1", 1.02), ("2", 0.98)):
        p50 = 0.97 * power_curve(wind * gain)
        spread = widen + 0.25 * p50 * (1.0 - p50)
        quantiles[turbine] = (
            np.clip(p50 - 1.1 * spread, 0.0, 1.0),
            p50,
            np.clip(p50 + spread, 0.0, 1.0),
        )
    quantiles["plant"] = tuple(
        (a + b) / 2 for a, b in zip(quantiles["1"], quantiles["2"])
    )
    frames = [
        pd.DataFrame(
            {
                "h": h,
                "target_time_utc": times,
                "turbine": turbine,
                "p10": q[0],
                "p50": q[1],
                "p90": q[2],
                "wind_fc_ms": wind,
                "temp_fc_c": temp,
            }
        )
        for turbine, q in quantiles.items()
    ]
    out = pd.concat(frames, ignore_index=True)
    order = {t: i for i, t in enumerate(timeline.TURBINES)}
    out["_order"] = out["turbine"].map(order)
    out = out.sort_values(["h", "_order"], kind="stable").drop(columns="_order")
    return out.reset_index(drop=True)
