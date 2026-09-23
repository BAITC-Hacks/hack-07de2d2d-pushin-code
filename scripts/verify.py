#!/usr/bin/env python3
"""Jury check: the February forecasts are complete, well-formed and use no future data.

    python3 scripts/verify.py [--root .] [--from 2026-01-31] [--to 2026-02-28] [--json]

For every issue D in the range it checks, against docs/CONTRACT.md:
- the §7 CSV outputs/forecasts/{D}.csv: columns in order, 48 h x {1, 2, plant}, issue and
  target times in +05:00, one version, P10 <= P50 <= P90 in [0, 1], wind in [0, 60);
- the §2 rule "no future": a weather run counts only once published, ~8 h after its start,
  so weather_init_max_utc + 8 h <= T in every row, and the same for every weather run of
  every version in the issue record;
- the agent trace outputs/traces/{D}.jsonl: event schema, an action, a final verdict;
- the §5.1 record outputs/forecasts/{D}.json: latest_version = the CSV version, no "stub";
then the combined outputs/forecast_feb2026.csv and the January metrics (model nMAE below
every baseline).

Standard library only, so plain python3 from a clean clone is enough. The time conventions
(§1: T = D 19:00 UTC = (D+1) 00:00 at UTC+5; target hour h starts at T + (h - 1) h; §2: the
8 h publication delay) are restated here on purpose instead of importing
backend/windcast/timeline.py: a checker that reused the code under test would agree with its
bugs.

Exit code 0 = PASS, 1 = FAIL. Human output is in Russian: the jury reads it.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

LOCAL_TZ = timezone(timedelta(hours=5))
HORIZON = 48
TURBINES = ("1", "2", "plant")
ROWS_PER_ISSUE = HORIZON * len(TURBINES)
TEST_FROM = date(2026, 1, 31)
TEST_TO = date(2026, 2, 28)
# docs/CONTRACT.md §2 (v0.4): a weather run is usable only once published, ~8 h after it
# starts (ECMWF open data). "No future" = init + 8 h <= T, i.e. the run started by D 11:00 UTC.
RUN_PUBLICATION_DELAY = timedelta(hours=8)
COLUMNS = (
    "issue_date",
    "issue_time_local",
    "target_time_local",
    "horizon_h",
    "turbine",
    "p10",
    "p50",
    "p90",
    "wind_fc_ms",
    "weather_init_max_utc",
    "version",
)
QUANTILES = ("p10", "p50", "p90")
DECIMAL_COLUMNS = ("p10", "p50", "p90", "wind_fc_ms")
INTEGER_COLUMNS = ("horizon_h", "version")
EVENT_KEYS = ("seq", "ts", "type", "title", "body", "meta")
EVENT_TYPES = ("thought", "tool_call", "tool_result", "action", "verdict", "error")
BASELINES = ("power_curve", "climatology", "persistence")
WIND_MAX_MS = 60.0
COMBINED = "forecast_feb2026.csv"
METRICS = "metrics_jan.json"
SHOW = 5  # concrete problems printed per failed check
JSON_PROBLEMS = 50  # problems kept per check in --json output

if TYPE_CHECKING:  # annotations only, so the script still runs on an old python3
    Rows = list[tuple[int, dict[str, str]]]  # (line in the file, row by column name)


# --- time and value parsing ----------------------------------------------------------------


def issue_moment(issue: date) -> datetime:
    """T for issue D: D 19:00 UTC = (D+1) 00:00 local (§1)."""
    return datetime(issue.year, issue.month, issue.day, 19, tzinfo=timezone.utc)


def iso_local(moment: datetime) -> str:
    return moment.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M+05:00")


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def published_late(init: datetime, moment: datetime) -> str | None:
    """None if the run started at `init` was published by T (§2), else what went wrong."""
    published = init + RUN_PUBLICATION_DELAY
    if published <= moment:
        return None
    return (
        f"прогон {iso_utc(init)} опубликован ≈ {iso_utc(published)}, "
        f"позже момента выпуска T={iso_utc(moment)}"
    )


_MOMENT = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6})\d*)?)?"
    r"(Z|[+-]\d{2}:?\d{2})"
)
_DECIMAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_INTEGER = re.compile(r"\d+")


def parse_moment(value: object) -> datetime | None:
    """ISO time WITH a zone ("2026-02-13T00:00Z", "...+05:00"); None if unparseable or naive."""
    if not isinstance(value, str):
        return None
    match = _MOMENT.fullmatch(value.strip())
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.group(1, 2, 3, 4, 5))
    second = int(match.group(6) or 0)
    micro = int((match.group(7) or "0").ljust(6, "0"))
    zone = match.group(8)
    try:
        if zone == "Z":
            tz = timezone.utc
        else:
            digits = zone[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            tz = timezone(-offset if zone[0] == "-" else offset)
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=tz)
    except ValueError:
        return None


def parse_decimal(text: str) -> float | None:
    """A plain finite decimal ("0.1234", "7.5"); None for text, nan, inf, "1_0"."""
    if not _DECIMAL.fullmatch(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def parse_int(text: str) -> int | None:
    return int(text) if _INTEGER.fullmatch(text) else None


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def in_test_period(text: str) -> bool:
    try:
        return TEST_FROM <= date.fromisoformat(text) <= TEST_TO
    except ValueError:
        return False


def shorten(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def show(value: object) -> str:
    return shorten(repr(value))


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


# --- results -------------------------------------------------------------------------------


@dataclass
class Problem:
    file: str
    line: int | None
    message: str

    def render(self) -> str:
        where = self.file if self.line is None else f"{self.file}:{self.line}"
        return f"{where} · {self.message}"


@dataclass
class Check:
    key: str
    title: str
    unit: str
    checked: int = 0
    failed: int = 0
    problems: list[Problem] = field(default_factory=list)
    note: str = ""

    def add(self, problems: list[Problem]) -> None:
        """Count one checked unit (a file, a row, a run); it fails if it has problems."""
        self.checked += 1
        if problems:
            self.failed += 1
            self.problems.extend(problems)

    @property
    def status(self) -> str:
        if self.failed:
            return "fail"
        return "pass" if self.checked else "skip"


def new_checks() -> list[Check]:
    return [
        Check("issues", "Выпуски на месте: forecasts/{D}.csv", "выпусков"),
        Check("no_future", "Без будущего: прогон опубликован до T (CSV)", "строк"),
        Check(
            "no_future_runs",
            "Без будущего: прогон опубликован до T ({D}.json)",
            "прогонов",
        ),
        Check("header", "Заголовок CSV: столбцы §7 по порядку", "файлов"),
        Check("rows", "144 строки: турбины 1, 2, plant × h 1…48", "файлов"),
        Check("times", "Дата выпуска, T и целевые часы в +05:00", "строк"),
        Check("version", "Одна версия ≥ 1 на файл", "файлов"),
        Check("quantiles", "P10 ≤ P50 ≤ P90, все в [0, 1]", "строк"),
        Check("wind", "Ветер wind_fc_ms в [0, 60) м/с", "строк"),
        Check("traces", "Лента шагов агента: traces/{D}.jsonl", "выпусков"),
        Check("records", "Запись {D}.json: latest_version = версия CSV", "выпусков"),
        Check("no_stub", "Без заглушек: нигде нет source = stub", "выпусков"),
        Check("combined", f"Сводный {COMBINED} = файлы выпусков", "строк"),
        Check("metrics", "Январь: nMAE модели ниже всех базовых линий", "файлов"),
    ]


@dataclass
class IssueCsv:
    rows: Rows  # rows with the right number of fields
    version: int | None  # the one valid version in the file, if there is exactly one


def read_csv(path: Path) -> tuple[list[str] | None, list[tuple[int, list[str]]]]:
    """Header and non-blank rows with their line numbers; a UTF-8 BOM is tolerated."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        rows = [(reader.line_num, fields) for fields in reader if fields]
    return header, rows


