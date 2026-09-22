"""Minimal agent loop: OpenAI tool calling + SSE trace. FastAPI. ~80 lines.
Copy, replace TOOLS with your own functions, keep the event schema."""
import asyncio, json, inspect, os, time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from openai import OpenAI

app, client = FastAPI(), OpenAI(api_key=os.environ["OPENAI_API_KEY"])
RUNS: dict[str, dict] = {}
ALMATY = timezone(timedelta(hours=5))

# ---- tools: plain functions; docstring + annotations become the schema ----
def find_rules(job_kind: str) -> str:
    """Return regulation clauses applicable to a job kind."""
    return "..."

def create_permit(job: str, measures: list[str]) -> str:
    """ACTION: create a permit document; returns its id."""
    return "PERMIT-001"

TOOLS = [find_rules, create_permit]

def schema(fn):
    sig = inspect.signature(fn)
    props = {k: {"type": "array" if v.annotation is list or getattr(v.annotation, "__origin__", None) is list else "string"}
             for k, v in sig.parameters.items()}
    return {"type": "function", "function": {"name": fn.__name__, "description": fn.__doc__ or "",
            "parameters": {"type": "object", "properties": props, "required": list(props)}}}

def emit(run, type_, title, body="", **meta):
    ev = {"seq": len(run["events"]) + 1, "ts": datetime.now(ALMATY).isoformat(),
          "type": type_, "title": title, "body": body, "meta": meta}
    run["events"].append(ev); run["queue"].put_nowait(ev)

async def agent(run_id: str, task: str):
    run = RUNS[run_id]
    msgs = [{"role": "system", "content": "Ты агент-допускающий. Используй инструменты. Последний шаг — всегда действие."},
            {"role": "user", "content": task}]
    for step in range(8):
        r = client.chat.completions.create(model="gpt-5", messages=msgs,
                                           tools=[schema(t) for t in TOOLS])
        m = r.choices[0].message; msgs.append(m)
        if m.content: emit(run, "thought", m.content[:120], m.content)
        if not m.tool_calls:
            emit(run, "verdict", "Итог", m.content or "", artifact=run.get("artifact")); break
        for tc in m.tool_calls:
            args = json.loads(tc.function.arguments)
            emit(run, "tool_call", tc.function.name, json.dumps(args, ensure_ascii=False), tool=tc.function.name, args=args)
            fn = next(t for t in TOOLS if t.__name__ == tc.function.name)
            out = fn(**args)
            kind = "action" if (fn.__doc__ or "").startswith("ACTION") else "tool_result"
            if kind == "action": run["artifact"] = out
            emit(run, kind, f"{tc.function.name} → {str(out)[:60]}", str(out), tool=tc.function.name)
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": str(out)})
    else:
        emit(run, "error", "Лимит шагов", "Агент не завершил задачу за 8 шагов")
    run["done"] = True; run["queue"].put_nowait(None)

@app.post("/api/runs")
async def create_run(body: dict):
    run_id = str(int(time.time() * 1000))
    RUNS[run_id] = {"events": [], "queue": asyncio.Queue(), "done": False, "artifact": None}
    asyncio.create_task(agent(run_id, body["task"]))
    return {"id": run_id}

@app.get("/api/runs/{run_id}/events")
async def events(run_id: str):
    async def gen():
        run = RUNS[run_id]
        for ev in run["events"]: yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        while not run["done"]:
            ev = await run["queue"].get()
            if ev is None: break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    r = RUNS[run_id]; return {"done": r["done"], "artifact": r["artifact"], "events": r["events"]}

@app.get("/health")
async def health(): return {"ok": True}
