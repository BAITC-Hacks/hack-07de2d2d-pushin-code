"""Read-only dispatcher chat for contract §6.8.

The model may choose which of the small file-backed tools to call, but it never
gets access to a writer or to the forecast model.  This makes the answer usable
when OpenAI is unavailable too: the deterministic path is built from exactly
the same issue-summary tool.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from windcast import agent, paths, store

LIVE = "live"
MAX_TOOL_CALLS = 4
TIMEOUT_S = 30.0
_LOCAL_TZ = timezone(timedelta(hours=5))

SYSTEM_PROMPT = """Ты — помощник диспетчера ВЭС. Отвечай только по данным, которые
вернули инструменты: не придумывай числа, даты, часы или риски. Ответь по-русски не
более чем тремя предложениями; даты и часы указывай по Астане (UTC+5). Если данных
нет, скажи об этом прямо. Перед итоговым ответом вызови нужный инструмент.
Про пик, минимум, среднее, риски или изменения выпуска сначала вызывай get_issue_summary —
он покрывает все 48 часов; get_hours отдаёт не больше 12 часов и годится только для деталей
конкретных часов, по нему нельзя судить о пике или минимуме всего выпуска.
Про точность, ошибку, качество модели или надёжность прогноза вызывай get_quality: там ошибка
(nMAE) модели и простых методов на январской проверке и доля часов, когда факт попал в вероятный
диапазон. Факта за февраль нет — так и скажи, точность показана на январе.
Пиши простыми словами: «средняя ошибка», «вероятный диапазон», проценты с запятой (15,8 %),
без обозначений nMAE, P10, P50, P90 и без долей вида 0.1576."""


class AskTimeout(TimeoutError):
    """The full chat request exceeded the contract's 30-second limit."""


def make_client():
    """Use the same OpenAI client factory as the dispatcher agent."""
    return agent.make_client()