def header_problem(header: list[str] | None) -> str:
    if not header:
        return "файл пуст — нет заголовка"
    missing = [column for column in COLUMNS if column not in header]
    extra = [column for column in header if column not in COLUMNS]
    parts = []
    if missing:
        parts.append("нет столбцов " + ", ".join(missing))
    if extra:
        parts.append("лишние " + ", ".join(show(column) for column in extra))
    if not parts:
        parts.append("столбцы не в порядке §7")
    return "; ".join(parts) + " · получено: " + shorten(",".join(header), 160)


def row_key(row: dict[str, str]) -> tuple[str, str]:
    return row["turbine"], row["horizon_h"]


def same_value(column: str, expected: str, actual: str) -> bool:
    """Equal cells; decimals may differ in formatting only ("0.12" = "0.1200")."""
    if expected == actual:
        return True
    if column in DECIMAL_COLUMNS:
        a, b = parse_decimal(expected), parse_decimal(actual)
        return a is not None and b is not None and abs(a - b) <= 1e-9
    if column in INTEGER_COLUMNS:
        a = parse_int(expected)
        return a is not None and a == parse_int(actual)
    return False


# --- the checks ----------------------------------------------------------------------------


class Verifier:
    def __init__(self, root: Path, first: date, last: date) -> None:
        self.root = root
        self.dates = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        self.checks = {check.key: check for check in new_checks()}
        self.csvs: dict[date, IssueCsv] = {}

    def run(self) -> list[Check]:
        for issue in self.dates:
            issue_csv = self.check_issue_csv(issue)
            events = self.check_trace(issue)
            version = issue_csv.version if issue_csv else None
            record = self.check_record(issue, version)
            self.check_no_stub(issue, record, events)
        self.check_combined()
        self.check_metrics()
        return list(self.checks.values())

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    # outputs/forecasts/{D}.csv

    def check_issue_csv(self, issue: date) -> IssueCsv | None:
        path = self.root / "outputs" / "forecasts" / f"{issue}.csv"
        label = self.rel(path)
        if not path.is_file():
            self.checks["issues"].add([Problem(label, None, "нет файла выпуска")])
            return None
        self.checks["issues"].add([])
        try:
            header, raw = read_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            message = f"не читается как CSV в UTF-8: {exc}"
            self.checks["header"].add([Problem(label, None, message)])
            return None
        if header != list(COLUMNS):
            self.checks["header"].add([Problem(label, 1, header_problem(header))])
            return None
        self.checks["header"].add([])
        rows = self.check_rows(label, raw)
        version = self.check_version(label, rows)
        moment = issue_moment(issue)
        for line, row in rows:
            self.check_row_values(issue, moment, label, line, row)
        self.csvs[issue] = IssueCsv(rows, version)
        return self.csvs[issue]

    def check_rows(self, label: str, raw: list[tuple[int, list[str]]]) -> Rows:
        problems: list[Problem] = []
        if len(raw) != ROWS_PER_ISSUE:
            message = f"{len(raw)} строк данных вместо {ROWS_PER_ISSUE}"
            problems.append(Problem(label, None, message))
        rows: Rows = []
        first_seen: dict[tuple[str, int], int] = {}
        for line, fields in raw:
            if len(fields) != len(COLUMNS):
                message = f"{len(fields)} полей вместо {len(COLUMNS)}"
                problems.append(Problem(label, line, message))
                continue
            row = dict(zip(COLUMNS, fields))
            rows.append((line, row))
            turbine, h = row["turbine"], parse_int(row["horizon_h"])
            wrong = []
            if turbine not in TURBINES:
                wrong.append(f"turbine={show(turbine)} — ожидается 1, 2 или plant")
            if h is None or not 1 <= h <= HORIZON:
                horizon = show(row["horizon_h"])
                wrong.append(f"horizon_h={horizon} — ожидается целое 1…{HORIZON}")
            if wrong:
                problems.append(Problem(label, line, "; ".join(wrong)))
            elif (turbine, h) in first_seen:
                first = first_seen[turbine, h]
                message = f"повтор turbine={turbine} h={h} (впервые — строка {first})"
                problems.append(Problem(label, line, message))
            else:
                first_seen[turbine, h] = line
        missing = [
            f"turbine={turbine} h={h}"
            for turbine in TURBINES
            for h in range(1, HORIZON + 1)
            if (turbine, h) not in first_seen
        ]
        if missing and rows:
            example = ", ".join(missing[:3]) + (" …" if len(missing) > 3 else "")
            message = (
                f"нет {len(missing)} из {ROWS_PER_ISSUE} пар (turbine, h): {example}"
            )
            problems.append(Problem(label, None, message))
        self.checks["rows"].add(problems)
        return rows

    def check_version(self, label: str, rows: Rows) -> int | None:
        if not rows:
            return None
        problems: list[Problem] = []
        found: dict[int, int] = {}
        for line, row in rows:
            value = parse_int(row["version"])
            if value is None or value < 1:
                message = f"version={show(row['version'])} — нужно целое ≥ 1"
                problems.append(Problem(label, line, message))
            else:
                found.setdefault(value, line)
        if len(found) > 1:
            listed = ", ".join(str(value) for value in sorted(found))
            message = f"в файле несколько версий: {listed} — нужна одна, последняя"
            problems.append(Problem(label, None, message))
        self.checks["version"].add(problems)
        return next(iter(found)) if len(found) == 1 and not problems else None

    def check_row_values(
        self, issue: date, moment: datetime, label: str, line: int, row: dict[str, str]
    ) -> None:
        where = f"turbine={row['turbine']} h={row['horizon_h']}"

        def problems(messages: list[str]) -> list[Problem]:
            if not messages:
                return []
            return [Problem(label, line, f"{where} · " + "; ".join(messages))]

        times = []
        if row["issue_date"] != issue.isoformat():
            times.append(f"issue_date={show(row['issue_date'])}, ожидается {issue}")
        expected = iso_local(moment)
        if row["issue_time_local"] != expected:
            got = show(row["issue_time_local"])
            times.append(f"issue_time_local={got}, ожидается {expected}")
        h = parse_int(row["horizon_h"])
        if h is not None and 1 <= h <= HORIZON:
            target = iso_local(moment + timedelta(hours=h - 1))
            if row["target_time_local"] != target:
                got = show(row["target_time_local"])
                times.append(f"target_time_local={got}, ожидается {target}")
        self.checks["times"].add(problems(times))

        quantiles = []
        values = {}
        for column in QUANTILES:
            values[column] = parse_decimal(row[column])
            if values[column] is None:
                quantiles.append(f"{column}={show(row[column])} — не число")
            elif not 0.0 <= values[column] <= 1.0:
                quantiles.append(f"{column}={row[column]} вне [0, 1]")
        for low, high in (("p10", "p50"), ("p50", "p90")):
            if None not in (values[low], values[high]) and values[low] > values[high]:
                quantiles.append(f"{low}={row[low]} > {high}={row[high]}")
        self.checks["quantiles"].add(problems(quantiles))

        wind = parse_decimal(row["wind_fc_ms"])
        if wind is None:
            winds = [f"wind_fc_ms={show(row['wind_fc_ms'])} — не число"]
        elif not 0.0 <= wind < WIND_MAX_MS:
            winds = [f"wind_fc_ms={row['wind_fc_ms']} вне [0, 60) м/с"]
        else:
            winds = []
        self.checks["wind"].add(problems(winds))

        raw = row["weather_init_max_utc"]
        init = parse_moment(raw)
        if init is None:
            future = [
                (
                    f"weather_init_max_utc={show(raw)} — не время с зоной "
                    "(нужно вида 2026-02-13T00:00Z), будущее не исключить"
                )
            ]
        else:
            late = published_late(init, moment)
            future = [f"weather_init_max_utc: {late}"] if late else []
        self.checks["no_future"].add(problems(future))

    # outputs/traces/{D}.jsonl

    def check_trace(self, issue: date) -> list[tuple[int, dict]] | None:
        path = self.root / "outputs" / "traces" / f"{issue}.jsonl"
        label = self.rel(path)
        check = self.checks["traces"]
        if not path.is_file():
            check.add([Problem(label, None, "нет ленты шагов агента")])
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            check.add([Problem(label, None, f"не читается как UTF-8: {exc}")])
            return None
        problems: list[Problem] = []
        events: list[tuple[int, dict]] = []
        # split("\n"), not splitlines(): JSON strings may hold raw U+2028 and friends
        for number, line in enumerate(text.split("\n"), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(Problem(label, number, f"не JSON: {exc.msg}"))
                continue
            if not isinstance(event, dict):
                problems.append(Problem(label, number, "событие — не JSON-объект"))
                continue
            events.append((number, event))
            missing = [key for key in EVENT_KEYS if key not in event]
            if missing:
                message = "нет ключей " + ", ".join(missing)
                problems.append(Problem(label, number, message))
            if "type" in event and event["type"] not in EVENT_TYPES:
                allowed = ", ".join(EVENT_TYPES)
                message = f"type={show(event['type'])} — не из схемы ({allowed})"
                problems.append(Problem(label, number, message))
            if "meta" in event and not isinstance(event["meta"], dict):
                problems.append(Problem(label, number, "meta — не объект"))
        if not events:
            problems.append(Problem(label, None, "в ленте нет событий"))
        else:
            if not any(event.get("type") == "action" for _, event in events):
                message = "нет события action — выпуск не опубликован действием агента"
                problems.append(Problem(label, None, message))
            number, last = events[-1]
            if last.get("type") != "verdict":
                message = (
                    f"последнее событие type={show(last.get('type'))}, а нужно verdict"
                )
                problems.append(Problem(label, number, message))
        check.add(problems)
        return events

    # outputs/forecasts/{D}.json

    def check_record(self, issue: date, csv_version: int | None) -> dict | None:
        path = self.root / "outputs" / "forecasts" / f"{issue}.json"
        label = self.rel(path)
        check = self.checks["records"]
        if not path.is_file():
            check.add([Problem(label, None, "нет записи выпуска")])
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            check.add([Problem(label, None, f"не читается как JSON: {exc}")])
            return None
        if not isinstance(record, dict):
            check.add([Problem(label, None, "запись — не JSON-объект")])
            return None
        problems: list[Problem] = []
        if record.get("issue_date") != issue.isoformat():
            got = show(record.get("issue_date"))
            problems.append(
                Problem(label, None, f"issue_date={got}, ожидается {issue}")
            )
        latest = record.get("latest_version")
        if not is_int(latest) or latest < 1:
            message = f"latest_version={show(latest)} — нужно целое ≥ 1"
            problems.append(Problem(label, None, message))
            latest = None
        versions = record.get("versions")
        if not isinstance(versions, dict) or not versions:
            problems.append(Problem(label, None, "versions нет или пусто"))
        elif latest is not None and str(latest) not in versions:
            present = ", ".join(sorted(versions))
            message = f"версии {latest} нет в versions (есть: {present})"
            problems.append(Problem(label, None, message))
        if latest is not None and csv_version is not None and latest != csv_version:
            message = f"latest_version={latest}, а в {issue}.csv version={csv_version}"
            problems.append(Problem(label, None, message))
        check.add(problems)
        if isinstance(versions, dict):
            self.check_record_runs(issue, label, versions)
        return record

    def check_record_runs(self, issue: date, label: str, versions: dict) -> None:
        """Every weather run of every version (v1 included) was published by T (§2)."""
        moment = issue_moment(issue)
        check = self.checks["no_future_runs"]
        for key in sorted(versions, key=lambda k: (len(k), k)):
            version = versions[key]
            runs = version.get("weather_runs") if isinstance(version, dict) else None
            if not isinstance(runs, list):
                continue
            for index, run in enumerate(runs):
                run = run if isinstance(run, dict) else {}
                raw = run.get("init_utc")
                init = parse_moment(raw)
                where = f"v{key} weather_runs[{index}] (часы {run.get('hours', '?')})"
                if init is None:
                    message = f"{where}: init_utc={show(raw)} — не время с зоной"
                    check.add([Problem(label, None, message)])
                else:
                    late = published_late(init, moment)
                    check.add(
                        [Problem(label, None, f"{where}: {late}")] if late else []
                    )

    def check_no_stub(
        self, issue: date, record: dict | None, events: list[tuple[int, dict]] | None
    ) -> None:
        if record is None and events is None:
            return
        problems: list[Problem] = []
        if record is not None:
            label = self.rel(self.root / "outputs" / "forecasts" / f"{issue}.json")
            if record.get("source") == "stub":
                problems.append(
                    Problem(label, None, "source=stub — заглушка вместо данных")
                )
            versions = record.get("versions")
            if isinstance(versions, dict):
                for key, version in versions.items():
                    if isinstance(version, dict) and version.get("source") == "stub":
                        message = (
                            f"версия {key}: source=stub — заглушка на главном пути"
                        )
                        problems.append(Problem(label, None, message))
        if events:
            label = self.rel(self.root / "outputs" / "traces" / f"{issue}.jsonl")
            for number, event in events:
                meta = event.get("meta")
                if isinstance(meta, dict) and meta.get("source") == "stub":
                    message = f"событие {show(event.get('type'))}: meta.source=stub"
                    problems.append(Problem(label, number, message))
        self.checks["no_stub"].add(problems)

    # outputs/forecast_feb2026.csv

    def check_combined(self) -> None:
        check = self.checks["combined"]
        covered = [issue for issue in self.dates if TEST_FROM <= issue <= TEST_TO]
        if not covered:
            check.note = f"диапазон вне теста {TEST_FROM}…{TEST_TO} — не проверяется"
            return
        path = self.root / "outputs" / COMBINED
        label = self.rel(path)
        if not path.is_file():
            check.note = f"нет {label}"
            check.add([Problem(label, None, "нет сводного файла выпусков")])
            return
        try:
            header, raw = read_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            check.note = "файл не читается"
            check.add([Problem(label, None, f"не читается как CSV в UTF-8: {exc}")])
            return
        if header != list(COLUMNS):
            check.note = "заголовок не по §7"
            check.add([Problem(label, 1, header_problem(header))])
            return
        wanted = {issue.isoformat() for issue in covered}
        by_issue: dict[str, Rows] = {}
        for line, fields in raw:
            if len(fields) != len(COLUMNS):
                message = f"{len(fields)} полей вместо {len(COLUMNS)}"
                check.add([Problem(label, line, message)])
                continue
            row = dict(zip(COLUMNS, fields))
            if row["issue_date"] in wanted:
                by_issue.setdefault(row["issue_date"], []).append((line, row))
            elif not in_test_period(row["issue_date"]):
                message = f"issue_date={show(row['issue_date'])} вне теста {TEST_FROM}…{TEST_TO}"
                check.add([Problem(label, line, message)])
            # else: a test issue outside the checked range, not compared this time
        for issue in covered:
            reference = self.csvs.get(issue)
            if reference is None:
                continue  # the issue file is missing or unreadable: reported by its own checks
            self.compare_combined(
                label, issue, reference.rows, by_issue.get(str(issue), [])
            )

    def compare_combined(
        self, label: str, issue: date, expected: Rows, actual: Rows
    ) -> None:
        """The combined rows of one issue equal its file's rows, as a multiset."""
        check = self.checks["combined"]
        wanted: dict[tuple[str, str], list[dict[str, str]]] = {}
        for _, row in expected:
            wanted.setdefault(row_key(row), []).append(row)
        present: dict[tuple[str, str], Rows] = {}
        for line, row in actual:
            present.setdefault(row_key(row), []).append((line, row))
        for key, rows in wanted.items():
            where = f"{issue} turbine={key[0]} h={key[1]}"
            found = present.pop(key, [])
            for index, want in enumerate(rows):
                if index >= len(found):
                    message = f"{where} — нет строки, которая есть в {issue}.csv"
                    check.add([Problem(label, None, message)])
                    continue
                line, row = found[index]
                diff = [c for c in COLUMNS if not same_value(c, want[c], row[c])]
                cells = "; ".join(
                    f"{c}={show(row[c])}, в {issue}.csv {show(want[c])}"
                    for c in diff[:3]
                )
                check.add([Problem(label, line, f"{where} · {cells}")] if diff else [])
            for line, _ in found[len(rows) :]:
                message = f"{where} — лишняя строка, в {issue}.csv их меньше"
                check.add([Problem(label, line, message)])
        for key, rows in present.items():
            for line, _ in rows:
                message = f"{issue} turbine={key[0]} h={key[1]} — лишняя строка, в {issue}.csv её нет"
                check.add([Problem(label, line, message)])

    # outputs/metrics_jan.json

    def check_metrics(self) -> None:
        check = self.checks["metrics"]
        path = self.root / "outputs" / METRICS
        label = self.rel(path)
        if not path.is_file():
            check.note = f"нет {label}"
            message = (
                "нет файла метрик января — нечем подтвердить, что модель лучше базовых "
                "линий (PYTHONPATH=backend python -m windcast.evaluate --from 2025-12-31 --to 2026-01-29)"
            )
            check.add([Problem(label, None, message)])
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            check.note = "файл не читается"
            check.add([Problem(label, None, f"не читается как JSON: {exc}")])
            return
        methods = data.get("methods") if isinstance(data, dict) else None
        if not isinstance(methods, list):
            check.note = "нет methods"
            check.add([Problem(label, None, "нет списка methods (§6.6)")])
            return
        nmae: dict[str, float] = {}
        for method in methods:
            if (
                isinstance(method, dict)
                and isinstance(method.get("key"), str)
                and is_number(method.get("nmae"))
            ):
                nmae[method["key"]] = float(method["nmae"])
        keys = ("model", *BASELINES)
        problems: list[Problem] = []
        missing = [key for key in keys if key not in nmae]
        if missing:
            message = "нет числового nmae для: " + ", ".join(missing)
            problems.append(Problem(label, None, message))
        model = nmae.get("model")
        for baseline in BASELINES:
            if model is not None and baseline in nmae and not model < nmae[baseline]:
                message = f"nMAE модели {model:.4g} не ниже, чем у {baseline} {nmae[baseline]:.4g}"
                problems.append(Problem(label, None, message))
        found = [f"{key} {nmae[key]:.4g}" for key in keys if key in nmae]
        check.note = "nMAE: " + " · ".join(found) if found else "nMAE нет"
        check.add(problems)


# --- output --------------------------------------------------------------------------------


def render(checks: list[Check], root: Path, first: date, last: date) -> str:
    count = (last - first).days + 1
    issues = plural(count, "выпуск", "выпуска", "выпусков")
    lines = [
        f"Windcast · проверка выпусков {first} … {last} ({count} {issues})",
        f"Корень: {root}",
        "T — момент выпуска D: D 19:00 UTC = (D+1) 00:00 по UTC+5",
        (
            "Прогон погоды годен, если опубликован до T: старт + 8 ч ≤ T, "
            "т. е. старт не позже D 11:00 UTC (§2)"
        ),
    ]
    if not (root / "outputs").is_dir():
        lines.append("Внимание: нет каталога outputs/ — верно ли указан --root?")
    lines.append("")
    marks = {"pass": "✓", "fail": "✗", "skip": "–"}
    width = max(len(check.title) for check in checks)
    for check in checks:
        if check.note:
            summary = check.note
        elif check.status == "skip":
            summary = "нечего проверять"
        else:
            summary = f"{check.checked - check.failed}/{check.checked} {check.unit}"
        lines.append(f"  {marks[check.status]} {check.title.ljust(width)}  {summary}")
    failed = [check for check in checks if check.status == "fail"]
    if failed:
        lines += ["", f"Ошибки — первые {SHOW} на проверку (файл:строка · что не так):"]
        for check in failed:
            lines.append(f"  ✗ {check.title}")
            lines += [f"      {problem.render()}" for problem in check.problems[:SHOW]]
            if len(check.problems) > SHOW:
                lines.append(f"      … и ещё {len(check.problems) - SHOW}")
    lines.append("")
    if failed:
        n = len(failed)
        what = plural(
            n, "проверка не пройдена", "проверки не пройдены", "проверок не пройдено"
        )
        lines.append(f"FAIL: {n} {what}")
    else:
        lines.append("PASS")
    return "\n".join(lines)


def as_json(checks: list[Check], root: Path, first: date, last: date) -> dict:
    failed = [check.key for check in checks if check.status == "fail"]
    return {
        "result": "FAIL" if failed else "PASS",
        "failed_checks": failed,
        "root": str(root),
        "from": first.isoformat(),
        "to": last.isoformat(),
        "issues_expected": (last - first).days + 1,
        "checks": [
            {
                "id": check.key,
                "title": check.title,
                "status": check.status,
                "unit": check.unit,
                "checked": check.checked,
                "failed": check.failed,
                "note": check.note,
                "problems_total": len(check.problems),
                "problems": [
                    {"file": p.file, "line": p.line, "message": p.message}
                    for p in check.problems[:JSON_PROBLEMS]
                ],
            }
            for check in checks
        ],
    }


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"неверная дата «{text}»: нужен формат ГГГГ-ММ-ДД"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify.py",
        description=(
            "Проверка выпусков прогноза: полнота, формат CSV (§7), правило «без будущего», "
            "ленты шагов агента, записи выпусков, сводный CSV и метрики января."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="корень репозитория (по умолчанию — каталог над scripts/)",
    )
    parser.add_argument(
        "--from",
        dest="first",
        metavar="ГГГГ-ММ-ДД",
        type=parse_date,
        default=TEST_FROM,
        help="первая дата выпуска, по умолчанию %(default)s",
    )
    parser.add_argument(
        "--to",
        dest="last",
        metavar="ГГГГ-ММ-ДД",
        type=parse_date,
        default=TEST_TO,
        help="последняя дата выпуска, по умолчанию %(default)s",
    )
    parser.add_argument(
        "--json", action="store_true", help="машиночитаемый вывод (JSON)"
    )
    args = parser.parse_args(argv)
    if args.first > args.last:
        parser.error("--from позже, чем --to")
    root = args.root.resolve()
    checks = Verifier(root, args.first, args.last).run()
    if args.json:
        report = as_json(checks, root, args.first, args.last)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(checks, root, args.first, args.last))
    return 1 if any(check.status == "fail" for check in checks) else 0


if __name__ == "__main__":
    try:  # never crash on a console that cannot print ✓ or Cyrillic
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
