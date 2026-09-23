"""The agent's six tools (contract §5) over one run context, plus their OpenAI schemas.

Tools are deterministic and never decide the agent's course:
- fetch_weather(run="latest") only reports facts against the current version (fresher run?
  every run published by T? wind shift, new risks) — whether to recalculate is the agent's
  decision (the LLM in mode "agent", the same policy as a rule in mode "deterministic");
- recalc_forecast executes that decision, behind a hard guardrail in code: no fresher run
  published by T (init + 8 h <= T, contract v0.4 §2) means refusal, whatever was asked;
- numbers come only from the model (ports.predict); the LLM's summary is published only if
  every number in it appears in tool outputs, otherwise the template summary is used.

Each tool returns a compact JSON-serialisable dict for the LLM (aggregates, never 144 rows)
and emits tool_call / tool_result (publish: action) events for the trace.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from windcast import ports, store, timeline

log = logging.getLogger(__name__)

LIVE = "live"
RUNS = ("previous", "latest")
HORIZON = timeline.HORIZON
TURBINES = timeline.TURBINES
EVENT_TYPES = ("thought", "tool_call", "tool_result", "action", "verdict", "error")
STAGES = ("weather", "prep", "model", "forecast", "analysis", "recalc")
STATUSES = ("ok", "warn", "skip", "error")
FLAG_KINDS = ("ramp", "ice", "wind_gt20", "models_diverge")
KIND_RU = {
    "ramp": "рампа",
    "ice": "обледенение",
    "wind_gt20": "ветер > 20 м/с",
    "models_diverge": "расхождение моделей",
}

RECALC_SHIFT_MS = 0.5
SHIFT_HORIZON_H = 24
RAMP_THRESHOLD = 0.3
RAMP_WINDOW_H = 2
ICE_RANGE_C = (-3.0, 1.0)
ICE_MIN_HOURS = 2
WIND_STOP_MS = 20.0
DIVERGE_MS = 3.0
MINUS = "−"


class ToolError(Exception):
    """The tool refuses: the message goes back to the caller and into the trace."""


# ---------- formatting ----------
def pct(value: float) -> int:
    return round(float(value) * 100)


def _r(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def fmt1(value: float) -> str:
    text = f"{abs(float(value)):.1f}".replace(".", ",")
    return MINUS + text if round(float(value), 1) < 0 else text


def fmt_shift(value: float) -> str:
    """|Δwind| for titles: one decimal unless that would read as exactly the threshold."""
    text = fmt1(value)
    return f"{float(value):.2f}".replace(".", ",") if text == "0,5" else text


def signed1(value: float) -> str:
    sign = "+" if round(float(value), 1) >= 0 else MINUS
    return sign + f"{abs(float(value)):.1f}".replace(".", ",")


def signed_int(value: int) -> str:
    return ("+" if int(value) >= 0 else MINUS) + str(abs(int(value)))


def to_utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def local_label(value) -> str:
    """ "14.02 13:00" — local time as the dispatcher reads it."""
    return to_utc(value).tz_convert(timeline.LOCAL_TZ).strftime("%d.%m %H:%M")


def utc_label(value) -> str:
    return to_utc(value).strftime("%d.%m %H:%M UTC")


def _span(times: list, i: int, j: int, *, through_end: bool) -> str:
    end = to_utc(times[j]) + (pd.Timedelta(hours=1) if through_end else pd.Timedelta(0))
    end_text = end.tz_convert(timeline.LOCAL_TZ).strftime("%H:%M")
    return f"{local_label(times[i])}–{end_text}"


# ---------- weather ----------
def normalize_weather(weather: dict) -> dict:
    """Copy of fetch_weather() output with UTC timestamps, sorted by h; checks the columns."""
    if not isinstance(weather, dict) or "hourly" not in weather:
        raise ToolError("fetch_weather вернул не тот формат: нет hourly")
    hourly = pd.DataFrame(weather["hourly"]).copy()
    need = {"h", "target_time_utc", "wind_100m_ms", "temp_c", "init_time_utc"}
    missing = sorted(need - set(hourly.columns))
    if missing:
        raise ToolError(f"в погоде нет колонок: {', '.join(missing)}")
    for column in ("target_time_utc", "init_time_utc"):
        hourly[column] = pd.to_datetime(hourly[column], utc=True).astype(
            "datetime64[ns, UTC]"
        )
    hourly["h"] = hourly["h"].astype(int)
    out = dict(weather)
    out["hourly"] = hourly.sort_values("h").reset_index(drop=True)
    out["runs"] = list(weather.get("runs") or [])
    out["source"] = str(weather.get("source") or "unknown")
    if weather.get("issue_time_utc") is not None:
        out["issue_time_utc"] = to_utc(weather["issue_time_utc"]).to_pydatetime()
    return out


def weather_runs(weather: dict, published: Callable[[Any], bool]) -> list[dict]:
    """Runs behind a weather window; before_issue = published (init + 8 h) by T."""
    hourly = weather["hourly"]
    declared = weather.get("runs") or [{"hours": "1-48"}]
    out = []
    for run in declared:
        lo, hi = store.hour_range(run.get("hours", "")) or (1, HORIZON)
        inits = list(hourly.loc[hourly["h"].between(lo, hi), "init_time_utc"])
        if run.get("init_utc"):
            inits.append(to_utc(run["init_utc"]))
        if not inits:
            continue
        init = max(inits)
        out.append(
            {
                "hours": str(run.get("hours") or f"{lo}-{hi}"),
                "model": str(run.get("model") or "unknown"),
                "init_utc": timeline.iso_utc(init),
                "before_issue": bool(published(init)),
            }
        )
    return out


def init_by_h(weather: dict) -> dict[int, str]:
    hourly = weather["hourly"]
    return {
        int(h): timeline.iso_utc(init)
        for h, init in zip(hourly["h"], hourly["init_time_utc"])
    }


# ---------- forecast frames ----------
def _turbine_label(value) -> str:
    text = str(value).strip().lower()
    if text in TURBINES:
        return text
    try:
        return str(int(float(text)))
    except ValueError:
        return text


def sanitize_forecast(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """predict() output in contract shape: 144 rows, UTC, P10 <= P50 <= P90 in [0, 1].

    Clipping and ordering the quantiles is the only change made to the model's numbers;
    the count of touched rows is reported in the trace.
    """
    need = [
        "h",
        "target_time_utc",
        "turbine",
        "p10",
        "p50",
        "p90",
        "wind_fc_ms",
        "temp_fc_c",
    ]
    df = pd.DataFrame(frame).copy()
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ToolError(
            f"predict вернул не тот формат: нет колонок {', '.join(missing)}"
        )
    df = df[need].copy()
    df["h"] = df["h"].astype(int)
    df["turbine"] = df["turbine"].map(_turbine_label)
    df["target_time_utc"] = pd.to_datetime(df["target_time_utc"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    q = df[["p10", "p50", "p90"]].astype(float).to_numpy()
    if np.isnan(q).any():
        raise ToolError("predict вернул пустые P10/P50/P90")
    fixed = q.copy()
    fixed = np.sort(np.clip(fixed, 0.0, 1.0), axis=1)
    touched = int((np.abs(fixed - q) > 1e-12).any(axis=1).sum())
    df[["p10", "p50", "p90"]] = fixed
    expected = {(h, t) for h in range(1, HORIZON + 1) for t in TURBINES}
    if len(df) != len(expected) or set(zip(df["h"], df["turbine"])) != expected:
        raise ToolError(
            f"predict вернул {len(df)} строк вместо {len(expected)} (48 ч × 1, 2, ВЭС)"
        )
    order = {t: i for i, t in enumerate(TURBINES)}
    df["_order"] = df["turbine"].map(order)
    df = df.sort_values(["h", "_order"]).drop(columns="_order").reset_index(drop=True)
    return df, touched


def plant_rows(frame: pd.DataFrame) -> pd.DataFrame:
    plant = frame[frame["turbine"] == "plant"]
    return plant.sort_values("h").reset_index(drop=True)


def plant_stats(frame: pd.DataFrame) -> dict:
    plant = plant_rows(frame)
    p50 = plant["p50"].to_numpy(dtype=float)
    band = (plant["p90"] - plant["p10"]).to_numpy(dtype=float)
    times = list(plant["target_time_utc"])
    i_max, i_min = int(np.argmax(p50)), int(np.argmin(p50))
    return {
        "peak_pct": pct(p50[i_max]),
        "peak_time_local": local_label(times[i_max]),
        "min_pct": pct(p50[i_min]),
        "min_time_local": local_label(times[i_min]),
        "mean_pct": pct(p50.mean()),
        "band_p10_p90_pct": pct(band.mean()),
        "window_local": f"{local_label(times[0])} — {local_label(times[-1])}",
    }


def _stats_text(stats: dict) -> str:
    return (
        f"ВЭС: пик {stats['peak_pct']} % номинала — {stats['peak_time_local']}, "
        f"минимум {stats['min_pct']} % — {stats['min_time_local']}, "
        f"в среднем {stats['mean_pct']} %; ширина P10–P90 в среднем "
        f"{stats['band_p10_p90_pct']} п.п."
    )


# ---------- flags ----------
def _segments(mask, min_len: int) -> list[tuple[int, int]]:
    out, start = [], None
    for i, flag in enumerate(list(mask) + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if i - start >= min_len:
                out.append((start, i - 1))
            start = None
    return out


def _ramp_flags(hs, times, p50) -> list[dict]:
    """Plant P50 change >= 0.3 within 2 h; adjacent ramps of one sign are merged."""
    spans: list[list[int]] = []
    i, n = 0, len(p50)
    while i < n - 1:
        best_k, best_d = 0, 0.0
        for k in range(1, RAMP_WINDOW_H + 1):
            if i + k < n:
                d = p50[i + k] - p50[i]
                if abs(d) >= RAMP_THRESHOLD and abs(d) > abs(best_d):
                    best_k, best_d = k, d
        if not best_k:
            i += 1
            continue
        j = i + best_k
        sign = 1 if best_d > 0 else -1
        if spans and spans[-1][2] == sign and spans[-1][1] >= i:
            spans[-1][1] = j
        else:
            spans.append([i, j, sign])
        i = j
    flags = []
    for i, j, _sign in spans:
        d = p50[j] - p50[i]
        word = "спад " + MINUS if d < 0 else "рост +"
        flags.append(
            {
                "kind": "ramp",
                "from_h": int(hs[i]),
                "to_h": int(hs[j]),
                "text": f"{word}{pct(abs(d))} % за {j - i} ч · "
                f"{_span(times, i, j, through_end=False)}",
            }
        )
    return flags


def _ice_wind_flags(hs, times, temp, wind) -> list[dict]:
    temp = np.asarray(temp, dtype=float)
    wind = np.asarray(wind, dtype=float)
    lo, hi = ICE_RANGE_C
    flags = []
    for i, j in _segments((temp >= lo) & (temp <= hi), ICE_MIN_HOURS):
        seg = temp[i : j + 1]
        flags.append(
            {
                "kind": "ice",
                "from_h": int(hs[i]),
                "to_h": int(hs[j]),
                "text": f"риск обледенения: t от {fmt1(seg.min())} до {fmt1(seg.max())} °C"
                f" · {_span(times, i, j, through_end=True)}",
            }
        )
    for i, j in _segments(wind > WIND_STOP_MS, 1):
        flags.append(
            {
                "kind": "wind_gt20",
                "from_h": int(hs[i]),
                "to_h": int(hs[j]),
                "text": f"ветер до {fmt1(wind[i : j + 1].max())} м/с — возможна остановка"
                f" · {_span(times, i, j, through_end=True)}",
            }
        )
    return flags


def _diverge_flags(weather: dict | None) -> list[dict] | None:
    """Only when the weather carries a second model (wind_100m_ms_<model>); else None."""
    if not weather:
        return None
    hourly = weather["hourly"]
    alt = [c for c in hourly.columns if str(c).startswith("wind_100m_ms_")]
    if not alt:
        return None
    main = hourly["wind_100m_ms"].to_numpy(dtype=float)
    spread = np.nanmax(
        np.abs(hourly[alt].to_numpy(dtype=float) - main[:, None]), axis=1
    )
    hs = hourly["h"].to_numpy(dtype=int)
    times = list(hourly["target_time_utc"])
    return [
        {
            "kind": "models_diverge",
            "from_h": int(hs[i]),
            "to_h": int(hs[j]),
            "text": f"погодные модели расходятся до {fmt1(spread[i : j + 1].max())} м/с"
            f" · {_span(times, i, j, through_end=True)}",
        }
        for i, j in _segments(spread > DIVERGE_MS, 2)
    ]


def forecast_flags(
    frame: pd.DataFrame, weather: dict | None
) -> tuple[list[dict], list[str]]:
    plant = plant_rows(frame)
    hs = plant["h"].to_numpy(dtype=int)
    times = list(plant["target_time_utc"])
    flags = _ramp_flags(hs, times, plant["p50"].to_numpy(dtype=float))
    flags += _ice_wind_flags(hs, times, plant["temp_fc_c"], plant["wind_fc_ms"])
    diverge = _diverge_flags(weather)
    flags += diverge or []
    flags.sort(key=lambda f: (f["from_h"], FLAG_KINDS.index(f["kind"])))
    return flags, ([] if diverge is not None else ["models_diverge"])


def weather_flags(weather: dict) -> list[dict]:
    """Risks visible from the weather alone (ice, wind > 20, model divergence)."""
    hourly = weather["hourly"]
    hs = hourly["h"].to_numpy(dtype=int)
    times = list(hourly["target_time_utc"])
    flags = _ice_wind_flags(hs, times, hourly["temp_c"], hourly["wind_100m_ms"])
    return flags + (_diverge_flags(weather) or [])


def new_flags(fresh: list[dict], current: list[dict]) -> list[dict]:
    """Flags of the fresh run with no overlapping flag of the same kind in the current one."""
    return [
        f
        for f in fresh
        if not any(
            c["kind"] == f["kind"]
            and c["from_h"] <= f["to_h"]
            and f["from_h"] <= c["to_h"]
            for c in current
        )
    ]


def flag_counts(flags: list[dict]) -> dict[str, int]:
    return {kind: sum(f["kind"] == kind for f in flags) for kind in FLAG_KINDS}


# ---------- versions ----------
@dataclass
class Version:
    number: int
    frame: pd.DataFrame
    weather: dict
    run: str
    change_note: str | None = None
    flags: list[dict] | None = None
    skipped_checks: list[str] = field(default_factory=list)
    jump: dict | None = None
    summary: str | None = None
    published: bool = False


def version_jump(old: Version, new: Version) -> dict | None:
    a = plant_rows(old.frame).set_index("target_time_utc")["p50"]
    b = plant_rows(new.frame).set_index("target_time_utc")["p50"]
    common = a.index.intersection(b.index)
    if len(common) == 0:
        return None
    delta = b.loc[common] - a.loc[common]
    at = delta.abs().idxmax()
    return {
        "vs_version": old.number,
        "peak_delta_pp": pct(b.max()) - pct(a.max()),
        "mean_delta_pp": pct(delta.mean()),
        "max_abs_delta_pp": pct(abs(delta.loc[at])),
        "max_delta_at_local": local_label(at),
    }


def analyze_version(version: Version, previous: Version | None) -> dict:
    flags, skipped = forecast_flags(version.frame, version.weather)
    version.flags, version.skipped_checks = flags, skipped
    has_previous = previous is not None and previous is not version
    version.jump = version_jump(previous, version) if has_previous else None
    return {
        "version": version.number,
        "flags": flags,
        "counts": flag_counts(flags),
        "skipped_checks": skipped,
        "jump_vs_previous": version.jump,
        "plant": plant_stats(version.frame),
    }


def change_note(wind_shift: float, jump: dict | None, base: int) -> str:
    note = f"ветер {signed1(wind_shift)} м/с"
    if jump:
        peak = jump["peak_delta_pp"]
        peak_text = f"пик {signed_int(peak)} п.п." if peak else "пик без изменений"
        note += f" → {peak_text}, в среднем {signed_int(jump['mean_delta_pp'])} п.п."
    return f"{note} против v{base}"


_GROUPED = {
    "ice": ("обледенение", "возможны потери"),
    "wind_gt20": ("ветер выше 20 м/с", "возможна остановка турбин"),
    "models_diverge": ("погодные модели расходятся", "неопределённость выше"),
}


def risk_digest(version: Version, per_kind: int = 2) -> str:
    """Risks for the dispatcher, grouped by kind: what, when, what to do."""
    flags = version.flags or []
    if not flags:
        return ""
    plant = plant_rows(version.frame)
    times = dict(zip(plant["h"].astype(int), plant["target_time_utc"]))

    def hours(flag: dict) -> str:
        end = to_utc(times[flag["to_h"]]) + pd.Timedelta(hours=1)
        end_text = end.tz_convert(timeline.LOCAL_TZ).strftime("%H:%M")
        return f"{local_label(times[flag['from_h']])}–{end_text}"

    parts = []
    ramps = [f for f in flags if f["kind"] == "ramp"]
    for flag in ramps[:per_kind]:
        what = flag["text"].split(" · ")[0]
        advice = "держать резерв" if what.startswith("спад") else "учесть в заявке"
        parts.append(f"{what} около {local_label(times[flag['from_h']])} — {advice}")
    if len(ramps) > per_kind:
        parts.append(f"ещё рамп: {len(ramps) - per_kind}")
    for kind, (word, advice) in _GROUPED.items():
        group = [f for f in flags if f["kind"] == kind]
        if group:
            more = f" и ещё {len(group) - per_kind}" if len(group) > per_kind else ""
            spans = ", ".join(hours(f) for f in group[:per_kind])
            parts.append(f"{word} {spans}{more} — {advice}")
    return "; ".join(parts)


def summary_text(version: Version) -> str:
    """Template dispatcher summary (mode deterministic, or when the LLM's is rejected)."""
    stats = plant_stats(version.frame)
    parts = [
        (
            f"Пик {stats['peak_pct']} % номинала — {stats['peak_time_local']}, "
            f"минимум {stats['min_pct']} % — {stats['min_time_local']}, "
            f"в среднем за 48 ч — {stats['mean_pct']} %."
        )
    ]
    risks = risk_digest(version)
    parts.append(f"Риски: {risks}." if risks else "Рисков не найдено.")
    if version.change_note:
        parts.append(f"Пересчитано на свежем прогоне: {version.change_note}.")
    return " ".join(parts)


def version_from_record(record: dict, number: int) -> Version:
    """A published version back from its record: frame from rows, weather from rows + runs."""
    entry = record["versions"][str(number)]
    rows = pd.DataFrame(entry["rows"])
    frame = pd.DataFrame(
        {
            "h": rows["h"].astype(int),
            "target_time_utc": pd.to_datetime(rows["target_time_local"], utc=True),
            "turbine": rows["turbine"].map(_turbine_label),
            "p10": rows["p10"].astype(float),
            "p50": rows["p50"].astype(float),
            "p90": rows["p90"].astype(float),
            "wind_fc_ms": rows["wind_fc_ms"].astype(float),
            "temp_fc_c": rows["temp_fc_c"].astype(float),
        }
    )
    frame, _ = sanitize_forecast(frame)
    plant = plant_rows(frame)
    runs = entry.get("weather_runs") or []
    inits = store.init_by_h_from_runs(runs)
    fallback = max(inits.values()) if inits else None
    hourly = pd.DataFrame(
        {
            "h": plant["h"],
            "target_time_utc": plant["target_time_utc"],
            "wind_100m_ms": plant["wind_fc_ms"],
            "temp_c": plant["temp_fc_c"],
            "init_time_utc": [inits.get(int(h), fallback) for h in plant["h"]],
        }
    )
    weather = normalize_weather(
        {
            "issue_date": record["issue_date"],
            "issue_time_utc": record["issue_time_utc"],
            "hourly": hourly,
            "runs": runs,
            "source": entry.get("source") or "unknown",
        }
    )
    return Version(
        number=number,
        frame=frame,
        weather=weather,
        run="record",
        change_note=entry.get("change_note"),
        flags=list(entry.get("flags") or []),
        summary=entry.get("summary"),
        published=True,
    )


# ---------- grounding: numbers only from tools ----------
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[float]:
    out = set()
    for token in _NUMBER.findall(text or ""):
        out.add(round(float(token.replace(",", ".")), 4))
        out.update(float(int(piece)) for piece in re.split(r"[.,]", token) if piece)
    return out


def ungrounded_numbers(text: str, sources: list[str]) -> list[str]:
    """Numbers in text that appear in none of the sources (tool outputs, prompts)."""
    allowed: set[float] = set()
    for source in sources:
        allowed |= _numbers(source)
    return [
        token
        for token in _NUMBER.findall(text or "")
        if round(float(token.replace(",", ".")), 4) not in allowed
    ]


# ---------- decision policy (the agent's; tools only report facts) ----------
def policy_recalc(facts: dict | None) -> bool:
    """Recalculate when the fresher run is legal and moves the wind or brings a new risk.

    Live also recalculates when the current version no longer overlaps the new window.
    """
    if not facts:
        return False
    return (
        bool(facts.get("fresher_than_current"))
        and bool(facts.get("all_inits_before_issue"))
        and (
            float(facts.get("mean_abs_wind_shift_h1_24_ms") or 0.0) > RECALC_SHIFT_MS
            or bool(facts.get("new_flags_vs_current"))
            or facts.get("overlap_hours") == 0
        )
    )


def decision_title(facts: dict | None, recalc: bool, number: int, live: bool) -> str:
    if not facts:
        return (
            "Решаю пересчитать"
            if recalc
            else f"Новый прогон недоступен — v{number} остаётся"
        )
    shift = float(facts.get("mean_abs_wind_shift_h1_24_ms") or 0.0)
    fresh = facts.get("new_flags_vs_current") or []
    kinds = ", ".join(sorted({KIND_RU.get(f["kind"], f["kind"]) for f in fresh}))
    if not facts.get("all_inits_before_issue", True):
        return f"Прогон опубликован после момента выпуска — v{number} остаётся"
    if not facts.get("fresher_than_current"):
        if live:
            return f"Новый прогон ещё не вышел — v{number} остаётся"
        return f"Более свежего прогона до момента выпуска нет — v{number} остаётся"
    over = shift > RECALC_SHIFT_MS
    if recalc and facts.get("overlap_hours") == 0:
        return "Текущая версия не покрывает новое окно — пересчитываю"
    if recalc:
        if over:
            return f"Сдвиг {fmt_shift(shift)} м/с больше порога — пересчитываю"
        if fresh:
            return f"Новый риск на свежем прогоне ({kinds}) — пересчитываю"
        return f"Сдвиг {fmt_shift(shift)} м/с, новых рисков нет, но пересчитываю"
    if over or fresh:
        extra = f", новый риск: {kinds}" if fresh else ""
        return f"Сдвиг {fmt_shift(shift)} м/с{extra}, но v{number} остаётся"
    return f"Сдвиг {fmt_shift(shift)} м/с, новых рисков нет — v{number} остаётся"


def no_fresher_text(ctx: RunContext) -> str:
    """Why no fresher run exists: the next one is published only after T."""
    limit = ctx.limit_time()
    step = pd.Timedelta(hours=6)
    newest = (limit - pd.Timedelta(timeline.RUN_AVAILABILITY_DELAY)).floor("6h")
    upcoming = newest + step
    ready = upcoming + pd.Timedelta(timeline.RUN_AVAILABILITY_DELAY)
    if ctx.live:
        return (
            "новый прогон ещё не опубликован — последний уже учтён; следующий стартует "
            f"{utc_label(upcoming)}, опубликован будет ≈ {utc_label(ready)}"
        )
    return (
        "более свежего прогона до момента выпуска нет — прогон вышел бы после T: "
        f"следующий стартовал {utc_label(upcoming)}, опубликован ≈ {utc_label(ready)}, "
        f"позже T = {utc_label(limit)}"
    )


def late_run_text(init, limit) -> str:
    ready = to_utc(init) + pd.Timedelta(timeline.RUN_AVAILABILITY_DELAY)
    return (
        f"прогон стартовал в {utc_label(init)}, опубликован ≈ {utc_label(ready)} — позже "
        f"момента выпуска T = {utc_label(limit)}"
    )


def _source_unavailable(run: str, exc: Exception) -> Outcome:
    text = f"{type(exc).__name__}: {exc}"
    return Outcome(
        {
            "ok": False,
            "run": run,
            "source_unavailable": True,
            "error": f"источник погоды недоступен — {text}",
        },
        "Источник погоды недоступен",
        f"Запрос погоды (run={run}) не удался: {text}.",
        "error",
    )


# ---------- run context and trace ----------
@dataclass
class RunContext:
    issue_date: str
    trigger: str = "issue"
    issue_time_utc: pd.Timestamp | None = None
    scenario: str | None = None
    record: dict | None = None
    effective_mode: str = "deterministic"
    weathers: dict[str, dict] = field(default_factory=dict)
    checks: dict[str, dict] = field(default_factory=dict)
    last_run: str | None = None
    current: Version | None = None
    versions: dict[int, Version] = field(default_factory=dict)
    facts: dict | None = None
    decision: dict | None = None
    recalcs: list[dict] = field(default_factory=list)
    failed: str | None = None
    tool_calls: int = 0
    grounding: list[str] = field(default_factory=list)
    new_run_announced: bool = False
    fetch_attempts: int = 0
    base_run: str = (
        "previous"  # the run v1 is built on; Live without an older snapshot: latest
    )
    no_older_noted: bool = False

    @property
    def live(self) -> bool:
        return self.issue_date == LIVE

    @property
    def latest_version(self) -> int:
        return max(self.versions, default=0)

    def limit_time(self) -> pd.Timestamp:
        """T for the no-future rule: the issue moment; live — now."""
        if self.live:
            return pd.Timestamp(datetime.now(timezone.utc))
        return to_utc(self.issue_time_utc)

    def published(self, init) -> bool:
        """Contract v0.4 §2: a run counts only once published, init + 8 h <= T.

        Live: the Forecast API serves only published runs, and the weather port stamps a
        Live snapshot with its fetch time, so the check is snapshot time <= now.
        """
        if self.live:
            return to_utc(init) <= self.limit_time()
        return timeline.published_before_issue(
            to_utc(init).to_pydatetime(), self.issue_date
        )

    def unpublished(self, inits) -> list:
        return [init for init in inits if not self.published(init)]

    def event_version(self) -> int:
        return self.current.number if self.current else self.latest_version + 1

    def event_source(self) -> str | None:
        if self.current is not None:
            return self.current.weather.get("source")
        if self.last_run in self.weathers:
            return self.weathers[self.last_run].get("source")
        return None

    def fetch(self, run: str) -> dict:
        if (
            self.live and run == "previous"
        ):  # not an outage: Live may have no older snapshot
            return ports.fetch_weather(self.issue_date, run=run)
        self.fetch_attempts += 1
        if self.scenario == "weather_outage" and self.fetch_attempts == 1:
            raise ConnectionError(
                "источник погоды не отвечает (симуляция отказа Open-Meteo)"
            )
        return ports.fetch_weather(self.issue_date, run=run)


class Tracer:
    """Events per docs/references/trace-event-schema.md, streamed to emit and kept."""

    def __init__(self, issue_date: str, emit: Callable[[dict], None] | None = None):
        self.issue_date = issue_date
        self.events: list[dict] = []
        self._emit = emit or (lambda event: None)

    def event(
        self,
        type_: str,
        title: str,
        body: str = "",
        *,
        tool: str | None = None,
        stage: str | None = None,
        status: str = "ok",
        version: int | None = None,
        source: str | None = None,
        **extra: Any,
    ) -> dict:
        event = {
            "seq": len(self.events) + 1,
            "ts": datetime.now(timeline.LOCAL_TZ).isoformat(timespec="seconds"),
            "type": type_,
            "title": title,
            "body": body,
            "meta": {
                "tool": tool,
                "stage": stage,
                "status": status,
                "issue_date": self.issue_date,
                "version": version,
                "source": source,
                **extra,
            },
        }
        self.events.append(event)
        try:
            self._emit(event)
        except Exception:  # a broken listener must not break the issue
            log.exception("emit failed for event %s", event["seq"])
        return event


# ---------- the registry ----------
@dataclass
class Outcome:
    result: dict
    title: str
    body: str = ""
    status: str = "ok"
    stage: str | None = None
    version: int | None = None


TOOLS: dict[str, dict] = {
    "fetch_weather": {
        "params": ("run",),
        "call_stage": "weather",
        "result_stage": "weather",
    },
    "check_data": {"params": ("run",), "call_stage": "prep", "result_stage": "prep"},
    "run_model": {"params": (), "call_stage": "model", "result_stage": "forecast"},
    "analyze_forecast": {
        "params": (),
        "call_stage": "analysis",
        "result_stage": "analysis",
    },
    "recalc_forecast": {
        "params": ("reason",),
        "call_stage": "recalc",
        "result_stage": "recalc",
    },
    "publish_forecast": {
        "params": ("summary", "version"),
        "call_stage": None,
        "result_stage": None,
    },
}

_WHY = {
    "check_data": "Проверяю полноту и что погода не из будущего",
    "run_model": "Данные в порядке — запускаю модель",
    "analyze_forecast": "Ищу резкие спады, обледенение и ветер выше 20 м/с",
    "recalc_forecast": "Пересчитываю прогноз на свежем прогоне",
    "publish_forecast": "Публикую выпуск и сводку для диспетчера",
}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


_RUN_PROP = {
    "type": "string",
    "enum": list(RUNS),
    "description": "previous — прогон на сутки старше, для v1; latest — самый свежий прогон, опубликованный до момента выпуска T",
}

TOOL_SCHEMAS = [
    _fn(
        "fetch_weather",
        "Погода на окно 48 ч с метками init прогонов (Open-Meteo, архивный прогноз, "
        "доступный на момент выпуска). run=latest дополнительно возвращает ФАКТЫ сравнения "
        "с текущей версией: fresher_than_current, all_inits_before_issue, "
        "mean_abs_wind_shift_h1_24_ms, new_flags_vs_current. Ничего не решает.",
        {"run": _RUN_PROP},
        ["run"],
    ),
    _fn(
        "check_data",
        "Подготовка данных: пропуски, свежесть прогона и правило «без будущего» — каждый "
        "прогон опубликован до момента выпуска T (старт + 8 ч ≤ T). Без run — последняя "
        "полученная погода.",
        {"run": _RUN_PROP},
        [],
    ),
    _fn(
        "run_model",
        "Запуск ML-модели на проверенной погоде прогона previous: P10/P50/P90 по часам × "
        "турбины 1, 2 и ВЭС (версия v1). Числа считает только модель.",
        {},
        [],
    ),
    _fn(
        "analyze_forecast",
        "Анализ текущей версии: рампы ≥ 30 % номинала за 2 ч, обледенение (t от −3 до "
        "+1 °C), ветер > 20 м/с, расхождение погодных моделей, скачок против прошлой версии.",
        {},
        [],
    ),
    _fn(
        "recalc_forecast",
        "Выполняет решение о пересчёте: модель на самом свежем прогоне до T → новая версия "
        "с change_note и анализом рисков (analyze_forecast после него не нужен). Код "
        "откажет, если прогона свежее текущего, опубликованного до T, нет.",
        {
            "reason": {
                "type": "string",
                "description": "почему пересчитываешь — одной фразой",
            }
        },
        ["reason"],
    ),
    _fn(
        "publish_forecast",
        "ДЕЙСТВИЕ: публикует текущую версию — CSV выпуска и запись для диспетчера. "
        "summary — сводка по-русски, 2–3 предложения, числа только из инструментов.",
        {
            "summary": {"type": "string", "description": "сводка для диспетчера"},
            "version": {
                "type": "integer",
                "description": "номер версии (необязательно)",
            },
        },
        ["summary"],
    ),
]


def _call_text(name: str, kwargs: dict) -> str:
    def short(value) -> str:
        text = json.dumps(value, ensure_ascii=False)
        return text if len(text) <= 60 else text[:57] + '…"'

    return f"{name}({', '.join(f'{k}={short(v)}' for k, v in kwargs.items())})"


class ToolRegistry:
    """The six tools over one RunContext; every call is traced."""

    def __init__(self, ctx: RunContext, tracer: Tracer):
        self.ctx = ctx
        self.tracer = tracer
        self._impl: dict[str, Callable[..., Outcome]] = {
            "fetch_weather": self._fetch_weather,
            "check_data": self._check_data,
            "run_model": self._run_model,
            "analyze_forecast": self._analyze_forecast,
            "recalc_forecast": self._recalc_forecast,
            "publish_forecast": self._publish_forecast,
        }

    @staticmethod
    def schemas() -> list[dict]:
        return TOOL_SCHEMAS

    def call(
        self, name: str, args: dict | None = None, *, origin: str = "rule"
    ) -> dict:
        """Run one tool. origin: "llm" (OpenAI tool call) or "rule" (deterministic driver)."""
        ctx = self.ctx
        spec = TOOLS.get(name)
        if spec is None:
            result = {
                "ok": False,
                "error": f"нет инструмента «{name}»; есть: {', '.join(TOOLS)}",
            }
            self.tracer.event(
                "error",
                f"Неизвестный инструмент {name}",
                result["error"],
                tool=name,
                status="error",
                version=ctx.event_version(),
            )
            ctx.grounding.append(json.dumps(result, ensure_ascii=False))
            return result
        kwargs = {k: v for k, v in dict(args or {}).items() if k in spec["params"]}
        published = ctx.current is not None and ctx.current.published
        if (
            name == "recalc_forecast"
            and origin == "llm"
            and published
            and ctx.decision is None
        ):  # the LLM's own decision, traced before it acts
            self.decide(True, by="llm", reason=str(kwargs.get("reason") or ""))
        ctx.tool_calls += 1
        if name == "fetch_weather":
            why = (
                "Смотрю новый прогон погоды и сравниваю с текущей версией"
                if kwargs.get("run") == "latest"
                else "Беру погоду на момент выпуска — без неё модель не запустить"
            )
        else:
            why = _WHY[name]
        self.tracer.event(
            "tool_call",
            why,
            _call_text(name, kwargs),
            tool=name,
            stage=spec["call_stage"],
            status="ok",
            version=ctx.event_version(),
            source=ctx.event_source(),
            args=kwargs,
        )
        try:
            outcome = self._impl[name](origin=origin, **kwargs)
        except ToolError as exc:
            outcome = Outcome(
                {"ok": False, "error": str(exc)}, f"{name}: отказ", str(exc), "error"
            )
        except Exception as exc:
            # Kuba's functions, network, data: reported in the trace, never hidden
            log.exception("tool %s failed", name)
            text = f"{type(exc).__name__}: {exc}"
            outcome = Outcome(
                {"ok": False, "error": text}, f"{name}: ошибка", text, "error"
            )
        published = name == "publish_forecast" and outcome.result.get("ok")
        self.tracer.event(
            "action" if published else "tool_result",
            outcome.title,
            outcome.body,
            tool=name,
            stage=outcome.stage or spec["result_stage"],
            status=outcome.status,
            version=outcome.version or ctx.event_version(),
            source=ctx.event_source(),
        )
        ctx.grounding.append(
            json.dumps(outcome.result, ensure_ascii=False, default=str)
        )
        return outcome.result

    def decide(
        self,
        recalc: bool,
        *,
        by: str,
        reason: str = "",
        status: str | None = None,
        title: str | None = None,
        keep_reason: str | None = None,
    ) -> dict:
        """Record and trace the agent's recalculation decision (LLM's or the rule's)."""
        ctx = self.ctx
        number = ctx.current.number if ctx.current else ctx.latest_version
        title = title or decision_title(ctx.facts, recalc, number, ctx.live)
        facts = ctx.facts or {}
        lines = []
        detail = ""
        if facts and not facts.get("all_inits_before_issue", True):
            late = ctx.unpublished(ctx.weathers["latest"]["hourly"]["init_time_utc"])
            if late:
                detail = late_run_text(max(late), ctx.limit_time())
        elif facts and not facts.get("fresher_than_current"):
            detail = no_fresher_text(ctx)
        if detail:
            detail = detail[0].upper() + detail[1:] + "."
            lines.append(detail)
        if reason:
            lines.append(reason)
        if facts:
            fresh = facts.get("new_flags_vs_current") or []
            lines.append(
                "Факты: прогон свежее текущего — "
                f"{'да' if facts.get('fresher_than_current') else 'нет'}; опубликован до T — "
                f"{'да' if facts.get('all_inits_before_issue') else 'нет'}; средний сдвиг "
                f"ветра за часы 1–24 — {fmt1(facts.get('mean_abs_wind_shift_h1_24_ms') or 0)}"
                f" м/с; новых рисков — {len(fresh)}."
            )
        if by == "rule" and facts:
            lines.append(
                "Политика: пересчёт, если прогон свежее текущего, опубликован до T и средний сдвиг "
                "ветра за часы 1–24 больше 0,5 м/с или появился новый риск."
            )
        ctx.decision = {
            "recalc": recalc,
            "by": by,
            "reason": reason or title,
            "title": title,
            "detail": detail,
            "keep_reason": keep_reason,
        }
        self.tracer.event(
            "thought",
            title,
            " ".join(lines),
            stage="recalc",
            status=status or ("ok" if recalc else "skip"),
            version=number,
            source=ctx.event_source(),
            decision="recalc" if recalc else "keep",
            by=by,
        )
        return ctx.decision

    # ----- tools -----
    def _fetch_weather(self, run: str = "previous", *, origin: str) -> Outcome:
        ctx = self.ctx
        if run not in RUNS:
            raise ToolError(
                'run: "previous" (прогон для v1) или "latest" (самый свежий до T)'
            )
        try:
            raw = ctx.fetch(run)
        except Exception as exc:  # noqa: BLE001 — any weather-port failure: source unavailable
            if (
                run == "previous"
                and ctx.live
                and ctx.trigger == "issue"
                and not ctx.versions
            ):
                return self._live_latest_as_base(exc)
            return _source_unavailable(run, exc)
        return self._weather_outcome(run, normalize_weather(raw))

    def _live_latest_as_base(self, exc: Exception) -> Outcome:
        """Live has no older snapshot: v1 is built on the latest run instead."""
        ctx = self.ctx
        if not ctx.no_older_noted:
            ctx.no_older_noted = True
            self.tracer.event(
                "thought",
                "Для Live нет более старого снимка — выпускаю v1 на последнем прогоне",
                f"Источник погоды: {type(exc).__name__}: {exc}. v1 строю на последнем "
                "прогоне; более свежий проверю при следующем обновлении погоды.",
                stage="weather",
                status="ok",
                version=ctx.event_version(),
            )
        try:
            raw = ctx.fetch("latest")
        except Exception as error:  # noqa: BLE001 — any weather-port failure
            return _source_unavailable("latest", error)
        ctx.base_run = "latest"
        outcome = self._weather_outcome("latest", normalize_weather(raw))
        outcome.result["requested_run"] = "previous"
        outcome.result["note"] = (
            "для Live нет более старого снимка — v1 на последнем прогоне"
        )
        outcome.body = (
            "Более старого снимка нет — взят последний прогон. " + outcome.body
        )
        return outcome

    def _weather_outcome(self, run: str, weather: dict) -> Outcome:
        ctx = self.ctx
        ctx.weathers[run] = weather
        ctx.last_run = run
        limit = ctx.limit_time()
        hourly = weather["hourly"]
        runs = weather_runs(weather, ctx.published)
        all_before = not ctx.unpublished(hourly["init_time_utc"])
        wind = hourly["wind_100m_ms"].astype(float)
        temp = hourly["temp_c"].astype(float)
        result = {
            "ok": True,
            "run": run,
            "source": weather["source"],
            "hours": len(hourly),
            "runs": runs,
            "all_inits_before_issue": all_before,
            "issue_time_utc": timeline.iso_utc(limit),
            "wind_ms": {
                "min": _r(wind.min(), 1),
                "mean": _r(wind.mean(), 1),
                "max": _r(wind.max(), 1),
            },
            "temp_c": {"min": _r(temp.min(), 1), "max": _r(temp.max(), 1)},
        }
        runs_text = "; ".join(
            f"часы {r['hours']} — {r['model']}, прогон {utc_label(r['init_utc'])} "
            f"{'✓ опубликован до T' if r['before_issue'] else '✗ опубликован после T'}"
            for r in runs
        )
        body = (
            f"{runs_text}. Ветер {fmt1(wind.min())}–{fmt1(wind.max())} м/с, в среднем "
            f"{fmt1(wind.mean())}; t от {fmt1(temp.min())} до {fmt1(temp.max())} °C. "
            f"Источник: {weather['source']}."
        )
        title = f"Погода получена: {len(hourly)} ч, источник {weather['source']}"
        status = "ok" if all_before else "warn"
        if run == "latest":
            facts = self._compare_latest(weather, all_before)
            result.update(facts)
            if facts.get("current_version"):
                shift = facts["mean_abs_wind_shift_h1_24_ms"]
                fresh = facts["new_flags_vs_current"]
                title = (
                    f"Новый прогон: {'свежее' if facts['fresher_than_current'] else 'не свежее'}"
                    f" v{facts['current_version']}, сдвиг ветра {fmt1(shift)} м/с"
                )
                body += (
                    f" Против v{facts['current_version']}: средний сдвиг ветра за часы 1–24 "
                    f"{signed1(facts['mean_wind_shift_h1_24_ms'])} м/с (по модулю "
                    f"{fmt1(shift)}); новых рисков: {len(fresh)}"
                    + (f" ({'; '.join(f['text'] for f in fresh)})" if fresh else "")
                    + "."
                )
        return Outcome(result, title, body, status)

    def _compare_latest(self, weather: dict, all_before: bool) -> dict:
        """Facts of the latest run against the current version's weather — no decision."""
        ctx = self.ctx
        base = ctx.current
        if base is None:
            facts = {"current_version": None, "fresher_than_current": None}
            ctx.facts = None
            return facts
        cols = ["target_time_utc", "h", "wind_100m_ms", "init_time_utc"]
        merged = weather["hourly"][cols].merge(
            base.weather["hourly"][cols], on="target_time_utc", suffixes=("", "_cur")
        )
        if len(merged):
            fresher = bool(
                (merged["init_time_utc"] > merged["init_time_utc_cur"]).any()
            )
        else:  # Live: the current version no longer overlaps the new window
            newest = base.weather["hourly"]["init_time_utc"].max()
            fresher = bool(weather["hourly"]["init_time_utc"].max() > newest)
        head = merged[merged["h"] <= SHIFT_HORIZON_H]
        delta = head["wind_100m_ms"].astype(float) - head["wind_100m_ms_cur"].astype(
            float
        )
        fresh = new_flags(weather_flags(weather), weather_flags(base.weather))
        facts = {
            "current_version": base.number,
            "fresher_than_current": fresher,
            "all_inits_before_issue": all_before,
            "mean_abs_wind_shift_h1_24_ms": _r(delta.abs().mean())
            if len(delta)
            else 0.0,
            "mean_wind_shift_h1_24_ms": _r(delta.mean()) if len(delta) else 0.0,
            "new_flags_vs_current": fresh,
            "overlap_hours": len(merged),
            "current_runs": weather_runs(base.weather, ctx.published),
        }
        ctx.facts = facts
        return facts

    def _check_data(self, run: str | None = None, *, origin: str) -> Outcome:
        ctx = self.ctx
        run = run or ctx.last_run
        if run not in ctx.weathers:
            raise ToolError("Сначала fetch_weather — проверять нечего")
        weather = ctx.weathers[run]
        report = dict(ports.check_data(ctx.issue_date, weather) or {})
        limit = ctx.limit_time()
        hourly = weather["hourly"]
        late = ctx.unpublished(hourly["init_time_utc"])
        after = len(late)
        notes = [str(n) for n in report.get("notes") or []]
        if late:
            notes.append(f"{after} ч: {late_run_text(max(late), limit)} — брать нельзя")
        complete = len(hourly) == HORIZON and set(hourly["h"]) == set(
            range(1, HORIZON + 1)
        )
        if not complete:
            notes.append(f"в окне {len(hourly)} ч вместо {HORIZON}")
        missing = int(report.get("missing_hours") or 0)
        ok = bool(report.get("ok", True)) and after == 0 and complete
        result = {
            "ok": ok,
            "run": run,
            "missing_hours": missing,
            "runs_before_issue": bool(report.get("runs_before_issue", True))
            and after == 0,
            "hours_after_issue": after,
            "issue_time_utc": timeline.iso_utc(limit),
            "init_max_utc": timeline.iso_utc(hourly["init_time_utc"].max()),
            "notes": notes,
        }
        ctx.checks[run] = result
        if ok:
            title = f"Данные в порядке: {HORIZON - missing} из {HORIZON} ч, прогоны опубликованы до T"
        else:
            title = "Данные непригодны: " + (
                notes[-1] if notes else "проверка не пройдена"
            )
        body = (
            "; ".join(notes)
            + f". Момент выпуска T = {utc_label(limit)}; самый свежий прогон стартовал "
            f"{utc_label(hourly['init_time_utc'].max())} (публикация ≈ через 8 ч)."
        )
        return Outcome(result, title, body, "ok" if ok else "error")

    def _run_model(self, *, origin: str) -> Outcome:
        ctx = self.ctx
        if ctx.versions:
            raise ToolError(
                f"v{ctx.latest_version} уже опубликована; новый прогон — через "
                'fetch_weather(run="latest") и recalc_forecast'
            )
        base = ctx.base_run
        weather = ctx.weathers.get(base)
        if weather is None:
            raise ToolError(
                'Сначала fetch_weather(run="previous") — v1 строится на нём'
            )
        check = ctx.checks.get(base)
        if check is None:
            raise ToolError(
                f'Сначала check_data(run="{base}") — полнота и «без будущего»'
            )
        if not check["ok"]:
            raise ToolError("Погода не прошла check_data — модель на ней не запускаю")
        frame, touched = sanitize_forecast(ports.predict(ctx.issue_date, weather))
        ctx.current = Version(number=1, frame=frame, weather=weather, run=base)
        stats = plant_stats(frame)
        model_version = ports.model_version()
        result = {
            "ok": True,
            "version": 1,
            "run": base,
            "rows": len(frame),
            "model_version": model_version,
            "plant": stats,
            "turbine_mean_pct": {
                t: pct(frame.loc[frame["turbine"] == t, "p50"].mean()) for t in TURBINES
            },
        }
        run_ru = "последний прогон" if base == "latest" else "предыдущий прогон"
        body = f"Модель {model_version}, {run_ru}. {_stats_text(stats)}"
        if touched:
            body += (
                f" Поправлено строк (обрезка в [0, 1], порядок квантилей): {touched}."
            )
        title = "Прогноз готов: 48 ч × Т1, Т2, ВЭС · P10/P50/P90"
        return Outcome(result, title, body, "ok", version=1)

    def _analyze_forecast(self, *, origin: str) -> Outcome:
        ctx = self.ctx
        version = ctx.current
        if version is None:
            raise ToolError("Анализировать нечего — сначала run_model")
        analysis = analyze_version(version, ctx.versions.get(version.number - 1))
        flags = analysis["flags"]
        result = {"ok": True, **analysis}
        if flags:
            kinds = ", ".join(sorted({KIND_RU[f["kind"]] for f in flags}))
            title = f"Рисков: {len(flags)} — {kinds}"
            body = "; ".join(f["text"] for f in flags) + "."
        else:
            title = "Рисков не найдено"
            body = "Рамп, обледенения и ветра выше 20 м/с нет."
        jump = analysis["jump_vs_previous"]
        if jump:
            body += (
                f" Против v{jump['vs_version']}: пик {signed_int(jump['peak_delta_pp'])} п.п., "
                f"в среднем {signed_int(jump['mean_delta_pp'])} п.п., наибольшее изменение "
                f"{jump['max_abs_delta_pp']} п.п. — {jump['max_delta_at_local']}."
            )
        if analysis["skipped_checks"]:
            body += " Расхождение погодных моделей не проверял — нет второй модели."
        return Outcome(
            result, title, body, "warn" if flags else "ok", version=version.number
        )

    def _recalc_forecast(self, reason: str = "", *, origin: str) -> Outcome:
        ctx = self.ctx
        base = ctx.current
        if base is None or not base.published:
            raise ToolError(
                "Сначала опубликуйте текущую версию (publish_forecast), потом пересчитывайте"
            )
        decided = ctx.decision or {}
        if (
            origin == "llm"
            and decided.get("by") == "rule"
            and not decided.get("recalc")
        ):
            raise ToolError(
                f"Решение по новому прогону уже принято: {decided['title']}"
            )
        weather = ctx.weathers.get("latest")
        if weather is None:
            try:
                raw = ctx.fetch("latest")
            except Exception as exc:  # noqa: BLE001 — any weather-port failure
                text = f"источник погоды недоступен — {type(exc).__name__}: {exc}"
                result = {
                    "ok": False,
                    "status": "error",
                    "error": text,
                    "version": base.number,
                }
                ctx.recalcs.append(result)
                return Outcome(
                    result,
                    "Новый прогон недоступен — пересчёт не выполнен",
                    f"{text[0].upper()}{text[1:]}. v{base.number} остаётся.",
                    "warn",
                    version=base.number,
                )
            weather = normalize_weather(raw)
            ctx.weathers["latest"] = weather
        limit = ctx.limit_time()
        late = ctx.unpublished(weather["hourly"]["init_time_utc"])
        facts = self._compare_latest(weather, not late)
        refusal = None
        if late:
            refusal = late_run_text(max(late), limit) + " (правило «без будущего»)"
        elif not facts["fresher_than_current"]:
            refusal = no_fresher_text(ctx)
        else:
            check = dict(ports.check_data(ctx.issue_date, weather) or {})
            if not check.get("ok", True):
                refusal = "свежий прогон не прошёл проверку данных: " + "; ".join(
                    str(n) for n in check.get("notes") or []
                )
        if refusal:
            result = {
                "ok": False,
                "status": "refused",
                "error": refusal,
                "version": base.number,
            }
            ctx.recalcs.append(result)
            title = "Отказ: " + refusal.split(" — ")[0]
            body = f"{refusal[0].upper()}{refusal[1:]}. v{base.number} остаётся."
            return Outcome(result, title, body, "skip", version=base.number)
        frame, touched = sanitize_forecast(ports.predict(ctx.issue_date, weather))
        number = ctx.latest_version + 1
        candidate = Version(number=number, frame=frame, weather=weather, run="latest")
        analysis = analyze_version(candidate, base)
        candidate.change_note = change_note(
            facts["mean_wind_shift_h1_24_ms"], candidate.jump, base.number
        )
        ctx.current = candidate
        result = {
            "ok": True,
            "status": "recalculated",
            "version": number,
            "reason": reason,
            "change_note": candidate.change_note,
            "model_version": ports.model_version(),
            **{
                k: analysis[k] for k in ("flags", "counts", "jump_vs_previous", "plant")
            },
        }
        ctx.recalcs.append(result)
        risks = risk_digest(candidate) or "не найдено"
        body = (
            f"Модель {result['model_version']} на свежем прогоне. {_stats_text(analysis['plant'])}"
            f" Риски: {risks}."
        )
        if touched:
            body += f" Поправлено строк: {touched}."
        title = f"Пересчитал → v{number}: {candidate.change_note}"
        return Outcome(result, title, body, "ok", version=number)

    def _publish_forecast(
        self, summary: str = "", version: Any = None, *, origin: str
    ) -> Outcome:
        ctx = self.ctx
        current = ctx.current
        if current is None:
            raise ToolError("Публиковать нечего — сначала run_model")
        if current.published:
            raise ToolError(
                f"v{current.number} уже опубликована; новая версия — только после recalc_forecast"
            )
        if current.flags is None:
            analyze_version(current, ctx.versions.get(current.number - 1))
        text = str(summary or "").strip()
        notes = []
        if origin == "llm":
            bad = ungrounded_numbers(text, ctx.grounding)
            if not text:
                notes.append("сводка пустая — взял шаблонную")
            elif bad:
                notes.append(
                    f"в сводке числа не из инструментов ({', '.join(bad[:4])}) — взял шаблонную"
                )
            if notes:
                text = summary_text(current)
        elif not text:
            text = summary_text(current)
        if version is not None and str(version).lstrip("vV") != str(current.number):
            notes.append(f"номер версии назначен по журналу: v{current.number}")
        entry = {
            "version": current.number,
            "created_at": store.now_local(),
            "weather_runs": weather_runs(current.weather, ctx.published),
            "source": current.weather["source"],
            "change_note": current.change_note,
            "summary": text,
            "flags": current.flags,
            "rows": store.forecast_rows(current.frame),
            "model_version": ports.model_version(),
        }
        if ctx.record is None:
            ctx.record = store.new_record(
                ctx.issue_date, ctx.issue_time_utc, ctx.effective_mode
            )
        ctx.record["mode"] = ctx.effective_mode
        record_file, csv_file = store.save_version(
            ctx.record, entry, init_by_h(current.weather)
        )
        current.published = True
        current.summary = text
        ctx.versions[current.number] = current
        result = {
            "ok": True,
            "version": current.number,
            "rows": len(entry["rows"]),
            "csv": store.relative(csv_file),
            "record": store.relative(record_file),
            "summary": text,
            "notes": notes,
        }
        body = f"{store.relative(csv_file)} · строк: {len(entry['rows'])} · {text}"
        if notes:
            body += " (" + "; ".join(notes) + ")"
        return Outcome(
            result,
            f"Опубликован выпуск v{current.number}",
            body,
            "warn" if notes else "ok",
            version=current.number,
        )
