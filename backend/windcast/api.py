"""HTTP API for the dispatcher UI — docs/CONTRACT.md §6.

Reads what the agent writes (§5.1), fresh on every request: records outputs/forecasts/{D}.json
(live: outputs/live/latest.json), CSVs outputs/forecasts/{D}.csv, traces outputs/traces/{D}.jsonl
and outputs/metrics_jan.json. A missing or half-written file counts as missing. Agent runs go
through windcast.runs (background thread + SSE).

    PYTHONPATH=backend uvicorn windcast.api:app --port 8000     # one worker: runs live in memory

On startup the live watcher (windcast.watcher, LIVE_WATCH_MINUTES) begins checking the weather.

Every 4xx is {"error": "<текст по-русски>"}; unexpected failures are 500 with the same shape.
"""

from __future__ import annotations

import csv
import importlib
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, NoReturn

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException

from windcast import paths, runs, timeline, watcher

LIVE = "live"
MODES = ("agent", "deterministic")
TRIGGERS = ("issue", "new_weather_run")
JOURNAL_MAX = 200
TURBINE_FILTERS = ("all", "plant", "1", "2")
FLAG_KINDS = ("ramp", "ice", "wind_gt20", "models_diverge")
CSV_COLUMNS = (
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
RUN_FROM, RUN_TO = timeline.BACKTEST_FROM, timeline.TEST_TO  # dates a run may target
MAX_RANGE_DAYS = 366
ECMWF_CYCLE_H = 6  # ECMWF IFS runs at 00/06/12/18 UTC
ECMWF_DELAY_H = 7  # a run becomes available ~7 h after its init time
SSE_HEADERS = {
    # no-transform keeps compressing proxies (Caddy `encode`) from buffering the stream
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}
_DEFAULT_ERRORS = {
    404: "Не найдено",
    405: "Метод не поддерживается",
}


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    watcher.start()  # off when LIVE_WATCH_MINUTES=0
    try:
        yield
    finally:
        watcher.stop()


app = FastAPI(
    title="Windcast API",
    version="0.3",
    description="Агент диспетчера ВЭС: прогноз выработки на 48 ч (docs/CONTRACT.md §6).",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
    redoc_url=None,
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --- errors -------------------------------------------------------------------------------


def _fail(status: int, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail=message)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    try:
        phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:
        phrase = ""
    if not isinstance(detail, str) or not detail or detail == phrase:
        detail = _DEFAULT_ERRORS.get(exc.status_code, f"Ошибка {exc.status_code}")
        if exc.status_code == 404:
            detail = f"{detail}: {request.url.path}"
    return JSONResponse(
        {"error": detail},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


_VALIDATION_TEXT = {
    "missing": "обязательное поле",
    "json_invalid": "некорректный JSON",
    "dict_type": "нужен JSON-объект",
}


@app.exception_handler(RequestValidationError)
async def _validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    parts = []
    for err in exc.errors()[:3]:
        where = ".".join(str(x) for x in err.get("loc", ()) if x != "body") or "запрос"
        parts.append(
            f"{where}: {_VALIDATION_TEXT.get(err.get('type'), err.get('msg'))}"
        )
    return JSONResponse(
        {"error": "Неверный запрос — " + "; ".join(parts)}, status_code=400
    )


@app.exception_handler(Exception)
async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": f"Внутренняя ошибка: {type(exc).__name__}: {exc}"}, status_code=500
    )


# --- parameters ---------------------------------------------------------------------------


def _date_or_400(value: Any) -> date:
    try:
        return timeline.parse_issue_date(value)
    except (TypeError, ValueError) as exc:
        _fail(400, str(exc))


def _issue_param(value: str) -> str:
    """Issue date from a path: "live" or YYYY-MM-DD (400 otherwise)."""
    if value == LIVE:
        return LIVE
    return _date_or_400(value).isoformat()


def _range_params(
    lo: str | None, hi: str | None, default_lo: date, default_hi: date
) -> tuple[date, date]:
    start = _date_or_400(lo) if lo else default_lo
    end = _date_or_400(hi) if hi else default_hi
    if start > end:
        _fail(400, f"Начало периода {start} позже конца {end}")
    if (end - start).days >= MAX_RANGE_DAYS:
        _fail(400, f"Слишком большой период: не больше {MAX_RANGE_DAYS} дней")
    return start, end


def _version_param(value: str | None) -> int | None:
    """None for "latest" (or empty), else a positive version number (400 otherwise)."""
    if value is None or value.strip().lower() in ("", "latest"):
        return None
    text = value.strip().lower().removeprefix("v")
    if not text.isdigit() or int(text) < 1:
        _fail(400, f"Неверная версия «{value}»: latest или номер (1, 2, …)")
    return int(text)


def _choice(value: Any, choices: tuple[str, ...], name: str, default: str) -> str:
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text not in choices:
        _fail(400, f"Неверное значение {name} «{value}»: {' | '.join(choices)}")
    return text


def _mode_param(value: Any) -> str:
    mode = _choice(value, MODES, "mode", runs.default_mode())
    if mode == "agent" and not runs.has_openai_key():
        return "deterministic"  # contract §5: without OPENAI_API_KEY the agent is deterministic
    return mode


def _scenario_param(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _choice(value, runs.SCENARIOS, "scenario", "")


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    data: Any = {}
    if raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            _fail(400, "Тело запроса — не JSON")
    if not isinstance(data, dict):
        _fail(400, "Тело запроса должно быть JSON-объектом")
    merged: dict[str, Any] = dict(request.query_params)
    merged.update(data)
    return merged


# --- files written by the agent -------------------------------------------------------------


def _read_json(path: Path) -> Any:
    """Parsed JSON, or None when the file is missing, unreadable or half-written."""
    try:
        return runs.jsonable(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    events = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a line still being written
        if isinstance(event, dict):
            events.append(runs.jsonable(event))
    return events


def _read_bytes(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def _record_path(issue: str) -> Path:
    if issue == LIVE:
        return paths.live_dir() / "latest.json"
    return paths.forecasts_dir() / f"{issue}.json"


def _trace_path(issue: str) -> Path:
    if issue == LIVE:
        return paths.live_dir() / "latest_trace.jsonl"
    return paths.traces_dir() / f"{issue}.jsonl"


def _csv_path(issue: str) -> Path:
    if issue == LIVE:
        return paths.live_dir() / "latest.csv"
    return paths.forecasts_dir() / f"{issue}.csv"


def _versions(record: dict) -> dict[int, dict]:
    raw = record.get("versions")
    items: list[tuple[Any, Any]] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = [(v.get("version"), v) for v in raw if isinstance(v, dict)]
    out: dict[int, dict] = {}
    for key, value in items:
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            out[number] = value
    return dict(sorted(out.items()))


def _latest_version(record: dict, versions: dict[int, dict]) -> int:
    try:
        latest = int(record.get("latest_version"))
    except (TypeError, ValueError):
        latest = -1
    return latest if latest in versions else max(versions)


def _load_record(issue: str) -> dict | None:
    """The issue record (§5.1), or None if there is none with at least one version."""
    record = _read_json(_record_path(issue))
    if not isinstance(record, dict) or not _versions(record):
        return None
    return record


def _missing_issue_text(issue: str) -> str:
    if issue == LIVE:
        return "Live-выпуска ещё нет — запустите «Выпустить прогноз сейчас»"
    return f"Выпуск {issue} ещё не посчитан"


def _record_or_404(issue: str) -> dict:
    record = _load_record(issue)
    if record is None:
        _fail(404, _missing_issue_text(issue))
    return record


def _pick_version(record: dict, issue: str, wanted: int | None) -> tuple[int, dict]:
    versions = _versions(record)
    number = _latest_version(record, versions) if wanted is None else wanted
    if number not in versions:
        _fail(404, f"У выпуска {issue} нет версии {number}")
    return number, versions[number]


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_local(value: Any) -> Any:
    parsed = _parse_dt(value)
    return timeline.iso_local(parsed) if parsed else value


def _issue_times(issue: str, record: dict) -> tuple[Any, Any]:
    local, utc = record.get("issue_time_local"), record.get("issue_time_utc")
    if issue != LIVE:
        local = local or timeline.iso_local(timeline.issue_time_local(issue))
        utc = utc or timeline.iso_utc(timeline.issue_time_utc(issue))
    return local, utc


def _hours_span(value: Any) -> tuple[int, int]:
    if isinstance(value, str):
        lo, sep, hi = value.replace("–", "-").replace("—", "-").partition("-")
        if sep and lo.strip().isdigit() and hi.strip().isdigit():
            return int(lo), int(hi)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            pass
    return 1, timeline.HORIZON


def _weather_runs(version: dict, issue_time_utc: Any) -> list[dict]:
    """weather_runs with `before_issue` filled in when the writer left it out."""
    moment = _parse_dt(issue_time_utc)
    out = []
    for item in version.get("weather_runs") or []:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        init = _parse_dt(item.get("init_utc"))
        if "before_issue" not in item and moment and init:
            item["before_issue"] = init <= moment
        out.append(item)
    return out


def _row(row: dict) -> dict:
    row = dict(row)
    if "turbine" in row:
        row["turbine"] = str(row["turbine"])
    row.setdefault("actual", None)
    return row


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _flag_counts(flags: Any) -> dict[str, int]:
    counts = dict.fromkeys(FLAG_KINDS, 0)
    for flag in flags or []:
        kind = flag.get("kind") if isinstance(flag, dict) else None
        if isinstance(kind, str) and kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


# --- views --------------------------------------------------------------------------------


def _forecast_view(
    record: dict, issue: str, wanted: int | None, turbine: str
) -> dict[str, Any]:
    number, version = _pick_version(record, issue, wanted)
    versions = _versions(record)
    local, utc = _issue_times(issue, record)
    rows = [_row(r) for r in version.get("rows") or [] if isinstance(r, dict)]
    if turbine != "all":
        rows = [r for r in rows if r.get("turbine") == turbine]
    view: dict[str, Any] = {
        "issue_date": record.get("issue_date") or issue,
        "issue_time_local": local,
        "issue_time_utc": utc,
        "version": number,
        "versions": list(versions),
        "latest_version": _latest_version(record, versions),
        "mode": record.get("mode"),
        "recorded_at": record.get("recorded_at"),
        "created_at": version.get("created_at"),
        "source": version.get("source"),
        "weather_runs": _weather_runs(version, utc),
        "change_note": version.get("change_note"),
        "summary": version.get("summary"),
        "flags": version.get("flags") or [],
        "rows": rows,
    }
    for source in (record, version):  # keep anything else the writer added
        for key, value in source.items():
            if key != "versions":
                view.setdefault(key, value)
    return view


def _issue_entry(day: date, running: set[str]) -> dict[str, Any]:
    iso = day.isoformat()
    entry: dict[str, Any] = {
        "issue_date": iso,
        "issue_time_local": timeline.iso_local(timeline.issue_time_local(day)),
        "status": "missing",
        "version": None,
        "versions": [],
        "mean_p50": None,
        "peak_p50": None,
        "flags": _flag_counts(None),
        "source": None,
    }
    record = _load_record(iso)
    if record is not None:
        versions = _versions(record)
        number = _latest_version(record, versions)
        version = versions[number]
        p50 = [
            r["p50"]
            for r in version.get("rows") or []
            if isinstance(r, dict)
            and str(r.get("turbine")) == "plant"
            and _is_number(r.get("p50"))
        ]
        entry.update(
            status="published",
            issue_time_local=record.get("issue_time_local")
            or entry["issue_time_local"],
            version=number,
            versions=list(versions),
            mean_p50=round(sum(p50) / len(p50), 4) if p50 else None,
            peak_p50=round(max(p50), 4) if p50 else None,
            flags=_flag_counts(version.get("flags")),
            source=version.get("source"),
        )
    if iso in running:
        entry["status"] = "running"
    return entry


def _fmt(value: Any, digits: int) -> str:
    return f"{value:.{digits}f}" if _is_number(value) else ""


def _csv_from_record(record: dict, issue: str, number: int, version: dict) -> str:
    """§7 CSV of one version, built from the record rows."""
    runs_by_span = []
    for item in version.get("weather_runs") or []:
        if isinstance(item, dict) and (init := _parse_dt(item.get("init_utc"))):
            runs_by_span.append((*_hours_span(item.get("hours")), init))
    local, _ = _issue_times(issue, record)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in version.get("rows") or []:
        if not isinstance(row, dict):
            continue
        h = row.get("h", row.get("horizon_h"))
        init_max = row.get("weather_init_max_utc")
        if not init_max:
            covering = [
                init for lo, hi, init in runs_by_span if _is_number(h) and lo <= h <= hi
            ]
            pool = covering or [init for _, _, init in runs_by_span]
            init_max = timeline.iso_utc(max(pool)) if pool else ""
        writer.writerow(
            [
                record.get("issue_date") or issue,
                local or "",
                row.get("target_time_local") or "",
                h if h is not None else "",
                row.get("turbine", ""),
                _fmt(row.get("p10"), 4),
                _fmt(row.get("p50"), 4),
                _fmt(row.get("p90"), 4),
                _fmt(row.get("wind_fc_ms"), 2),
                init_max,
                number,
            ]
        )
    return buf.getvalue()


def _csv_response(content: bytes | str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _latest_available_run(now: datetime) -> datetime:
    ready = now.astimezone(timezone.utc) - timedelta(hours=ECMWF_DELAY_H)
    return ready.replace(
        hour=ready.hour - ready.hour % ECMWF_CYCLE_H, minute=0, second=0, microsecond=0
    )


def _live_current() -> dict[str, Any] | None:
    record = _load_record(LIVE)
    if record is None:
        return None
    number, version = _pick_version(record, LIVE, None)
    issued = (
        version.get("created_at")
        or record.get("issue_time_local")
        or record.get("recorded_at")
    )
    inits = [
        init
        for item in version.get("weather_runs") or []
        if isinstance(item, dict) and (init := _parse_dt(item.get("init_utc")))
    ]
    return {
        "version": number,
        "issued_at_local": _as_local(issued),
        "weather_run_utc": timeline.iso_utc(max(inits)) if inits else None,
    }


def _metrics_or_404() -> dict:
    data = _read_json(paths.metrics_file())
    if not isinstance(data, dict):
        _fail(404, "Метрики ещё не посчитаны")
    return data


def _model_version() -> str:
    try:
        return str(importlib.import_module("windcast.model").MODEL_VERSION)
    except Exception:  # noqa: BLE001 — a broken or absent model must not break /health
        return "stub"


def _run_or_404(run_id: str) -> runs.Run:
    run = runs.get(run_id)
    if run is None:
        _fail(404, f"Прогон {run_id} не найден")
    return run


# --- §6.7 service -------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    ready = sum(1 for d in timeline.issue_dates() if _load_record(d.isoformat()))
    return {
        "ok": True,
        "mode": runs.default_mode(),
        "model_version": _model_version(),
        "issues_ready": ready,
        **watcher.status(),
    }


# --- §6.1 calendar ------------------------------------------------------------------------


@app.get("/api/issues")
def list_issues(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> list[dict[str, Any]]:
    start, end = _range_params(date_from, date_to, timeline.TEST_FROM, timeline.TEST_TO)
    running = runs.running_issue_dates()
    return [_issue_entry(day, running) for day in timeline.issue_dates(start, end)]


# --- §6.2 / §6.5 issues (static routes before /api/forecasts/{issue_date}) -----------------


@app.get("/api/forecasts/february.csv")
def february_csv() -> Response:
    data = _read_bytes(paths.outputs_dir() / "forecast_feb2026.csv")
    if data is None:
        _fail(404, "Сводный CSV за февраль ещё не собран")
    return _csv_response(data, "forecast_feb2026.csv")


@app.get("/api/forecasts/live")
def live_forecast(version: str = "latest", turbine: str = "all") -> dict[str, Any]:
    wanted = _version_param(version)
    turbine = _choice(turbine, TURBINE_FILTERS, "turbine", "all")
    return _forecast_view(_record_or_404(LIVE), LIVE, wanted, turbine)


@app.get("/api/forecasts/{issue_date}.csv")
def forecast_csv(issue_date: str, version: str = "latest") -> Response:
    issue = _issue_param(issue_date)
    wanted = _version_param(version)
    csv_file = _csv_path(issue)
    record = _load_record(issue)
    if record is None:  # a CSV without a record (e.g. from the CLI) is still served
        data = _read_bytes(csv_file) if csv_file and wanted is None else None
        if data is None:
            _fail(404, _missing_issue_text(issue))
        return _csv_response(data, f"{issue}.csv")
    number, chosen = _pick_version(record, issue, wanted)
    latest = _latest_version(record, _versions(record))
    if number == latest and csv_file is not None:
        data = _read_bytes(csv_file)
        if data is not None:
            return _csv_response(data, f"{issue}.csv")
    name = f"{issue}.csv" if number == latest else f"{issue}_v{number}.csv"
    return _csv_response(_csv_from_record(record, issue, number, chosen), name)


@app.get("/api/forecasts/{issue_date}")
def forecast(issue_date: str, version: str = "latest", turbine: str = "all") -> dict:
    issue = _issue_param(issue_date)
    wanted = _version_param(version)
    turbine = _choice(turbine, TURBINE_FILTERS, "turbine", "all")
    return _forecast_view(_record_or_404(issue), issue, wanted, turbine)


# --- §6.3 runs and traces -------------------------------------------------------------------


@app.post("/api/runs")
async def create_run(request: Request) -> dict[str, str]:
    body = await _json_body(request)
    raw_issue = body.get("issue_date")
    if raw_issue is None or raw_issue == "":
        _fail(400, "Не указана дата выпуска: issue_date = ГГГГ-ММ-ДД или live")
    if isinstance(raw_issue, str) and raw_issue.strip().lower() == LIVE:
        issue = LIVE
    else:
        day = _date_or_400(raw_issue)
        if not RUN_FROM <= day <= RUN_TO:
            _fail(400, f"Дата выпуска {day} вне периода {RUN_FROM} … {RUN_TO}")
        issue = day.isoformat()
    mode = _mode_param(body.get("mode"))
    trigger = _choice(body.get("trigger"), TRIGGERS, "trigger", "issue")
    scenario = _scenario_param(body.get("scenario"))
    run = runs.start_issue(issue, mode=mode, trigger=trigger, scenario=scenario)
    return {"id": run.id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return _run_or_404(run_id).to_dict()


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> Response:
    run = _run_or_404(run_id)
    last_id = request.headers.get("last-event-id") or request.query_params.get("after")
    after = int(last_id) if last_id and last_id.strip().isdigit() else 0
    after = min(after, run.event_count())
    if after and run.done and after >= run.event_count():
        return Response(status_code=204)  # tells EventSource not to reconnect
    return StreamingResponse(
        runs.stream(run, after), media_type="text/event-stream", headers=SSE_HEADERS
    )


@app.get("/api/traces/{issue_date}")
def trace(issue_date: str, version: str = "latest") -> dict[str, Any]:
    issue = _issue_param(issue_date)
    wanted = _version_param(version)
    path = _trace_path(issue)
    events = _read_jsonl(path)
    if not events:
        _fail(404, f"Лента шагов выпуска {issue} не найдена")
    record = _load_record(issue)
    recorded_at = record.get("recorded_at") if record else None
    if not recorded_at:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            recorded_at = timeline.iso_local(mtime)
        except OSError:
            recorded_at = None
    if record is not None:
        versions = _versions(record)
        number = _latest_version(record, versions)
        if wanted is not None and wanted not in versions:
            _fail(404, f"У выпуска {issue} нет версии {wanted}")
    else:
        seen = [
            e["meta"]["version"]
            for e in events
            if isinstance(e.get("meta"), dict) and _is_number(e["meta"].get("version"))
        ]
        number = int(max(seen)) if seen else None
    if wanted is not None:  # the trace as of that version
        number = wanted
        events = [
            e
            for e in events
            if not (
                isinstance(e.get("meta"), dict)
                and _is_number(e["meta"].get("version"))
                and e["meta"]["version"] > wanted
            )
        ]
    return {
        "issue_date": issue,
        "version": number,
        "recorded_at": recorded_at,
        "events": events,
    }


# --- §6.4 backtest ------------------------------------------------------------------------


@app.post("/api/backtest")
async def create_backtest(request: Request) -> dict[str, str]:
    body = await _json_body(request)
    start = _date_or_400(body.get("from") or timeline.TEST_FROM)
    end = _date_or_400(body.get("to") or timeline.TEST_TO)
    for day in (start, end):
        if not RUN_FROM <= day <= RUN_TO:
            _fail(400, f"Дата {day} вне периода {RUN_FROM} … {RUN_TO}")
    if start > end:
        _fail(400, f"Начало периода {start} позже конца {end}")
    mode = _mode_param(body.get("mode"))
    run = runs.start_backtest(timeline.issue_dates(start, end), mode=mode)
    return {"id": run.id}


# --- §6.5 live ----------------------------------------------------------------------------


@app.get("/api/live/status")
def live_status() -> dict[str, Any]:
    now = _now()
    latest = _latest_available_run(now)
    upcoming = latest + timedelta(hours=ECMWF_CYCLE_H)
    return {
        "now_local": timeline.iso_local(now),
        "latest_run_utc": timeline.iso_utc(latest),
        "next_run_utc": timeline.iso_utc(upcoming),
        "next_run_available_local": timeline.iso_local(
            upcoming + timedelta(hours=ECMWF_DELAY_H)
        ),
        "current": _live_current(),
    }


@app.get("/api/live/journal")
def live_journal(limit: str = "20") -> list[dict[str, Any]]:
    text = limit.strip()
    if not text.isdigit() or not 1 <= int(text) <= JOURNAL_MAX:
        _fail(400, f"Неверный limit «{limit}»: число от 1 до {JOURNAL_MAX}")
    return runs.read_journal(int(text))


# --- §6.6 quality (January) ---------------------------------------------------------------


@app.get("/api/metrics")
def metrics(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> dict[str, Any]:
    _range_params(date_from, date_to, timeline.BACKTEST_FROM, timeline.BACKTEST_TO)
    return {k: v for k, v in _metrics_or_404().items() if k != "series"}


@app.get("/api/metrics/series")
def metrics_series(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    turbine: str = "plant",
) -> list[dict[str, Any]]:
    start = _date_or_400(date_from).isoformat() if date_from else None
    end = _date_or_400(date_to).isoformat() if date_to else None
    if start and end and start > end:
        _fail(400, f"Начало периода {start} позже конца {end}")
    turbine = _choice(turbine, TURBINE_FILTERS, "turbine", "plant")
    rows = []
    for row in _metrics_or_404().get("series") or []:
        if not isinstance(row, dict):
            continue
        day = str(row.get("target_time_local") or "")[:10]
        if (start and day < start) or (end and day > end):
            continue
        if turbine != "all" and str(row.get("turbine", "plant")) != turbine:
            continue
        rows.append(row)
    rows.sort(key=lambda r: str(r.get("target_time_local") or ""))
    return rows
