"""Issues in a row, each made by the agent (contract §8).

    PYTHONPATH=backend python -m windcast.backtest --from 2026-01-31 --to 2026-02-28 [--mode ...]

One progress line per issue, then outputs/forecast_feb2026.csv from the per-issue CSVs of the
test range. Exit code 1 if any issue failed. Default mode: agent when OPENAI_API_KEY is set,
otherwise deterministic.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from windcast import agent, ports, store, timeline, tools


def _flags_text(entry: dict) -> str:
    counts = tools.flag_counts(entry.get("flags") or [])
    shown = [f"{kind}:{n}" for kind, n in counts.items() if n]
    return ",".join(shown) or "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m windcast.backtest",
        description="Выпуски подряд: агент проходит полный цикл на каждую дату.",
    )
    parser.add_argument(
        "--from", dest="date_from", default=timeline.TEST_FROM.isoformat()
    )
    parser.add_argument("--to", dest="date_to", default=timeline.TEST_TO.isoformat())
    parser.add_argument("--mode", choices=agent.MODES, default=None)
    args = parser.parse_args(argv)
    mode = args.mode or (
        "agent" if os.environ.get("OPENAI_API_KEY") else "deterministic"
    )
    try:
        start = timeline.parse_issue_date(args.date_from)
        end = timeline.parse_issue_date(args.date_to)
    except ValueError as exc:
        parser.error(str(exc))
    if end < start:
        parser.error("--to раньше --from")
    ports_status = ", ".join(f"{k}={v}" for k, v in ports.status().items())
    days = timeline.issue_dates(start, end)
    print(
        f"backtest {start} → {end}: {len(days)} issues · mode={mode} · {ports_status}"
    )
    failed: list[str] = []
    for day in days:
        key = day.isoformat()
        began = time.perf_counter()
        try:
            result = agent.run_issue(key, mode=mode, trigger="issue")
        except Exception as exc:  # noqa: BLE001 — one broken day must not stop the month
            failed.append(key)
            print(f"{key}  FAIL  {type(exc).__name__}: {exc}", flush=True)
            continue
        elapsed = time.perf_counter() - began
        record = store.load_record(key)
        entry = store.version_entry(record) if result["version"] else None
        if entry is None:
            failed.append(key)
            print(f"{key}  FAIL  nothing published ({elapsed:.1f}s)", flush=True)
            continue
        print(
            f"{key}  v{result['version']}  source={entry['source']}  mode={record['mode']}"
            f"  flags={_flags_text(entry)}  ({elapsed:.1f}s)",
            flush=True,
        )
    path, count = store.write_combined_csv()
    print(f"combined: {store.relative(path)} ({count} issues)")
    if failed:
        print(f"FAILED {len(failed)}: {' '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
