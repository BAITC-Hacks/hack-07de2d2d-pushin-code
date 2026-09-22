// Minimal trace panel: subscribes to SSE, renders the agent's decision trail.
// Props: runId (string). No deps beyond React.
import { useEffect, useRef, useState } from "react";

const ICON = { thought: "💭", tool_call: "🔧", tool_result: "↩", action: "⚡", verdict: "✅", error: "⛔" };

export default function TracePanel({ runId }) {
  const [events, setEvents] = useState([]);
  const [done, setDone] = useState(false);
  const bottom = useRef(null);

  useEffect(() => {
    if (!runId) return;
    setEvents([]); setDone(false);
    const es = new EventSource(`/api/runs/${runId}/events`);
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      setEvents((xs) => [...xs, ev]);
      if (ev.type === "verdict" || ev.type === "error") { setDone(true); es.close(); }
    };
    es.onerror = () => { setDone(true); es.close(); };
    return () => es.close();
  }, [runId]);

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [events]);

  if (!runId) return <div className="trace-empty">Введите задачу — агент начнёт работу</div>;

  return (
    <div className="trace">
      {events.map((ev) => <Event key={ev.seq} ev={ev} />)}
      {!done && <div className="trace-running">● агент работает…</div>}
      <div ref={bottom} />
    </div>
  );
}

function Event({ ev }) {
  const [open, setOpen] = useState(ev.type === "action" || ev.type === "verdict");
  const cls = `trace-ev trace-${ev.type}`;
  const time = ev.ts?.slice(11, 19);
  return (
    <div className={cls} onClick={() => setOpen((o) => !o)}>
      <div className="trace-head">
        <span className="trace-icon">{ICON[ev.type] ?? "•"}</span>
        <span className="trace-title">{ev.title}</span>
        <span className="trace-time">{time}</span>
      </div>
      {open && ev.body && <pre className="trace-body">{ev.body}</pre>}
      {ev.type === "verdict" && ev.meta?.artifact && (
        <a className="trace-artifact" href={`/api/artifacts/${ev.meta.artifact}`}>Открыть результат →</a>
      )}
    </div>
  );
}

/* Minimal CSS to drop into the app:
.trace{display:flex;flex-direction:column;gap:6px;max-height:70vh;overflow:auto;padding:8px}
.trace-ev{border-radius:8px;padding:8px 10px;background:#f6f7f9;cursor:pointer}
.trace-thought{color:#666;font-style:italic;background:transparent}
.trace-tool_result{opacity:.75;margin-left:20px}
.trace-action{background:#fff4d6;border-left:4px solid #f5a623}
.trace-verdict{background:#e6f7ec;border-left:4px solid #2fa84f;font-weight:600}
.trace-error{background:#fdecec;border-left:4px solid #d64545}
.trace-head{display:flex;gap:8px;align-items:center}.trace-time{margin-left:auto;font-size:11px;color:#999}
.trace-body{white-space:pre-wrap;font-size:12px;margin:6px 0 0}.trace-running{color:#2fa84f;padding:6px}
*/
