#!/usr/bin/env python3
"""Aggregate tokens / turns / tool-calls from a run's events.jsonl (--json mode).

Usage: summarize-run.py runs/<name>/events.jsonl [more.jsonl ...]

Prints one row per run so ebrowse and agent-browser runs can be compared
side by side on the metric that matters here: token cost per task.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def summarize(path: Path) -> dict:
    turns = tool_calls = 0
    tin = tout = treason = ttot = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        if e.get("type") == "turn_end":
            turns += 1
            u = (e.get("message") or {}).get("usage") or {}
            tin += u.get("input", 0)
            tout += u.get("output", 0)
            treason += u.get("reasoning", 0)
            ttot += u.get("totalTokens", 0)
            tool_calls += len(e.get("toolResults") or [])
    return {
        "run": path.parent.name,
        "turns": turns,
        "tool_calls": tool_calls,
        "in": tin,
        "out": tout,
        "reason": treason,
        "total": ttot,
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 2
    rows = [summarize(Path(p)) for p in argv]
    hdr = f"{'run':<28} {'turns':>5} {'tools':>6} {'in':>9} {'out':>8} {'reason':>7} {'total':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['run']:<28} {r['turns']:>5} {r['tool_calls']:>6} "
            f"{r['in']:>9} {r['out']:>8} {r['reason']:>7} {r['total']:>9}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