def _schemas() -> list[dict[str, Any]]:
    issue = {
        "type": "string",
        "description": "Дата выпуска ГГГГ-ММ-ДД или live.",
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "get_issue_summary",
                "description": "Сводка выбранного выпуска ВЭС и его риски.",
                "parameters": {
                    "type": "object",
                    "properties": {"issue_date": issue},
                    "required": ["issue_date"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_hours",
                "description": "Почасовые значения одной турбины или ВЭС, максимум 12 ч.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_date": issue,
                        "from_h": {"type": "integer", "minimum": 1, "maximum": 48},
                        "to_h": {"type": "integer", "minimum": 1, "maximum": 48},
                        "turbine": {"type": "string", "enum": ["plant", "1", "2"]},
                    },
                    "required": ["issue_date", "from_h", "to_h", "turbine"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_weather_runs",
                "description": "Прогоны погоды, использованные в выбранном выпуске.",
                "parameters": {
                    "type": "object",
                    "properties": {"issue_date": issue},
                    "required": ["issue_date"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_agent_steps",
                "description": "Заголовки шагов и решений агента для выбранного выпуска.",
                "parameters": {
                    "type": "object",
                    "properties": {"issue_date": issue},
                    "required": ["issue_date"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_quality",
                "description": "Методы и покрытие интервала P10–P90 за январь.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_live",
                "description": "Последний live-выпуск из outputs/live/latest.json.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
    ]


TOOL_SCHEMAS = _schemas()


def _jsonable(value: Any) -> Any:
    """Make a file value safe for a tool JSON response, including NaN forecast cells."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _issue_key(value: Any) -> str:
    if value is None:
        return LIVE
    if not isinstance(value, str):
        raise TypeError("Дата выпуска — ГГГГ-ММ-ДД, live или null")
    text = value.strip()
    if not text:
        return LIVE
    return store.issue_key(text)


def _entry(issue_date: Any) -> tuple[str, dict | None, dict | None]:
    issue = _issue_key(issue_date)
    record = store.load_record(issue)
    return issue, record, store.version_entry(record) if record else None


def _pct(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number * 100) if math.isfinite(number) else None


def _local_label(value: Any) -> str | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_LOCAL_TZ)
    return stamp.astimezone(_LOCAL_TZ).strftime("%d.%m %H:%M")


def get_issue_summary(issue_date: Any) -> dict[str, Any]:
    """Latest file-backed plant P50 summary, risks and version metadata."""
    issue, record, version = _entry(issue_date)
    if not record or not version:
        return {"issue_date": issue, "found": False, "message": "Выпуск не найден"}
    plant = [
        row
        for row in version.get("rows") or []
        if isinstance(row, dict)
        and str(row.get("turbine")) == "plant"
        and _pct(row.get("p50")) is not None
    ]
    plant.sort(key=lambda row: int(row.get("h", 0) or 0))
    stats: dict[str, Any] | None = None
    if plant:
        values = [float(row["p50"]) for row in plant]
        high = max(range(len(values)), key=values.__getitem__)
        low = min(range(len(values)), key=values.__getitem__)
        stats = {
            "peak_pct": _pct(values[high]),
            "peak_time_local": _local_label(plant[high].get("target_time_local")),
            "min_pct": _pct(values[low]),
            "min_time_local": _local_label(plant[low].get("target_time_local")),
            "mean_pct": _pct(sum(values) / len(values)),
        }
    versions = []
    for key in record.get("versions") or {}:
        try:
            versions.append(int(key))
        except (TypeError, ValueError):
            continue
    return _jsonable(
        {
            "issue_date": issue,
            "found": True,
            "version": version.get("version", record.get("latest_version")),
            "versions": sorted(versions),
            "plant": stats,
            "flags": version.get("flags") or [],
            "summary": version.get("summary"),
            "change_note": version.get("change_note"),
        }
    )


def get_hours(issue_date: Any, from_h: Any, to_h: Any, turbine: Any) -> dict[str, Any]:
    """Return at most 12 file-backed forecast rows for one requested turbine."""
    issue, record, version = _entry(issue_date)
    if not record or not version:
        return {"issue_date": issue, "found": False, "message": "Выпуск не найден"}
    if isinstance(from_h, bool) or isinstance(to_h, bool):
        raise TypeError("Часы должны быть целыми числами от 1 до 48")
    try:
        start, end = int(from_h), int(to_h)
    except (TypeError, ValueError) as exc:
        raise ValueError("Часы должны быть целыми числами от 1 до 48") from exc
    if not 1 <= start <= 48 or not 1 <= end <= 48 or end < start:
        raise ValueError("Диапазон часов должен быть от 1 до 48")
    limited_end = min(end, start + 11)
    selected = str(turbine).strip()
    if selected not in {"plant", "1", "2"}:
        raise ValueError("Турбина — plant, 1 или 2")
    fields = (
        "h",
        "target_time_local",
        "turbine",
        "p10",
        "p50",
        "p90",
        "wind_fc_ms",
        "temp_fc_c",
    )
    rows = [
        {field: row.get(field) for field in fields}
        for row in version.get("rows") or []
        if isinstance(row, dict)
        and str(row.get("turbine")) == selected
        and start <= int(row.get("h", 0) or 0) <= limited_end
    ]
    rows.sort(key=lambda row: int(row["h"] or 0))
    return _jsonable(
        {
            "issue_date": issue,
            "version": version.get("version", record.get("latest_version")),
            "turbine": selected,
            "from_h": start,
            "to_h": limited_end,
            "truncated": limited_end != end,
            "rows": rows[:12],
        }
    )


def get_weather_runs(issue_date: Any) -> dict[str, Any]:
    issue, record, version = _entry(issue_date)
    if not record or not version:
        return {"issue_date": issue, "found": False, "message": "Выпуск не найден"}
    return _jsonable(
        {
            "issue_date": issue,
            "version": version.get("version", record.get("latest_version")),
            "weather_runs": version.get("weather_runs") or [],
        }
    )


def get_agent_steps(issue_date: Any) -> dict[str, Any]:
    issue = _issue_key(issue_date)
    try:
        events = store.load_trace(issue)
    except (OSError, ValueError):
        events = []
    steps = []
    for event in events:
        if not isinstance(event, dict) or not str(event.get("title") or "").strip():
            continue
        meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}
        steps.append(
            {
                "type": event.get("type"),
                "title": str(event["title"]).strip(),
                "stage": meta.get("stage"),
                "version": meta.get("version"),
            }
        )
    return _jsonable({"issue_date": issue, "steps": steps[-20:]})


def get_quality() -> dict[str, Any]:
    try:
        data = json.loads(paths.metrics_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"found": False, "message": "Метрики не найдены"}
    if not isinstance(data, dict):
        return {"found": False, "message": "Метрики не найдены"}
    return _jsonable(
        {
            "found": True,
            "methods": data.get("methods") or [],
            "coverage_p10_p90": data.get("coverage_p10_p90"),
            "readable": {
                m.get("key"): f"{m.get('nmae', 0) * 100:.1f} %".replace(".", ",")
                for m in data.get("methods") or []
                if isinstance(m.get("nmae"), (int, float))
            },
            "coverage_readable": (
                f"{data['coverage_p10_p90'] * 100:.0f} %"
                if isinstance(data.get("coverage_p10_p90"), (int, float))
                else None
            ),
        }
    )


def get_live() -> dict[str, Any]:
    return get_issue_summary(LIVE)


_TOOLS = {
    "get_issue_summary": lambda args: get_issue_summary(args.get("issue_date")),
    "get_hours": lambda args: get_hours(
        args.get("issue_date"),
        args.get("from_h"),
        args.get("to_h"),
        args.get("turbine"),
    ),
    "get_weather_runs": lambda args: get_weather_runs(args.get("issue_date")),
    "get_agent_steps": lambda args: get_agent_steps(args.get("issue_date")),
    "get_quality": lambda args: get_quality(),
    "get_live": lambda args: get_live(),
}


def _run_tool(name: str, args: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    clean_args = args if isinstance(args, dict) else {}
    tool = _TOOLS.get(name)
    if tool is None:
        return clean_args, {"error": f"Неизвестный инструмент: {name}"}
    try:
        return clean_args, _jsonable(tool(clean_args))
    except (TypeError, ValueError) as exc:
        return clean_args, {"error": str(exc)}


def _tool_payload(call: Any) -> dict[str, Any]:
    function = getattr(call, "function", None)
    return {
        "id": str(getattr(call, "id", "")),
        "type": "function",
        "function": {
            "name": str(getattr(function, "name", "")),
            "arguments": getattr(function, "arguments", "{}") or "{}",
        },
    }


def _call_args(call: Any) -> tuple[str, dict[str, Any]]:
    function = getattr(call, "function", None)
    name = str(getattr(function, "name", ""))
    try:
        args = json.loads(getattr(function, "arguments", "{}") or "{}")
    except (TypeError, ValueError):
        args = {}
    return name, args if isinstance(args, dict) else {}


def _limit_sentences(answer: str) -> str:
    pieces = re.split(r"(?<=[.!?])\s+", answer.strip())
    return " ".join(pieces[:3]).strip()


def _plain(value: Any) -> str:
    return str(value or "").strip().rstrip(".!? ")


def _deterministic_answer(summary: dict[str, Any], *, no_key: bool) -> str:
    issue = summary.get("issue_date") or LIVE
    plant = summary.get("plant") if isinstance(summary.get("plant"), dict) else None
    if not summary.get("found") or not plant or plant.get("peak_pct") is None:
        parts = [f"Для выпуска {issue} данных нет."]
    else:
        parts = [
            (
                f"Выпуск {issue}: пик {plant['peak_pct']} % номинала — "
                f"{plant.get('peak_time_local') or 'время не указано'}, минимум "
                f"{plant.get('min_pct')} %, в среднем {plant.get('mean_pct')} %."
            )
        ]
        flags = summary.get("flags") if isinstance(summary.get("flags"), list) else []
        risk = "; ".join(
            _plain(item.get("text") or item.get("kind"))
            for item in flags
            if isinstance(item, dict) and (item.get("text") or item.get("kind"))
        )
        note = _plain(summary.get("change_note"))
        parts.append(
            f"Риски: {risk or 'флаги риска не отмечены'}{'; ' + note if note else ''}."
        )
    if no_key:
        parts.append("Режим: без ключа OpenAI отвечаю сводкой.")
    return " ".join(parts)


_QUALITY_WORDS = ("точн", "ошиб", "качеств", "надёжн", "надежн", "nmae", "метрик")


def _is_quality_question(question: str) -> bool:
    text = question.lower()
    return any(word in text for word in _QUALITY_WORDS)


def _quality_answer(quality: dict[str, Any]) -> str:
    if not quality.get("found"):
        return "Метрики точности не найдены."
    methods = {m.get("key"): m.get("nmae") for m in quality.get("methods") or []}
    model = methods.get("model")
    baselines = {k: v for k, v in methods.items() if k != "model" and v is not None}
    parts = []
    if model is not None:
        parts.append(
            f"На январской проверке (30 выпусков, факт известен) средняя ошибка модели — "
            f"{model * 100:.1f} % номинала".replace(".", ",")
        )
        if baselines:
            best_key = min(baselines, key=baselines.get)
            names = {
                "power_curve": "кривой мощности",
                "climatology": "климатологии",
                "persistence": "персистентности",
            }
            best = baselines[best_key]
            gain = (1 - model / best) * 100
            parts[-1] += (
                f", у лучшего простого метода ({names.get(best_key, best_key)}) — "
                f"{best * 100:.1f} %, модель точнее на {gain:.0f} %".replace(".", ",")
            )
    coverage = quality.get("coverage_p10_p90")
    if coverage is not None:
        parts.append(
            f"Факт попал в вероятный диапазон в {coverage * 100:.0f} % часов (цель — 80 %)"
        )
    parts.append("Факта за февраль нет, поэтому точность показана на январе")
    return ". ".join(parts) + "."


def _has_openai_key() -> bool:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    return bool(key) and "..." not in key


def ask(question: str, issue_date: str | None) -> dict[str, Any]:
    """Answer a dispatcher question through at most four read-only tool calls."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Вопрос не должен быть пустым")
    if len(question) > 500:
        raise ValueError("Вопрос не должен быть длиннее 500 символов")
    selected_issue = _issue_key(issue_date)
    tools_used: list[dict[str, Any]] = []
    if not _has_openai_key() and _is_quality_question(question):
        args, quality = _run_tool("get_quality", {})
        tools_used.append({"name": "get_quality", "args": args})
        return {
            "answer": _quality_answer(quality),
            "mode": "deterministic",
            "tools": tools_used,
            "issue_date": selected_issue,
        }
    if not _has_openai_key():
        args, summary = _run_tool("get_issue_summary", {"issue_date": selected_issue})
        tools_used.append({"name": "get_issue_summary", "args": args})
        return {
            "answer": _deterministic_answer(summary, no_key=True),
            "mode": "deterministic",
            "tools": tools_used,
            "issue_date": selected_issue,
        }

    deadline = time.monotonic() + TIMEOUT_S
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Вопрос: {question.strip()}\nВыбранный выпуск: {selected_issue}.",
        },
    ]
    client = make_client()
    final = ""
    grounding: list[str] = []
    turns_without_tools = 0
    while len(tools_used) < MAX_TOOL_CALLS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AskTimeout("Превышено время ожидания ответа агента")
        try:
            response = client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL") or agent.DEFAULT_MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                timeout=remaining,
            )
            message = response.choices[0].message
        except AskTimeout:
            raise
        except TimeoutError as exc:
            raise AskTimeout("Превышено время ожидания ответа агента") from exc
        except Exception as exc:
            if type(exc).__name__ == "APITimeoutError":
                raise AskTimeout("Превышено время ожидания ответа агента") from exc
            raise
        text = str(getattr(message, "content", "") or "").strip()
        calls = list(getattr(message, "tool_calls", None) or [])
        remaining_calls = MAX_TOOL_CALLS - len(tools_used)
        calls = calls[:remaining_calls]
        assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            assistant["tool_calls"] = [_tool_payload(call) for call in calls]
        messages.append(assistant)
        if not calls:
            if tools_used and text:
                final = text
                break
            turns_without_tools += 1
            if turns_without_tools >= 2:
                break
            messages.append(
                {
                    "role": "user",
                    "content": "Сначала вызови один из инструментов, затем ответь по его данным.",
                }
            )
            continue
        for call in calls:
            name, raw_args = _call_args(call)
            args, result = _run_tool(name, raw_args)
            tools_used.append({"name": name, "args": args})
            grounding.append(json.dumps(result, ensure_ascii=False, allow_nan=False))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(getattr(call, "id", "")),
                    "content": json.dumps(result, ensure_ascii=False, allow_nan=False),
                }
            )
    if time.monotonic() > deadline:
        raise AskTimeout("Превышено время ожидания ответа агента")
    if final and agent.tools.ungrounded_numbers(final, grounding):
        final = ""
    if not final:
        _, summary = _run_tool("get_issue_summary", {"issue_date": selected_issue})
        final = _deterministic_answer(summary, no_key=False)
    return {
        "answer": _limit_sentences(final),
        "mode": "agent",
        "tools": tools_used,
        "issue_date": selected_issue,
    }
