"""Deterministic generator of the frontend fixtures: one JSON per API response (CONTRACT §6).

The numbers are SYNTHETIC: a toy power curve over seeded pseudo-weather. They exist so the
frontend can build every screen before the backend is ready. They are NOT model output.

    python frontend/fixtures/_generate.py      # rewrites every *.json next to this file

Standard library only; the same code gives byte-identical files. The script ends by reading
every file back and asserting its shape against docs/CONTRACT.md §6 and
docs/references/trace-event-schema.md. Time conventions mirror backend/windcast/timeline.py:
issue D is made at T = (D+1) 00:00 local (UTC+5) = D 19:00 UTC; target hour h starts at
T + (h - 1) h.
"""

from __future__ import annotations

import json
import math
import random
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent
UTC = timezone.utc
LOCAL = timezone(timedelta(hours=5))
HORIZON = 48
TURBINES = ("1", "2", "plant")
WEATHER_MODEL = "ecmwf_ifs025"
MODEL_VERSION = "lgbm-q-2026-01-31"
FLAG_KINDS = ("ramp", "ice", "wind_gt20", "models_diverge")
STAGES = ("weather", "prep", "model", "forecast", "analysis", "recalc")
STATUSES = ("ok", "warn", "skip", "error")
EVENT_TYPES = ("thought", "tool_call", "tool_result", "action", "verdict", "error")
EVENT_KEYS = ["seq", "ts", "type", "title", "body", "meta"]
META_KEYS = [
    "tool",
    "args",
    "artifact",
    "stage",
    "status",
    "issue_date",
    "version",
    "source",
]
FORECAST_KEYS = [
    "issue_date",
    "issue_time_local",
    "issue_time_utc",
    "version",
    "versions",
    "weather_runs",
    "change_note",
    "summary",
    "flags",
    "rows",
]
ROW_KEYS = [
    "h",
    "target_time_local",
    "turbine",
    "p10",
    "p50",
    "p90",
    "wind_fc_ms",
    "temp_fc_c",
    "actual",
]
MAX_STEPS = 8
MINUS = "\u2212"

ISSUES = [date(2026, 1, 31) + timedelta(days=i) for i in range(29)]
DEMO_DAY = date(2026, 2, 13)
# Issues where the fresher run moved the wind enough for a v2: mean shift v2 - v1 over h 1-24.
V2_SHIFT = {
    date(2026, 2, 3): 0.8,
    date(2026, 2, 8): -1.1,
    date(2026, 2, 13): 0.9,
    date(2026, 2, 19): -0.8,
    date(2026, 2, 24): 1.3,
    date(2026, 2, 27): -0.7,
}
# Extra shapes so every flag kind and a warn state appear somewhere in February.
DIVERGE = {
    date(2026, 2, 6): (28, 37),
    date(2026, 2, 19): (19, 27),
    date(2026, 2, 26): (36, 44),
}
STORM = {date(2026, 2, 21): 32}  # h of a wind peak above 20 m/s
FRONT = {date(2026, 2, 4): (30, 5.5)}  # h, wind step in m/s
MISSING_TEMP = {date(2026, 2, 9): (17, 18)}  # check_data interpolates -> status warn

RECORDED_FROM = datetime(2026, 9, 23, 13, 10, 4, tzinfo=LOCAL)
RUN_ISSUE_AT = datetime(2026, 9, 23, 14, 38, 2, tzinfo=LOCAL)
RUN_NEW_WEATHER_AT = datetime(2026, 9, 23, 14, 41, 15, tzinfo=LOCAL)
LIVE_NOW = datetime(2026, 9, 23, 14, 37, tzinfo=LOCAL)
LIVE_ISSUED = datetime(2026, 9, 23, 14, 31, tzinfo=LOCAL)
LIVE_PREV_RUN = datetime(2026, 9, 22, 18, tzinfo=UTC)
LIVE_LATEST_RUN = datetime(2026, 9, 23, 0, tzinfo=UTC)
RUN_AVAILABLE_AFTER = timedelta(hours=6, minutes=40)  # ECMWF open data delay

METRICS_FROM, METRICS_TO = date(2025, 12, 31), date(2026, 1, 29)
SERIES_FROM, SERIES_DAYS = date(2026, 1, 15), 7


# ---------- formatting ----------
def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def r4(value: float) -> float:
    return round(value, 4) + 0.0  # + 0.0 turns -0.0 into 0.0


def r1(value: float) -> float:
    return round(value, 1) + 0.0


def iso_local(dt: datetime) -> str:
    return dt.astimezone(LOCAL).strftime("%Y-%m-%dT%H:%M+05:00")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def iso_ts(dt: datetime) -> str:
    return dt.astimezone(LOCAL).strftime("%Y-%m-%dT%H:%M:%S+05:00")


def parse_local(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M+05:00").replace(tzinfo=LOCAL)


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)


def issue_time_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 19, tzinfo=UTC)


def next_full_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def targets(first: datetime) -> list[datetime]:
    return [first + timedelta(hours=k) for k in range(HORIZON)]


def ddm(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}"


def dm(dt: datetime) -> str:
    return ddm(dt.astimezone(LOCAL).date())


def hm(dt: datetime) -> str:
    return f"{dm(dt)} {dt.astimezone(LOCAL).hour:02d}:00"


def hm_utc(dt: datetime) -> str:
    x = dt.astimezone(UTC)
    return f"{x.day:02d}.{x.month:02d} {x.hour:02d}:00 UTC"


def span(a: datetime, b: datetime) -> str:
    if dm(a) == dm(b):
        return f"{hm(a)}–{b.astimezone(LOCAL).hour:02d}:00"
    return f"{hm(a)} – {hm(b)}"


def dec1(value: float) -> str:
    return f"{abs(value):.1f}".replace(".", ",")


def signed1(value: float) -> str:
    value = round(value, 1)
    return ("+" if value >= 0 else MINUS) + dec1(value)


def signed_int(value: float) -> str:
    value = round(value)
    return ("+" if value >= 0 else MINUS) + str(abs(value))


def temp_text(value: float) -> str:
    return "0,0" if abs(value) < 0.05 else signed1(value)


def pct(value: float) -> str:
    return f"{round(value * 100)} %"


