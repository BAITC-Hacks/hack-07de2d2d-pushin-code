"""The dispatcher agent: weather → prep → model → forecast → analysis → recalc → publish.

run_issue drives the six tools of windcast.tools either with an OpenAI tool-calling loop
(mode "agent": the LLM picks the order, decides whether a new weather run is worth a new
version and writes the dispatcher summary) or with a fixed order and the same decision policy
as a rule (mode "deterministic"). Numbers always come from the model, so both modes publish
identical forecasts for the same weather run. Any LLM failure — no key, bad model, network,
a stop before the cycle is complete, the 8-call limit — is traced and the deterministic
driver finishes the cycle from wherever the LLM stopped.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any

import pandas as pd

from windcast import store, timeline, tools

log = logging.getLogger(__name__)

MODES = ("agent", "deterministic")
TRIGGERS = ("issue", "new_weather_run")
SCENARIOS = (None, "weather_outage")
MAX_TOOL_CALLS = 8
DEFAULT_MODEL = "gpt-5-mini"

SYSTEM_PROMPT = """Ты — агент диспетчера ветроэлектростанции: две турбины, ВЭС = среднее двух \
турбин, мощность — доля номинала. Ты сам ведёшь цикл выпуска почасового прогноза на 48 ч \
с интервалом P10–P90: погода → подготовка данных → модель → прогноз → анализ → пересчёт при \
обновлении погоды → публикация.