def plural_risk(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "риск"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "риска"
    return "рисков"


def dumps(obj: object) -> str:
    """JSON with one row / event / item per line: readable and diff-friendly."""

    def one(value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    if isinstance(obj, list):
        if not obj:
            return "[]\n"
        return "[\n" + ",\n".join("  " + one(v) for v in obj) + "\n]\n"
    assert isinstance(obj, dict)
    lines = []
    for key, value in obj.items():
        if (
            isinstance(value, list)
            and value
            and all(isinstance(x, dict) for x in value)
        ):
            inner = ",\n".join("    " + one(x) for x in value)
            lines.append(f"  {one(key)}: [\n{inner}\n  ]")
        else:
            lines.append(f"  {one(key)}: {one(value)}")
    return "{\n" + ",\n".join(lines) + "\n}\n"


# ---------- synthetic physics ----------
def power_curve(wind_ms: float) -> float:
    """Toy curve: cut-in 3 m/s, ~0.56 at 8.5 m/s, rated from 11.5 m/s, cut-out above 25."""
    if wind_ms < 3.0 or wind_ms > 25.0:
        return 0.0
    if wind_ms >= 11.5:
        return 1.0
    return ((wind_ms - 3.0) / 8.5) ** 1.35


def pseudo_weather(
    seed: int, n: int, first_local_hour: int, t_base: float, t_amp: float
) -> tuple[list[float], list[float]]:
    """Wind at 100 m (m/s) and temperature (°C): a synoptic wave + diurnal cycle + noise."""
    r = random.Random(seed)
    base, amp = 3.5 + 5.0 * r.random(), 2.0 + 3.5 * r.random()
    period, phase = 24.0 + 24.0 * r.random(), 48.0 * r.random()
    drift = r.uniform(-2.0, 2.0)
    raw = [(r.random() - 0.5) * 1.8 for _ in range(n)]
    wind, temp = [], []
    for k in range(n):
        noise = sum(raw[min(n - 1, max(0, k + j))] for j in (-2, -1, 0, 1, 2)) / 5
        lh = (first_local_hour + k) % 24
        w = base + amp * math.sin(2 * math.pi * (k + phase) / period)
        w += 1.1 * math.sin(2 * math.pi * (lh - 14) / 24) + noise
        wind.append(0.5 * (w + math.sqrt(w * w + 2.0)))  # soft floor: calm, never < 0
        t = t_base + t_amp * math.sin(2 * math.pi * (lh - 9) / 24)
        temp.append(t + drift * k / n + 0.6 * noise)
    return wind, temp


def demo_weather() -> tuple[list[float], list[float]]:
    """13.02, the demo day: thaw night at icing risk, rated wind until a cold front passes
    in the afternoon of 14.02 (one sharp ramp down), colder and calmer after, slow recovery."""
    r = random.Random(1313)
    raw = [(r.random() - 0.5) * 0.8 for _ in range(HORIZON)]
    wind, temp = [], []
    for k in range(HORIZON):
        h, lh = k + 1, k % 24
        noise = (raw[max(0, k - 1)] + raw[k] + raw[min(HORIZON - 1, k + 1)]) / 3
        front = 1 / (1 + math.exp(-(h - 15) / 0.7))
        w = 4.6 + 6.2 * (1 - front) + 3.2 * math.exp(-(((h - 38) / 7.0) ** 2))
        wind.append(w + 0.4 * math.sin(2 * math.pi * (lh - 14) / 24) + noise)
        cold = 5.5 / (1 + math.exp(-(h - 16) / 1.5))
        t = -0.5 + 2.2 * math.sin(2 * math.pi * (lh - 9) / 24) - cold
        temp.append(t + 0.5 * noise)
    return wind, temp


def february_weather(i: int, d: date) -> tuple[list[float], list[float]]:
    if d == DEMO_DAY:
        wind, temp = demo_weather()
        return [clamp(w, 0.3, 23.5) for w in wind], [clamp(t, -15.0, 3.0) for t in temp]
    r = random.Random(700 + i)
    t_base = -10.0 + 7.0 * i / 28 + r.gauss(0.0, 2.0)
    t_amp = 2.5 + 1.5 * r.random()
    wind, temp = pseudo_weather(100 + i, HORIZON, 0, t_base, t_amp)
    if d in FRONT:
        c, step = FRONT[d]
        wind = [
            w + step / (1 + math.exp(-(k + 1 - c) / 0.9)) for k, w in enumerate(wind)
        ]
    if d in STORM:
        c = STORM[d] - 1
        bump = 21.4 - wind[c]
        wind = [
            w + bump * math.exp(-(((k - c) / 3.2) ** 2)) for k, w in enumerate(wind)
        ]
    return [clamp(w, 0.3, 23.5) for w in wind], [clamp(t, -15.0, 3.0) for t in temp]


def build_rows(
    times: list[datetime],
    wind: list[float],
    temp: list[float],
    noise: list[list[float]],
    widen: tuple[int, int] | None,
) -> list[dict]:
    rows = []
    for k, t in enumerate(times):
        base = power_curve(wind[k]) * 0.96  # availability
        factor = 1.6 if widen and widen[0] <= k + 1 <= widen[1] else 1.0
        q = {}
        for j, (turbine, scale) in enumerate((("1", 1.03), ("2", 0.97))):
            p = clamp(base * scale * (1 + noise[j][k]))
            spread = (0.05 + 0.09 * k / 47 + 0.28 * p * (1 - p)) * factor
            q[turbine] = (r4(clamp(p - 1.1 * spread)), r4(p), r4(clamp(p + spread)))
        # plant = mean of the two turbines' normalised power (CONTRACT §3)
        q["plant"] = tuple(r4((q["1"][m] + q["2"][m]) / 2) for m in range(3))
        for turbine in TURBINES:
            p10, p50, p90 = q[turbine]
            rows.append(
                {
                    "h": k + 1,
                    "target_time_local": iso_local(t),
                    "turbine": turbine,
                    "p10": p10,
                    "p50": p50,
                    "p90": p90,
                    "wind_fc_ms": r1(wind[k]),
                    "temp_fc_c": r1(temp[k]),
                    "actual": None,
                }
            )
    return rows


def plant(rows: list[dict], key: str = "p50") -> list[float]:
    return [row[key] for row in rows if row["turbine"] == "plant"]


def detect_flags(
    times: list[datetime],
    rows: list[dict],
    diverge: tuple[int, int] | None,
    diverge_ms: float,
) -> list[dict]:
    p50 = plant(rows)
    wind, temp = plant(rows, "wind_fc_ms"), plant(rows, "temp_fc_c")
    flags = []
    k = 0
    while k <= HORIZON - 3:
        if abs(p50[k + 2] - p50[k]) < 0.3:
            k += 1
            continue
        up = p50[k + 2] > p50[k]
        best = max(
            (
                j
                for j in range(k, min(k + 3, HORIZON - 2))
                if (p50[j + 2] > p50[j]) == up
            ),
            key=lambda j: abs(p50[j + 2] - p50[j]),
        )
        dp = p50[best + 2] - p50[best]
        word = "рост" if dp > 0 else "спад"
        flags.append(
            {
                "kind": "ramp",
                "from_h": best + 1,
                "to_h": best + 3,
                "text": f"{word} {signed_int(dp * 100)} % за 2 ч",
            }
        )
        k = best + 5

    def windows(test) -> list[tuple[int, int]]:
        out, start = [], None
        for h in range(HORIZON + 1):
            hit = h < HORIZON and test(h)
            if hit and start is None:
                start = h
            if not hit and start is not None:
                out.append((start, h - 1))
                start = None
        return out

    def night(h: int) -> bool:
        lh = times[h].astimezone(LOCAL).hour
        return lh >= 21 or lh <= 8

    def icy(h: int) -> bool:
        return -3.0 <= temp[h] <= 1.0 and night(h)

    nights: list[list[int]] = []  # icing windows of one night merged: gaps <= 8 h
    for a, b in windows(icy):
        if nights and a - nights[-1][1] <= 8:
            nights[-1][1] = b
        else:
            nights.append([a, b])
    for a, b in nights:
        cold = [temp[h] for h in range(a, b + 1) if icy(h)]
        if len(cold) >= 2:
            lo, hi = temp_text(min(cold)), temp_text(max(cold))
            text = f"риск обледенения: t от {lo} до {hi} °C ночью"
            flags.append({"kind": "ice", "from_h": a + 1, "to_h": b + 1, "text": text})
    for a, b in windows(lambda h: wind[h] > 20.0):
        top = dec1(max(wind[a : b + 1]))
        text = f"ветер до {top} м/с — выше 20 м/с, возможна остановка по защите"
        flags.append(
            {"kind": "wind_gt20", "from_h": a + 1, "to_h": b + 1, "text": text}
        )
    if diverge:
        text = (
            f"ECMWF и ICON расходятся до {dec1(diverge_ms)} м/с — коридор шире обычного"
        )
        flags.append(
            {
                "kind": "models_diverge",
                "from_h": diverge[0],
                "to_h": diverge[1],
                "text": text,
            }
        )
    return sorted(flags, key=lambda f: (f["from_h"], FLAG_KINDS.index(f["kind"])))


def summary_text(times: list[datetime], rows: list[dict], flags: list[dict]) -> str:
    """Two or three sentences for the dispatcher: peak, minimum, average, risks."""
    p50 = plant(rows)
    mx = max(range(HORIZON), key=lambda k: (p50[k], -k))
    mn = min(range(HORIZON), key=lambda k: (p50[k], k))
    peak = f"Пик {pct(p50[mx])} номинала — {hm(times[mx])}"
    low = f"минимум {pct(p50[mn])} — {hm(times[mn])}"
    parts = [
        f"{peak}, {low}.",
        f"В среднем за 48 ч — {pct(sum(p50) / HORIZON)} номинала.",
    ]
    risks = []
    ramps = [f for f in flags if f["kind"] == "ramp"]
    if ramps:
        f = ramps[0]
        down = f["text"].startswith("спад")
        what = "резкий спад" if down else "резкий рост"
        todo = "держать резерв" if down else "учесть в заявке"
        more = f" (перепадов за 48 ч: {len(ramps)})" if len(ramps) > 1 else ""
        risks.append(f"{what} около {hm(times[f['from_h'] - 1])} — {todo}{more}")
    for f in flags:
        at = times[f["from_h"] - 1]
        if f["kind"] == "ice":
            risks.append(f"ночью {dm(at)} риск обледенения лопастей — возможны потери")
            break
    for f in flags:
        at = times[f["from_h"] - 1]
        if f["kind"] == "wind_gt20":
            risks.append(
                f"около {hm(at)} ветер выше 20 м/с — возможна остановка по защите"
            )
        if f["kind"] == "models_diverge":
            risks.append(
                f"с {hm(at)} погодные модели расходятся — коридор P10–P90 шире"
            )
    if risks:
        text = "; ".join(risks)
        parts.append(text[0].upper() + text[1:] + ".")
    else:
        parts.append(
            "Резких перепадов и рисков не видно — заявку можно подавать по P50."
        )
    return " ".join(parts)


def flags_brief(times: list[datetime], flags: list[dict]) -> str:
    if not flags:
        return "рисков нет"
    return "; ".join(
        f"{f['text']} ({span(times[f['from_h'] - 1], times[f['to_h'] - 1])})"
        for f in flags
    )


def weather_runs(first_run: datetime, second_run: datetime, t: datetime) -> list[dict]:
    return [
        {
            "hours": hours,
            "model": WEATHER_MODEL,
            "init_utc": iso_utc(run),
            "before_issue": run <= t,
        }
        for hours, run in (("1-24", first_run), ("25-48", second_run))
    ]


# ---------- February issues ----------
def make_issue(i: int, d: date) -> dict:
    t = issue_time_utc(d)
    times = targets(t)
    fresh_run = datetime(d.year, d.month, d.day, tzinfo=UTC)  # previous_day1, h 1-24
    prev_run = fresh_run - timedelta(days=1)  # previous_day2
    r = random.Random(900 + i)
    noise = [[r.uniform(-0.03, 0.03) for _ in range(HORIZON)] for _ in range(2)]
    diverge = DIVERGE.get(d)
    diverge_ms = 2.6 + 1.2 * r.random()
    wind, temp = february_weather(i, d)

    def version(n: int, w: list[float], tc: list[float], runs: list[dict]) -> dict:
        rows = build_rows(times, w, tc, noise, diverge)
        flags = detect_flags(times, rows, diverge, diverge_ms)
        return {
            "version": n,
            "weather_runs": runs,
            "change_note": None,
            "summary": summary_text(times, rows, flags),
            "flags": flags,
            "rows": rows,
            "wind": w,
            "temp": tc,
        }

    issue = {"i": i, "date": d, "t": t, "times": times, "fresh_run": fresh_run}
    issue["prev_run"] = prev_run
    shift = V2_SHIFT.get(d)
    if shift is None:
        # v1 on previous_day2 stays: the fresher run moves the wind less than 0.5 m/s
        issue["small_shift"] = r.uniform(0.05, 0.45) * r.choice((1, -1))
        issue["versions"] = {
            1: version(1, wind, temp, weather_runs(prev_run, prev_run, t))
        }
        return issue
    e = [r.gauss(0.0, 0.35) for _ in range(24)]
    mean_e = sum(e) / 24
    wind1 = [
        clamp(w - shift + e[k] - mean_e, 0.3, 23.5) if k < 24 else w
        for k, w in enumerate(wind)
    ]
    temp1 = [
        clamp(tc + r.uniform(-0.5, 0.5), -15.0, 3.0) if k < 24 else tc
        for k, tc in enumerate(temp)
    ]
    v1 = version(1, wind1, temp1, weather_runs(prev_run, prev_run, t))
    v2 = version(2, wind, temp, weather_runs(fresh_run, prev_run, t))
    real_shift = sum(r1(a) - r1(b) for a, b in zip(wind[:24], wind1[:24])) / 24
    p1, p2 = plant(v1["rows"]), plant(v2["rows"])
    mean_pp = sum(b - a for a, b in zip(p1[:24], p2[:24])) / 24 * 100
    peak_pp = (max(p2) - max(p1)) * 100
    note = f"ветер {signed1(real_shift)} м/с на часах 1–24 → P50 {signed_int(mean_pp)} п.п. в среднем"
    if abs(round(peak_pp)) >= 1:
        note += f", пик {signed_int(peak_pp)} п.п."
    v2["change_note"] = note + " против v1"
    issue["shift"] = real_shift
    issue["versions"] = {1: v1, 2: v2}
    return issue


def latest(issue: dict) -> dict:
    return issue["versions"][max(issue["versions"])]


def forecast_doc(issue_date: str, t: datetime, versions: list[int], v: dict) -> dict:
    return {
        "issue_date": issue_date,
        "issue_time_local": iso_local(t),
        "issue_time_utc": iso_utc(t),
        "version": v["version"],
        "versions": versions,
        "weather_runs": v["weather_runs"],
        "change_note": v["change_note"],
        "summary": v["summary"],
        "flags": v["flags"],
        "rows": v["rows"],
    }


def issue_entry(issue: dict) -> dict:
    v = latest(issue)
    p50 = plant(v["rows"])
    return {
        "issue_date": str(issue["date"]),
        "issue_time_local": iso_local(issue["t"]),
        "status": "published",
        "version": v["version"],
        "versions": sorted(issue["versions"]),
        "mean_p50": r4(sum(p50) / HORIZON),
        "peak_p50": max(p50),
        "flags": {k: sum(f["kind"] == k for f in v["flags"]) for k in FLAG_KINDS},
        "source": "api",
    }


# ---------- agent traces ----------
class Trace:
    def __init__(self, issue_date: str, start: datetime, seed: int) -> None:
        self.issue_date, self.clock = issue_date, start
        self.rng = random.Random(seed)
        self.events: list[dict] = []

    def add(
        self,
        kind: str,
        title: str,
        body: str,
        *,
        tool: str | None = None,
        stage: str | None = None,
        status: str = "ok",
        version: int = 1,
        args: dict | None = None,
        artifact: str | None = None,
        took: tuple[int, int] = (1, 2),
    ) -> None:
        if self.events:
            self.clock += timedelta(seconds=self.rng.randint(*took))
        meta = {
            "tool": tool,
            "args": args or {},
            "artifact": artifact,
            "stage": stage,
            "status": status,
            "issue_date": self.issue_date,
            "version": version,
            "source": "api",
        }
        self.events.append(
            {
                "seq": len(self.events) + 1,
                "ts": iso_ts(self.clock),
                "type": kind,
                "title": title,
                "body": body,
                "meta": meta,
            }
        )


def issue_events(issue: dict, tr: Trace) -> None:
    """Full run, trigger=issue: v1 on previous_day2 -> fresher run -> recalc decision."""
    d, t, times = issue["date"], issue["t"], issue["times"]
    ds, prev_run, fresh_run = str(d), issue["prev_run"], issue["fresh_run"]
    v1, v2 = issue["versions"][1], issue["versions"].get(2)
    csv = f"outputs/forecasts/{ds}.csv"
    first = times[0].astimezone(LOCAL)
    tr.add(
        "thought",
        f"Готовлю выпуск {ddm(d)} — только на данных до момента выпуска.",
        f"Момент выпуска T = {dm(first)}.{first.year} 00:00 по Астане ({hm_utc(t)}). "
        f"Горизонт 48 ч: {hm(times[0])} … {hm(times[-1])}. "
        "Факт выработки и SCADA в прогнозе не используются.",
    )
    tr.add(
        "tool_call",
        "Начинаю с погоды — без неё модель не запустить.",
        f'fetch_weather(issue_date="{ds}", run="previous") — для v1 беру прогон '
        "previous_day2 на всё окно",
        tool="fetch_weather",
        stage="weather",
        args={"issue_date": ds, "run": "previous"},
    )
    w, tc = [r1(x) for x in v1["wind"]], [r1(x) for x in v1["temp"]]
    tr.add(
        "tool_result",
        "Погода получена: 48 часов ECMWF IFS из Open-Meteo.",
        f"Previous Runs API · {WEATHER_MODEL} · прогон {hm_utc(prev_run)} для часов 1–48 · "
        f"ветер 100 м {dec1(min(w))}–{dec1(max(w))} м/с · "
        f"температура от {temp_text(min(tc))} до {temp_text(max(tc))} °C · источник: API",
        tool="fetch_weather",
        stage="weather",
        took=(1, 3),
    )
    tr.add(
        "tool_call",
        "Проверяю полноту и что погода не из будущего.",
        f'check_data(issue_date="{ds}")',
        tool="check_data",
        stage="prep",
        args={"issue_date": ds},
    )
    gap = MISSING_TEMP.get(d)
    if gap:
        a, b = gap
        tr.add(
            "tool_result",
            "Два часа без температуры — заполняю соседними значениями, выпуск не блокирую.",
            f"46 из 48 часов полные · температура пропущена в h {a}–{b} "
            f"({span(times[a - 1], times[b - 1])}), заполнена интерполяцией · "
            f"прогон {hm_utc(prev_run)} ≤ момента выпуска {hm_utc(t)} ✓",
            tool="check_data",
            stage="prep",
            status="warn",
            took=(0, 1),
        )
    else:
        tr.add(
            "tool_result",
            "Данные в порядке: 48 из 48 часов, прогон вышел до момента выпуска.",
            f"пропусков нет · самый поздний прогон {hm_utc(prev_run)} ≤ момента выпуска "
            f"{hm_utc(t)} ✓",
            tool="check_data",
            stage="prep",
            took=(0, 1),
        )
    tr.add(
        "tool_call",
        "Пропуски закрыты — запускаю модель."
        if gap
        else "Данные в порядке — запускаю модель.",
        f"run_model · {MODEL_VERSION} · 48 ч × Т1, Т2, ВЭС · квантили P10 / P50 / P90",
        tool="run_model",
        stage="model",
        args={"issue_date": ds, "weather": "previous"},
    )
    p50 = plant(v1["rows"])
    mx = max(range(HORIZON), key=lambda k: (p50[k], -k))
    mn = min(range(HORIZON), key=lambda k: (p50[k], k))
    tr.add(
        "tool_result",
        "Прогноз готов: 144 строки, P10 ≤ P50 ≤ P90.",
        f"ВЭС: пик P50 {pct(p50[mx])} — {hm(times[mx])}, минимум {pct(p50[mn])} — "
        f"{hm(times[mn])}, в среднем {pct(sum(p50) / HORIZON)} номинала",
        tool="run_model",
        stage="forecast",
        took=(2, 4),
    )
    tr.add(
        "tool_call",
        "Ищу резкие спады, обледенение и ветер выше 20 м/с.",
        f'analyze_forecast(run_id="{ds}-v1")',
        tool="analyze_forecast",
        stage="analysis",
        args={"run_id": f"{ds}-v1"},
    )
    n = len(v1["flags"])
    tr.add(
        "tool_result",
        f"Нашёл {n} {plural_risk(n)} — отмечу в сводке." if n else "Рисков не нашёл.",
        flags_brief(times, v1["flags"])
        if n
        else "рамп ≥ 30 % за 2 ч нет · ветер ≤ 20 м/с · условий обледенения нет · "
        "погодные модели согласны",
        tool="analyze_forecast",
        stage="analysis",
        status="warn" if n else "ok",
        took=(0, 1),
    )
    tr.add(
        "action",
        "Публикую v1 и сводку для диспетчера.",
        f"v1 · {csv} · 144 строки · запись в журнал выпусков",
        tool="publish_forecast",
        args={"issue_date": ds, "version": 1},
        artifact=csv,
    )
    tr.add(
        "thought",
        "Вышел более свежий прогон — проверю, изменилась ли погода.",
        f"Для часов 1–24 доступен прогон {hm_utc(fresh_run)} (previous_day1) — он вышел до "
        f"момента выпуска {hm_utc(t)}. Порог пересчёта — средний сдвиг ветра 0,5 м/с.",
        took=(1, 3),
    )
    tr.add(
        "tool_call",
        "Сравниваю новый прогон с тем, на котором посчитан v1.",
        f'recalc_forecast(issue_date="{ds}", reason="new_weather_run")',
        tool="recalc_forecast",
        stage="recalc",
        args={"issue_date": ds, "reason": "new_weather_run"},
    )
    if v2 is None:
        shift = issue["small_shift"]
        tr.add(
            "tool_result",
            f"Пересчёт не нужен: сдвиг ветра {signed1(shift)} м/с меньше порога 0,5.",
            f"прогон {hm_utc(fresh_run)} для часов 1–24 почти не отличается от "
            f"{hm_utc(prev_run)} · новых рисков нет · v1 остаётся в силе",
            tool="recalc_forecast",
            stage="recalc",
            status="skip",
            took=(1, 2),
        )
        tr.add(
            "verdict",
            f"Выпуск {ddm(d)} опубликован: v1, пересчёт не понадобился.",
            v1["summary"],
        )
        return
    tr.add(
        "tool_result",
        f"Сдвиг ветра {signed1(issue['shift'])} м/с больше порога 0,5 — пересчитал, это v2.",
        f"прогон {hm_utc(fresh_run)} для часов 1–24 вместо {hm_utc(prev_run)} · модель "
        f"перезапущена, анализ повторён: {flags_brief(times, v2['flags'])} · "
        f"{v2['change_note']}",
        tool="recalc_forecast",
        stage="recalc",
        version=2,
        took=(3, 5),
    )
    tr.add(
        "action",
        "Публикую v2 с объяснением, v1 оставляю для сравнения.",
        f"v2 · «{v2['change_note']}» · {csv} · 144 строки",
        tool="publish_forecast",
        version=2,
        args={"issue_date": ds, "version": 2},
        artifact=csv,
    )
    tr.add(
        "verdict",
        f"Выпуск {ddm(d)} опубликован: v2 на свежем прогоне.",
        v2["summary"],
        version=2,
    )


def new_weather_run_events(issue: dict, tr: Trace) -> None:
    """trigger=new_weather_run on an archive day whose v2 already used the last run ≤ T."""
    d, t, ds = issue["date"], issue["t"], str(issue["date"])
    fresh_run = issue["fresh_run"]
    next_run = fresh_run + timedelta(
        days=1
    )  # previous_day0 would be the only fresher one
    v = max(issue["versions"])
    tr.add(
        "thought",
        "Пришло событие «новый прогон погоды» — ищу прогон свежее текущего.",
        f"v{v} посчитана на прогоне {hm_utc(fresh_run)} для часов 1–24. Брать можно только "
        f"прогоны, вышедшие до момента выпуска {hm_utc(t)}.",
        version=v,
    )
    tr.add(
        "tool_call",
        "Проверяю, есть ли прогон свежее и не позже момента выпуска.",
        f'fetch_weather(issue_date="{ds}", run="latest")',
        tool="fetch_weather",
        stage="weather",
        version=v,
        args={"issue_date": ds, "run": "latest"},
    )
    tr.add(
        "tool_result",
        f"Свежее {hm_utc(fresh_run)} прогона до момента выпуска нет.",
        f"Previous Runs API · {WEATHER_MODEL}: последний прогон до {hm_utc(t)} — "
        f"{hm_utc(fresh_run)}, он уже учтён в v{v}. Следующий — {hm_utc(next_run)} "
        "(previous_day0).",
        tool="fetch_weather",
        stage="weather",
        version=v,
        took=(1, 3),
    )
    tr.add(
        "tool_call",
        f"Проверяю, можно ли взять прогон {hm_utc(next_run)}.",
        f'check_data(issue_date="{ds}") · кандидат — прогон {hm_utc(next_run)}',
        tool="check_data",
        stage="prep",
        version=v,
        args={"issue_date": ds, "candidate_init_utc": iso_utc(next_run)},
    )
    tr.add(
        "tool_result",
        f"Прогон {hm_utc(next_run)} позже момента выпуска {hm_utc(t)}.",
        f"init {iso_utc(next_run)} > T {iso_utc(t)} · runs_before_issue = false · "
        "правило «без будущего» запрещает его брать",
        tool="check_data",
        stage="prep",
        status="warn",
        version=v,
        took=(0, 1),
    )
    tr.add(
        "thought",
        "Отказываюсь пересчитывать: прогон вышел бы после момента выпуска.",
        f"Взять его — значит подглядеть в будущее. v{v} уже учла последний прогон, "
        "доступный до момента выпуска.",
        stage="recalc",
        status="skip",
        version=v,
        took=(1, 3),
    )
    tr.add(
        "verdict",
        f"Выпуск {ddm(d)} остаётся в версии v{v} — пересчёт запрещён правилом «без будущего».",
        f"Свежее {hm_utc(fresh_run)} прогонов до {hm_utc(t)} нет; прогон {hm_utc(next_run)} "
        "вышел бы после момента выпуска. Новая версия не публикуется.",
        status="skip",
        version=v,
    )


# ---------- live ----------
def make_live() -> tuple[dict, dict]:
    times = targets(next_full_hour(LIVE_ISSUED))
    wind, temp = pseudo_weather(
        4242, HORIZON, times[0].astimezone(LOCAL).hour, 15.0, 6.0
    )
    wind = [clamp(w, 0.3, 23.5) for w in wind]
    r = random.Random(4243)
    noise = [[r.uniform(-0.03, 0.03) for _ in range(HORIZON)] for _ in range(2)]
    rows = build_rows(times, wind, temp, noise, None)
    flags = detect_flags(times, rows, None, 0.0)
    v1 = {
        "version": 1,
        "weather_runs": weather_runs(LIVE_PREV_RUN, LIVE_PREV_RUN, LIVE_ISSUED),
        "change_note": None,
        "summary": summary_text(times, rows, flags),
        "flags": flags,
        "rows": rows,
    }
    status = {
        "now_local": iso_local(LIVE_NOW),
        "latest_run_utc": iso_utc(LIVE_LATEST_RUN),
        "next_run_utc": iso_utc(LIVE_LATEST_RUN + timedelta(hours=6)),
        "next_run_available_local": iso_local(
            LIVE_LATEST_RUN + timedelta(hours=6) + RUN_AVAILABLE_AFTER
        ),
        "current": {
            "version": 1,
            "issued_at_local": iso_local(LIVE_ISSUED),
            "weather_run_utc": iso_utc(LIVE_PREV_RUN),
        },
    }
    return status, forecast_doc("live", LIVE_ISSUED, [1], v1)


# ---------- quality (January backtest) ----------
def make_metrics() -> dict:
    by_horizon = []
    for h in range(1, HORIZON + 1):
        x = (h - 1) / 47
        model = 0.094 + 0.046 * x**0.9 + 0.0025 * math.sin(h / 3)
        curve = 0.12 + 0.054 * x**0.85 + 0.003 * math.sin(h / 3 + 1)
        by_horizon.append({"h": h, "model": r4(model), "power_curve": r4(curve)})
    mean = {
        key: round(sum(b[key] for b in by_horizon) / HORIZON, 3)
        for key in ("model", "power_curve")
    }
    return {
        "period": {"from": str(METRICS_FROM), "to": str(METRICS_TO)},
        "issues_count": (METRICS_TO - METRICS_FROM).days + 1,
        "coverage_p10_p90": 0.79,
        "methods": [
            {
                "key": "model",
                "label": "Модель · LightGBM, квантили",
                "nmae": mean["model"],
                "nrmse": 0.171,
            },
            {
                "key": "power_curve",
                "label": "Кривая мощности по прогнозу ветра",
                "nmae": mean["power_curve"],
                "nrmse": 0.207,
            },
            {
                "key": "climatology",
                "label": "Климатология час × месяц",
                "nmae": 0.216,
                "nrmse": 0.279,
            },
            {
                "key": "persistence",
                "label": "Персистентность · последние 24 ч",
                "nmae": 0.247,
                "nrmse": 0.334,
            },
        ],
        "by_horizon": by_horizon,
    }


def make_series() -> list[dict]:
    n = SERIES_DAYS * 24
    first = datetime(SERIES_FROM.year, SERIES_FROM.month, SERIES_FROM.day, tzinfo=LOCAL)
    wind, _ = pseudo_weather(
        791, n, 0, -12.0, 3.0
    )  # seed picked: nMAE ~0.11, cover ~0.8
    r = random.Random(99)
    err, out = 0.0, []
    for k in range(n):
        w = clamp(wind[k], 0.3, 23.5)
        p = power_curve(w) * 0.96
        # day-ahead (h <= 24) band, calibrated so ~80 % of facts fall inside it
        spread = (0.05 + 0.09 * (k % 24) / 47 + 0.28 * p * (1 - p)) * 1.6
        err = 0.8 * err + r.gauss(0.0, 1.4)  # forecast wind error, m/s
        fact = power_curve(clamp(w + err, 0.0, 30.0)) * 0.96
        out.append(
            {
                "target_time_local": iso_local(first + timedelta(hours=k)),
                "p10": r4(clamp(p - 1.1 * spread)),
                "p50": r4(p),
                "p90": r4(clamp(p + spread)),
                "actual": r4(fact),
            }
        )
    return out


# ---------- validation (CONTRACT §6, trace-event-schema.md) ----------
def is4(x: object) -> bool:
    return isinstance(x, float) and round(x, 4) == x


def check_events(events: list[dict], issue_date: str) -> None:
    assert events and events[-1]["type"] == "verdict", "a run ends with a verdict"
    prev, steps = None, 0
    for n, e in enumerate(events, 1):
        assert list(e) == EVENT_KEYS, e
        assert e["seq"] == n, e
        ts = datetime.strptime(e["ts"], "%Y-%m-%dT%H:%M:%S+05:00").replace(tzinfo=LOCAL)
        assert prev is None or ts >= prev, e
        prev = ts
        assert e["type"] in EVENT_TYPES, e
        assert isinstance(e["title"], str) and e["title"].strip(), e
        assert isinstance(e["body"], str) and e["body"].strip(), e
        m = e["meta"]
        assert list(m) == META_KEYS, m
        assert m["stage"] is None or m["stage"] in STAGES, m
        assert m["status"] in STATUSES, m
        assert m["issue_date"] == issue_date, m
        assert isinstance(m["version"], int) and m["version"] >= 1, m
        assert m["source"] in ("api", "cache"), m
        assert isinstance(m["args"], dict), m
        if e["type"] in ("tool_call", "tool_result", "action"):
            assert m["tool"], e
        if m["tool"] == "publish_forecast":
            assert e["type"] == "action" and m["artifact"], e
        steps += e["type"] in ("tool_call", "action")
    assert steps <= MAX_STEPS, f"{steps} steps > {MAX_STEPS}"


def check_forecast(doc: dict, issue_date: str) -> None:
    assert list(doc) == FORECAST_KEYS, list(doc)
    assert doc["issue_date"] == issue_date
    t = parse_utc(doc["issue_time_utc"])
    assert iso_local(t) == doc["issue_time_local"]
    if issue_date == "live":
        first = next_full_hour(t)
    else:
        assert t == issue_time_utc(date.fromisoformat(issue_date))
        first = t
    versions = doc["versions"]
    assert versions == list(range(1, len(versions) + 1)) and doc["version"] in versions
    assert (doc["change_note"] is None) == (doc["version"] == 1)
    assert [w["hours"] for w in doc["weather_runs"]] == ["1-24", "25-48"]
    for w in doc["weather_runs"]:
        assert list(w) == ["hours", "model", "init_utc", "before_issue"], w
        assert w["model"] == WEATHER_MODEL
        assert parse_utc(w["init_utc"]) <= t and w["before_issue"] is True, w
    sentences = len(re.findall(r"[.!?](?:\s|$)", doc["summary"]))
    assert 2 <= sentences <= 3, doc["summary"]
    rows = doc["rows"]
    assert len(rows) == HORIZON * len(TURBINES)
    assert {(r["h"], r["turbine"]) for r in rows} == {
        (h, tb) for h in range(1, HORIZON + 1) for tb in TURBINES
    }
    by = {(r["h"], r["turbine"]): r for r in rows}
    for r in rows:
        assert list(r) == ROW_KEYS, r
        assert r["target_time_local"] == iso_local(first + timedelta(hours=r["h"] - 1))
        assert all(is4(r[q]) for q in ("p10", "p50", "p90")), r
        assert 0.0 <= r["p10"] <= r["p50"] <= r["p90"] <= 1.0, r
        assert isinstance(r["wind_fc_ms"], float) and r["wind_fc_ms"] >= 0, r
        assert isinstance(r["temp_fc_c"], float), r
        assert r["actual"] is None, r
        if r["turbine"] == "plant":
            for q in ("p10", "p50", "p90"):
                mean = (by[(r["h"], "1")][q] + by[(r["h"], "2")][q]) / 2
                assert abs(r[q] - mean) <= 5e-5 + 1e-9, r
    p50, wind, temp = (
        [by[(h, "plant")][k] for h in range(1, HORIZON + 1)]
        for k in ("p50", "wind_fc_ms", "temp_fc_c")
    )
    for f in doc["flags"]:
        assert list(f) == ["kind", "from_h", "to_h", "text"], f
        assert f["kind"] in FLAG_KINDS and f["text"].strip(), f
        a, b = f["from_h"], f["to_h"]
        assert 1 <= a <= b <= HORIZON, f
        if f["kind"] == "ramp":
            assert b - a == 2 and abs(p50[b - 1] - p50[a - 1]) >= 0.3, f
        if f["kind"] == "ice":
            assert -3.0 <= temp[a - 1] <= 1.0 and -3.0 <= temp[b - 1] <= 1.0, f
        if f["kind"] == "wind_gt20":
            assert all(x > 20.0 for x in wind[a - 1 : b]), f


def load(name: str) -> object:
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def validate(expected: set[str]) -> None:
    assert {p.name for p in OUT.glob("*.json")} == expected, "unexpected file set"

    health = load("health.json")
    assert list(health) == ["ok", "mode", "model_version", "issues_ready"]
    assert health["ok"] is True and health["mode"] in ("agent", "deterministic")
    assert health["issues_ready"] == len(ISSUES) == 29

    issues = load("issues.json")
    assert [x["issue_date"] for x in issues] == [str(d) for d in ISSUES]
    for entry in issues:
        ds = entry["issue_date"]
        assert list(entry) == [
            "issue_date",
            "issue_time_local",
            "status",
            "version",
            "versions",
            "mean_p50",
            "peak_p50",
            "flags",
            "source",
        ]
        assert (
            entry["issue_time_local"]
            == f"{date.fromisoformat(ds) + timedelta(days=1)}T00:00+05:00"
        )
        assert entry["status"] == "published" and entry["source"] == "api"
        assert entry["version"] == max(entry["versions"]) and entry["versions"] in (
            [1],
            [1, 2],
        )
        doc = load(f"forecasts_{ds}.json")
        check_forecast(doc, ds)
        assert (
            doc["version"] == entry["version"] and doc["versions"] == entry["versions"]
        )
        p50 = [r["p50"] for r in doc["rows"] if r["turbine"] == "plant"]
        assert entry["mean_p50"] == r4(sum(p50) / HORIZON)
        assert entry["peak_p50"] == max(p50)
        assert list(entry["flags"]) == list(FLAG_KINDS)
        for kind in FLAG_KINDS:
            assert entry["flags"][kind] == sum(f["kind"] == kind for f in doc["flags"])
        day0 = f"{ds}T00:00Z"
        day_1 = f"{date.fromisoformat(ds) - timedelta(days=1)}T00:00Z"
        runs = [w["init_utc"] for w in doc["weather_runs"]]
        if entry["version"] == 2:
            assert runs == [day0, day_1], runs
            v1 = load(f"forecasts_{ds}_v1.json")
            check_forecast(v1, ds)
            assert v1["version"] == 1 and v1["versions"] == [1, 2]
            assert [w["init_utc"] for w in v1["weather_runs"]] == [day_1, day_1]
            assert v1["rows"][72:] == doc["rows"][72:], "h 25-48 share the same run"
            assert v1["rows"][:72] != doc["rows"][:72]
        else:
            assert runs == [day_1, day_1], runs
            assert not (OUT / f"forecasts_{ds}_v1.json").exists()
        trace = load(f"traces_{ds}.json")
        assert list(trace) == ["issue_date", "version", "recorded_at", "events"]
        assert trace["issue_date"] == ds and trace["version"] == entry["version"]
        parse_local(trace["recorded_at"])
        check_events(trace["events"], ds)
        actions = [e for e in trace["events"] if e["type"] == "action"]
        assert [e["meta"]["version"] for e in actions] == entry["versions"]
        assert trace["events"][-1]["body"] == doc["summary"]
        recalc = [e for e in trace["events"] if e["meta"]["tool"] == "recalc_forecast"]
        assert recalc[-1]["meta"]["status"] == (
            "ok" if entry["version"] == 2 else "skip"
        )
    assert sum(x["version"] == 2 for x in issues) == len(V2_SHIFT) == 6
    for kind in FLAG_KINDS:
        assert any(x["flags"][kind] for x in issues), f"no {kind} flag in February"
    assert any(not any(x["flags"].values()) for x in issues), "want a calm day too"

    demo = str(DEMO_DAY)
    run = load("run_events_issue.json")
    check_events(run, demo)
    saved = load(f"traces_{demo}.json")["events"]
    strip = [{k: v for k, v in e.items() if k != "ts"} for e in run]
    assert strip == [{k: v for k, v in e.items() if k != "ts"} for e in saved]
    assert sum(e["type"] == "action" for e in run) == 2

    refuse = load("run_events_new_weather_run.json")
    check_events(refuse, demo)
    assert not any(e["type"] == "action" for e in refuse)
    assert any("прогон вышел бы после момента выпуска" in e["title"] for e in refuse)
    assert any(
        e["meta"]["stage"] == "recalc" and e["meta"]["status"] == "skip" for e in refuse
    )
    assert refuse[-1]["meta"]["status"] == "skip"

    status = load("live_status.json")
    assert list(status) == [
        "now_local",
        "latest_run_utc",
        "next_run_utc",
        "next_run_available_local",
        "current",
    ]
    assert list(status["current"]) == ["version", "issued_at_local", "weather_run_utc"]
    now = parse_local(status["now_local"])
    latest_run, next_run = (
        parse_utc(status["latest_run_utc"]),
        parse_utc(status["next_run_utc"]),
    )
    assert latest_run + RUN_AVAILABLE_AFTER <= now < next_run + RUN_AVAILABLE_AFTER
    assert parse_local(status["next_run_available_local"]) > now
    assert parse_utc(status["current"]["weather_run_utc"]) <= latest_run
    live = load("forecasts_live.json")
    check_forecast(live, "live")
    assert live["version"] == status["current"]["version"]
    assert live["issue_time_local"] == status["current"]["issued_at_local"]
    assert parse_local(live["issue_time_local"]) <= now
    assert all(
        w["init_utc"] == status["current"]["weather_run_utc"]
        for w in live["weather_runs"]
    )

    metrics = load("metrics.json")
    assert list(metrics) == [
        "period",
        "issues_count",
        "coverage_p10_p90",
        "methods",
        "by_horizon",
    ]
    assert metrics["period"] == {"from": str(METRICS_FROM), "to": str(METRICS_TO)}
    assert metrics["issues_count"] == 30 and 0 < metrics["coverage_p10_p90"] < 1
    keys = [m["key"] for m in metrics["methods"]]
    assert keys == ["model", "power_curve", "climatology", "persistence"]
    for m in metrics["methods"]:
        assert (
            list(m) == ["key", "label", "nmae", "nrmse"]
            and 0 < m["nmae"] < m["nrmse"] < 1
        )
    best = metrics["methods"][0]
    assert all(
        best["nmae"] < m["nmae"] and best["nrmse"] < m["nrmse"]
        for m in metrics["methods"][1:]
    )
    assert [b["h"] for b in metrics["by_horizon"]] == list(range(1, HORIZON + 1))
    for b in metrics["by_horizon"]:
        assert (
            list(b) == ["h", "model", "power_curve"] and b["model"] < b["power_curve"]
        )

    series = load("metrics_series.json")
    first = datetime(SERIES_FROM.year, SERIES_FROM.month, SERIES_FROM.day, tzinfo=LOCAL)
    assert len(series) == SERIES_DAYS * 24 == 168
    for k, p in enumerate(series):
        assert list(p) == ["target_time_local", "p10", "p50", "p90", "actual"]
        assert p["target_time_local"] == iso_local(first + timedelta(hours=k))
        assert (
            0.0 <= p["p10"] <= p["p50"] <= p["p90"] <= 1.0 and 0.0 <= p["actual"] <= 1.0
        )
    inside = sum(p["p10"] <= p["actual"] <= p["p90"] for p in series) / len(series)
    assert 0.7 <= inside <= 0.9, f"series coverage {inside:.2f} is off the metrics"


# ---------- main ----------
def main() -> None:
    files: dict[str, object] = {}
    issues = [make_issue(i, d) for i, d in enumerate(ISSUES)]
    files["health.json"] = {
        "ok": True,
        "mode": "agent",
        "model_version": MODEL_VERSION,
        "issues_ready": len(issues),
    }
    files["issues.json"] = [issue_entry(x) for x in issues]
    clock = RECORDED_FROM
    for issue in issues:
        ds, vs = str(issue["date"]), sorted(issue["versions"])
        v = latest(issue)
        files[f"forecasts_{ds}.json"] = forecast_doc(ds, issue["t"], vs, v)
        if len(vs) > 1:
            files[f"forecasts_{ds}_v1.json"] = forecast_doc(
                ds, issue["t"], vs, issue["versions"][1]
            )
        tr = Trace(ds, clock, 3000 + issue["i"])
        issue_events(issue, tr)
        files[f"traces_{ds}.json"] = {
            "issue_date": ds,
            "version": v["version"],
            "recorded_at": iso_local(tr.clock),
            "events": tr.events,
        }
        clock = tr.clock + timedelta(seconds=2)
    demo = next(x for x in issues if x["date"] == DEMO_DAY)
    tr = Trace(str(DEMO_DAY), RUN_ISSUE_AT, 5001)
    issue_events(demo, tr)
    files["run_events_issue.json"] = tr.events
    tr = Trace(str(DEMO_DAY), RUN_NEW_WEATHER_AT, 5002)
    new_weather_run_events(demo, tr)
    files["run_events_new_weather_run.json"] = tr.events
    files["live_status.json"], files["forecasts_live.json"] = make_live()
    files["metrics.json"] = make_metrics()
    files["metrics_series.json"] = make_series()

    for stale in OUT.glob("*.json"):
        stale.unlink()
    for name, obj in files.items():
        (OUT / name).write_text(dumps(obj), encoding="utf-8")
    validate(set(files))

    size = sum((OUT / name).stat().st_size for name in files)
    print(f"{len(files)} files, {size / 1024:.0f} KiB, all checks passed")
    for entry in files["issues.json"]:
        flags = " ".join(f"{k}={n}" for k, n in entry["flags"].items() if n)
        print(
            f"  {entry['issue_date']} v{entry['version']} mean={entry['mean_p50']:.2f} "
            f"peak={entry['peak_p50']:.2f} {flags}"
        )


if __name__ == "__main__":
    main()