Правила:
1. Действуешь только через инструменты. Числа прогноза считает модель (run_model, \
recalc_forecast). Ты не придумываешь и не правишь числа: любое число в тексте бери дословно \
из ответов инструментов.
2. Правило «без будущего»: погода только из прогонов, опубликованных до момента выпуска T \
(старт прогона + 8 ч ≤ T). Его проверяют check_data и сам код — обойти нельзя.
3. Выпуск: fetch_weather(run="previous") → check_data → run_model → analyze_forecast → \
publish_forecast — это v1.
4. После v1 придёт событие о новом прогоне погоды. Посмотри его: fetch_weather(run="latest") \
вернёт факты сравнения с текущей версией. Политика пересчёта: пересчитывай, если прогон \
свежее текущего (fresher_than_current), все его прогоны опубликованы до T \
(all_inits_before_issue) и либо \
средний сдвиг ветра за часы 1–24 (mean_abs_wind_shift_h1_24_ms) больше 0,5 м/с, либо \
появился новый риск (new_flags_vs_current). Решение за тобой: перед вызовом recalc_forecast \
одной фразой напиши, почему пересчитываешь; если не пересчитываешь — объясни почему и закончи.
5. recalc_forecast уже анализирует новую версию — после него сразу publish_forecast (v2).
6. Не больше 8 вызовов инструментов на выпуск. Если инструмент вернул сбой источника — \
повтори его один раз.
7. summary в publish_forecast — сводка для диспетчера по-русски, 2–3 предложения: пик, \
минимум и среднее в % номинала с датой и временем, время рисков из анализа и что делать \
(держать резерв, учесть в заявке). Для v2 добавь, что изменилось (change_note).
8. Когда закончил, ответь коротким итогом по-русски без вызова инструментов."""

NEW_RUN_EVENT = (
    'Событие: вышел новый прогон погоды. Посмотри его через fetch_weather(run="latest") '
    "и реши по политике пересчёта."
)


def make_client():
    """OpenAI client from the environment (OPENAI_API_KEY, optional OPENAI_BASE_URL)."""
    from openai import OpenAI

    return OpenAI(
        timeout=float(os.environ.get("OPENAI_TIMEOUT_S", "60")), max_retries=1
    )


def _noop(event: dict) -> None:
    return None


def run_issue(
    issue_date: str,
    *,
    mode: str = "agent",
    trigger: str = "issue",
    emit: Callable[[dict], None] = _noop,
    client: Any = None,
    scenario: str | None = None,
) -> dict:
    """Make (trigger "issue") or update (trigger "new_weather_run") one issue.

    issue_date: "YYYY-MM-DD" or "live". Events go to emit as they happen; a full issue also
    writes them to outputs/traces/{D}.jsonl. ValueError (Russian text) for bad input — the
    API answers 400. Returns {"issue_date", "version", "record_path", "csv_path",
    "trace_path"}; version is None when nothing could be published.
    """
    key = store.issue_key(issue_date)
    if mode not in MODES:
        raise ValueError(f"Неизвестный режим «{mode}»: нужен agent или deterministic")
    if trigger not in TRIGGERS:
        raise ValueError(
            f"Неизвестный запуск «{trigger}»: нужен issue или new_weather_run"
        )
    if scenario not in SCENARIOS:
        raise ValueError(f"Неизвестный сценарий «{scenario}»")
    ctx = _context(key, trigger, scenario)
    tracer = tools.Tracer(key, emit)
    registry = tools.ToolRegistry(ctx, tracer)
    try:
        _intro(ctx, tracer, mode)
        if mode == "agent":
            _run_llm(ctx, registry, tracer, client)
        _drive(ctx, registry, tracer)
        _verdict(ctx, tracer)
    except Exception as exc:
        log.exception("issue %s failed", key)
        tracer.event(
            "error", "Сбой агента", f"{type(exc).__name__}: {exc}", status="error"
        )
        tracer.event(
            "verdict",
            "Итог: выпуск прерван ошибкой",
            f"{type(exc).__name__}: {exc}",
            status="error",
            version=ctx.latest_version or None,
        )
        raise
    finally:
        if trigger == "issue":
            store.write_trace(key, tracer.events)
    return {
        "issue_date": key,
        "version": ctx.latest_version or None,
        "record_path": str(store.record_path(key)),
        "csv_path": str(store.csv_path(key)),
        "trace_path": str(store.trace_path(key)),
    }


def _context(key: str, trigger: str, scenario: str | None) -> tools.RunContext:
    if key == tools.LIVE:
        issue_time = pd.Timestamp(timeline.live_times()[0])
    else:
        issue_time = pd.Timestamp(timeline.issue_time_utc(key))
    ctx = tools.RunContext(
        issue_date=key, trigger=trigger, issue_time_utc=issue_time, scenario=scenario
    )
    if trigger == "new_weather_run":
        record = store.load_record(key)
        if not record or not record.get("versions"):
            label = "Live-выпуск" if key == tools.LIVE else f"Выпуск {key}"
            raise ValueError(f"{label} ещё не опубликован — сначала выпустите его")
        ctx.record = record
        ctx.issue_time_utc = tools.to_utc(record["issue_time_utc"])
        number = int(record["latest_version"])
        version = tools.version_from_record(record, number)
        ctx.versions[number] = version
        ctx.current = version
    return ctx


def _intro(ctx: tools.RunContext, tracer: tools.Tracer, mode: str) -> None:
    how = (
        f"Режим: агент (LLM {os.environ.get('OPENAI_MODEL') or DEFAULT_MODEL})."
        if mode == "agent"
        else "Режим: детерминированный — тот же порядок и те же числа, решения по правилу."
    )
    if ctx.trigger == "new_weather_run":
        number = ctx.latest_version
        tracer.event(
            "thought",
            "Событие: вышел новый прогон погоды",
            f"Текущая версия v{number}. Ищу прогон свежее текущего и не позже момента выпуска"
            f" — и решаю, нужна ли новая версия. {how}",
            stage="recalc",
            version=number,
            source=ctx.event_source(),
        )
        ctx.new_run_announced = True
        return
    if ctx.live:
        title = "Live-выпуск: момент выпуска T — сейчас"
        moment = f"T = {tools.local_label(ctx.issue_time_utc)} местного"
    else:
        day = timeline.parse_issue_date(ctx.issue_date).strftime("%d.%m")
        title = f"Выпуск {day}: беру только погоду, опубликованную до T"
        moment = (
            f"T = {tools.local_label(ctx.issue_time_utc)} местного "
            f"({tools.utc_label(ctx.issue_time_utc)})"
        )
    tracer.event(
        "thought",
        title,
        f"{moment}; горизонт 48 ч. Беру только прогоны погоды, опубликованные до T (старт + 8 ч"
        " ≤ T). План: погода → "
        f"проверка → модель → анализ → v1 → проверка нового прогона. {how}",
        version=1,
    )


def _announce_new_run(ctx: tools.RunContext, tracer: tools.Tracer) -> None:
    ctx.new_run_announced = True
    tracer.event(
        "thought",
        "Событие: вышел новый прогон погоды",
        f"v{ctx.latest_version} опубликована. Проверяю, есть ли прогон свежее, опубликованный "
        "до момента выпуска, и насколько он меняет ветер и риски.",
        stage="recalc",
        version=ctx.latest_version,
        source=ctx.event_source(),
    )


def _should_announce(ctx: tools.RunContext) -> bool:
    return (
        ctx.trigger == "issue"
        and not ctx.new_run_announced
        and 1 in ctx.versions
        and ctx.decision is None
    )


# ---------- deterministic driver (also finishes whatever the LLM left) ----------
def _facts_fresh(ctx: tools.RunContext) -> bool:
    return bool(ctx.facts) and ctx.facts.get("current_version") == ctx.current.number


def _next_step(ctx: tools.RunContext) -> tuple[str, dict] | None:
    if ctx.failed:
        return None
    if ctx.trigger == "issue" and not ctx.versions:
        if "previous" not in ctx.weathers:
            return "fetch_weather", {"run": "previous"}
        check = ctx.checks.get("previous")
        if check is None:
            return "check_data", {"run": "previous"}
        if not check["ok"]:
            return "fail", {
                "reason": "погода не прошла проверку: " + "; ".join(check["notes"])
            }
        if ctx.current is None:
            return "run_model", {}
    current = ctx.current
    if current is None:
        return None
    if not current.published:
        if current.flags is None:
            return "analyze_forecast", {}
        return "publish_forecast", {"summary": tools.summary_text(current)}
    if ctx.decision is None:
        if not _facts_fresh(ctx):
            return "fetch_weather", {"run": "latest"}
        return "decide", {}
    if ctx.decision["recalc"] and not ctx.recalcs:
        return "recalc_forecast", {"reason": ctx.decision["reason"]}
    return None


def _describe(step: tuple[str, dict] | None) -> str:
    if step is None:
        return "ничего"
    name, args = step
    if name == "fail":
        return "остановка: данные непригодны"
    return (
        "решение о пересчёте" if name == "decide" else f"{name}({args.get('run', '')})"
    )


def _drive(
    ctx: tools.RunContext, registry: tools.ToolRegistry, tracer: tools.Tracer
) -> None:
    retried: set[str] = set()
    for _ in range(20):
        step = _next_step(ctx)
        if step is None:
            break
        name, args = step
        if name == "decide":
            registry.decide(tools.policy_recalc(ctx.facts), by="rule")
            continue
        if name == "fail":
            _fail(ctx, tracer, args["reason"], "check_data")
            break
        if (
            name == "fetch_weather"
            and args.get("run") == "latest"
            and _should_announce(ctx)
        ):
            _announce_new_run(ctx, tracer)
        result = registry.call(name, args, origin="rule")
        if name == "recalc_forecast" and not ctx.recalcs:
            ctx.recalcs.append({"status": "error", "error": str(result.get("error"))})
        if result.get("ok") or name == "recalc_forecast":
            if name == "fetch_weather" and args["run"] in retried:
                tracer.event(
                    "thought",
                    "Погода получена со второй попытки — доверие к выпуску ниже обычного",
                    "Источник погоды отказал в первый раз; данные взяты повторным запросом "
                    f"(источник: {result.get('source')}). Проверьте свежесть прогона.",
                    stage="weather",
                    status="warn",
                    version=ctx.event_version(),
                    source=result.get("source"),
                )
            continue
        if name == "fetch_weather" and args["run"] not in retried:
            retried.add(args["run"])
            tracer.event(
                "thought",
                "Источник погоды не ответил — повторяю запрос",
                str(result.get("error", "")),
                stage="weather",
                status="warn",
                version=ctx.event_version(),
            )
            continue
        if name == "fetch_weather" and args["run"] == "latest":
            registry.decide(
                False,
                by="rule",
                reason="Новый прогон недоступен после повторного запроса — текущая версия "
                "остаётся, доверие к ней ниже обычного.",
                status="warn",
            )
            continue
        _fail(ctx, tracer, str(result.get("error") or f"{name} не прошёл"), name)
        break


# ---------- LLM loop ----------
def _headline(text: str, limit: int = 90) -> str:
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _user_prompt(ctx: tools.RunContext) -> str:
    if ctx.trigger == "new_weather_run":
        number = ctx.latest_version
        runs = "; ".join(
            f"часы {r['hours']} — прогон {r['init_utc']}"
            for r in tools.weather_runs(ctx.current.weather, ctx.published)
        )
        return (
            f"Выпуск {ctx.issue_date} уже опубликован, текущая версия v{number} ({runs}). "
            f"{NEW_RUN_EVENT} Если пересчитывать не нужно или нельзя — объясни, почему "
            f"v{number} остаётся."
        )
    if ctx.live:
        return (
            f"Live-выпуск: момент выпуска T = сейчас, {timeline.iso_local(ctx.issue_time_utc)}. "
            "Окно — 48 ч от следующего полного часа. Выполни цикл и опубликуй v1."
        )
    return (
        f"Выпуск {ctx.issue_date}: момент выпуска T = {timeline.iso_local(ctx.issue_time_utc)} "
        f"({timeline.iso_utc(ctx.issue_time_utc)}). Горизонт h = 1…48. "
        "Выполни цикл и опубликуй v1."
    )


def _tool_call_payload(call) -> dict:
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.function.name,
            "arguments": call.function.arguments or "{}",
        },
    }


def _parse_args(raw: str | None) -> dict | None:
    try:
        args = json.loads(raw or "{}")
    except ValueError:
        return None
    return args if isinstance(args, dict) else None


def _run_llm(
    ctx, registry: tools.ToolRegistry, tracer: tools.Tracer, client: Any
) -> None:
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    if client is None:
        if not os.environ.get("OPENAI_API_KEY"):
            tracer.event(
                "thought",
                "Нет ключа OpenAI — веду цикл по регламенту",
                "OPENAI_API_KEY не задан: детерминированный режим — тот же порядок "
                "инструментов, те же числа, решение о пересчёте по правилу, сводка по шаблону.",
                status="warn",
                version=ctx.event_version(),
            )
            return
        try:
            client = make_client()
        except Exception as exc:  # noqa: BLE001 — any client failure means the fallback path
            tracer.event(
                "error",
                "LLM недоступен — веду цикл по регламенту",
                _exc(exc),
                status="error",
            )
            return
    user = _user_prompt(ctx)
    ctx.grounding.extend([SYSTEM_PROMPT, user])
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    kwargs: dict[str, Any] = {"model": model, "tools": registry.schemas()}
    effort = os.environ.get("OPENAI_REASONING_EFFORT", "low").strip()
    if effort and model.startswith("gpt-5"):
        kwargs["reasoning_effort"] = effort
    while True:
        if ctx.tool_calls >= MAX_TOOL_CALLS:
            _limit_reached(ctx, tracer)
            return
        try:
            response = client.chat.completions.create(messages=messages, **kwargs)
            message = response.choices[0].message
        except Exception as exc:  # noqa: BLE001 — no key, bad model, network: fall back
            tracer.event(
                "error",
                "LLM не ответил — довожу цикл по регламенту",
                _exc(exc),
                status="error",
                version=ctx.event_version(),
            )
            return
        ctx.effective_mode = "agent"
        text = (getattr(message, "content", None) or "").strip()
        calls = list(getattr(message, "tool_calls", None) or [])
        assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            assistant["tool_calls"] = [_tool_call_payload(c) for c in calls]
        messages.append(assistant)
        if text:
            bad = tools.ungrounded_numbers(text, ctx.grounding)
            tracer.event(
                "thought",
                _headline(text),
                text
                + (f" [числа не из инструментов: {', '.join(bad[:4])}]" if bad else ""),
                status="warn" if bad else "ok",
                version=ctx.event_version(),
                source=ctx.event_source(),
                by="llm",
            )
        if not calls:
            _llm_stopped(ctx, registry, tracer, text)
            return
        for call in calls:
            if ctx.tool_calls >= MAX_TOOL_CALLS:
                _limit_reached(ctx, tracer)
                return
            args = _parse_args(call.function.arguments)
            if args is None:
                result = {
                    "ok": False,
                    "error": "аргументы — не JSON-объект, повтори вызов",
                }
            else:
                result = registry.call(call.function.name, args, origin="llm")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
        if _should_announce(ctx):
            _announce_new_run(ctx, tracer)
            ctx.grounding.append(NEW_RUN_EVENT)
            messages.append({"role": "user", "content": NEW_RUN_EVENT})


def _exc(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _fail(ctx: tools.RunContext, tracer: tools.Tracer, reason: str, tool: str) -> None:
    ctx.failed = reason
    tracer.event(
        "error",
        "Цикл остановлен: данные непригодны",
        reason,
        tool=tool,
        stage=tools.TOOLS[tool]["result_stage"],
        status="error",
        version=ctx.event_version(),
    )


def _limit_reached(ctx: tools.RunContext, tracer: tools.Tracer) -> None:
    if _next_step(ctx) is None:  # the cycle is complete: an extra call is not an error
        return
    tracer.event(
        "error",
        f"Лимит {MAX_TOOL_CALLS} шагов исчерпан — довожу цикл по регламенту",
        f"Агент сделал {ctx.tool_calls} вызовов инструментов и не закончил цикл. Осталось: "
        f"{_describe(_next_step(ctx))}.",
        status="error",
        version=ctx.event_version(),
    )


def _llm_stopped(
    ctx, registry: tools.ToolRegistry, tracer: tools.Tracer, text: str
) -> None:
    """The LLM answered without tools: a decision to keep the version, or an early stop."""
    current = ctx.current
    if (
        current is not None
        and current.published
        and ctx.decision is None
        and _facts_fresh(ctx)
    ):
        registry.decide(False, by="llm", reason=text)
        return
    remaining = _next_step(ctx)
    if remaining is not None:
        tracer.event(
            "thought",
            "LLM закончил раньше цикла — довожу по регламенту",
            f"Следующий шаг по регламенту: {_describe(remaining)}.",
            status="warn",
            version=ctx.event_version(),
        )


# ---------- verdict ----------
def _keep_reason(ctx: tools.RunContext, number: int) -> str:
    facts = ctx.facts or {}
    if not facts or not facts.get("current_version"):
        return "новый прогон недоступен"
    if not facts.get("all_inits_before_issue", True):
        return "свежий прогон вышел бы после момента выпуска"
    if not facts.get("fresher_than_current"):
        if ctx.live:
            return "новый прогон ещё не вышел"
        return "более свежего прогона до момента выпуска нет"
    shift = tools.fmt_shift(facts.get("mean_abs_wind_shift_h1_24_ms") or 0.0)
    if tools.policy_recalc(facts):
        return f"агент оставил v{number} (сдвиг {shift} м/с)"
    return f"пересчёт не нужен (сдвиг {shift} м/с, новых рисков нет)"


def _verdict(ctx: tools.RunContext, tracer: tools.Tracer) -> None:
    if not ctx.versions:
        tracer.event(
            "verdict",
            "Итог: выпуск не опубликован",
            f"Причина: {ctx.failed or 'цикл не завершён'}. Прежние файлы выпуска не изменены.",
            status="error",
        )
        return
    number = ctx.latest_version
    version = ctx.versions[number]
    made = any(r.get("status") == "recalculated" for r in ctx.recalcs) and number > 1
    refused = next((r for r in ctx.recalcs if r.get("status") == "refused"), None)
    keep = _keep_reason(ctx, number)
    if ctx.trigger == "issue":
        if made:
            title = f"Итог: опубликована v{number} — пересчёт на свежем прогоне"
        elif refused:
            title = f"Итог: опубликована v{number}, пересчёт отклонён кодом"
        else:
            title = f"Итог: опубликована v{number} — {keep}"
        status = "ok"
    elif made:
        title = f"Итог: опубликована v{number} на новом прогоне"
        status = "ok"
    elif refused:
        title = f"Итог: отказ — v{number} остаётся"
        status = "skip"
    else:
        title = f"Итог: v{number} остаётся — {keep}"
        status = "skip"
    parts = [version.summary or tools.summary_text(version)]
    if made and version.change_note and version.change_note not in parts[0]:
        parts.append(f"Что изменилось: {version.change_note}.")
    reason = refused["error"] if refused else (ctx.decision or {}).get("detail")
    if not made and reason:
        parts.insert(0, f"{reason[0].upper()}{reason[1:].rstrip('.')}.")
    tracer.event(
        "verdict",
        title,
        " ".join(parts),
        status=status,
        version=number,
        source=version.weather.get("source"),
    )
