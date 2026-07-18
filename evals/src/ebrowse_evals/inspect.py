"""Canned inspection queries over a trace run directory.

Entity-centric root-causing without reading the whole trace: `overview` is the
entry point, `anomalies`/`errors` the triage lists, `step`/`trace-ref`/
`trace-section` the drill-downs, `timing` the latency lens, `grep` the escape
hatch. All output is concise deterministic plain text (golden-testable);
every command takes --json for structured output.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from ebrowse_evals.trace.records import (
    Anomaly,
    BrowserEvent,
    EbrowseLog,
    Record,
    RunEnd,
    RunMeta,
    Step,
    Summary,
)
from ebrowse_evals.trace.store import TraceReader

_REF_RE = re.compile(r"@e\d+\b")
_SID_RE = re.compile(r"\bs\d+\b")


def open_reader(run_dir: str) -> TraceReader:
    """Open a run dir or exit with a recovery-naming error (principle 8)."""
    try:
        return TraceReader(Path(run_dir))
    except FileNotFoundError as e:
        print(
            f"error: {e} — pass a run directory containing events.jsonl "
            f"(check with 'ebrowse-eval validate {run_dir}')",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _emit_json(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


def _rec_json(r: Record) -> dict[str, Any]:
    return r.to_dict()


# -- overview ---------------------------------------------------------------


def cmd_overview(run_dir: str, as_json: bool = False) -> int:
    reader = open_reader(run_dir)
    records = list(reader.records())
    meta = next((r for r in records if isinstance(r, RunMeta)), None)
    end = next((r for r in records if isinstance(r, RunEnd)), None)
    steps = [r for r in records if isinstance(r, Step)]
    anomalies = [r for r in records if isinstance(r, Anomaly)]
    summaries = [r for r in records if isinstance(r, Summary)]
    by_step_anom = {a.step for a in anomalies}
    by_step_err = {s.step for s in steps if s.error or (s.exit_code not in (0, None))}

    if as_json:
        return _emit_json(
            {
                "meta": _rec_json(meta) if meta else None,
                "end": _rec_json(end) if end else None,
                "steps": [
                    {
                        "step": s.step,
                        "command": s.command,
                        "exit_code": s.exit_code,
                        "url": s.browser.get("url"),
                        "latency_s": s.latency_s,
                        "anomaly": s.step in by_step_anom,
                        "error": s.step in by_step_err,
                    }
                    for s in steps
                ],
                "summaries": [_rec_json(s) for s in summaries],
            }
        )

    if meta:
        agent = meta.agent.get("harness", "?") + "/" + meta.agent.get("model", "?")
        print(
            f"run {meta.run_id or '?'} task={meta.task_id or '?'} agent={agent} "
            f"prompt={_truncate(meta.prompt, 60)!r}"
        )
    else:
        print("run ? (no run_meta — trace may be torn; run 'ebrowse-eval validate')")
    if end:
        totals = " ".join(f"{k}={v}" for k, v in sorted(end.totals.items()))
        ev = ""
        if end.eval is not None:
            ev = f" eval_success={end.eval.get('success')}"
        print(f"outcome={end.outcome} steps={end.steps} {totals}{ev}".rstrip())
    else:
        print("outcome=? (no run_end — run crashed or still in progress)")
    print("step  exit  latency  badge  command / url")
    for s in steps:
        badge = ("A" if s.step in by_step_anom else "") + ("E" if s.step in by_step_err else "")
        lat = f"{s.latency_s:.1f}s" if s.latency_s is not None else "?"
        exit_s = "?" if s.exit_code is None else str(s.exit_code)
        print(
            f"{s.step:<5} {exit_s:<5} {lat:<8} {badge or '-':<6} "
            f"{_truncate(s.command, 60)}  |  {s.browser.get('url', '?')}"
        )
    for sm in summaries:
        print(f"summary {sm.step_start}-{sm.step_end}: {_truncate(sm.text, 120)}")
    return 0


# -- anomalies --------------------------------------------------------------


def cmd_anomalies(run_dir: str, as_json: bool = False) -> int:
    reader = open_reader(run_dir)
    anomalies = reader.anomalies()
    if as_json:
        return _emit_json([_rec_json(a) for a in anomalies])
    if not anomalies:
        print("no anomalies")
        return 0
    for a in anomalies:
        print(f"step {a.step}  {a.kind}: {a.message}")
    return 0


# -- errors -----------------------------------------------------------------


def cmd_errors(run_dir: str, as_json: bool = False) -> int:
    reader = open_reader(run_dir)
    steps = reader.steps()
    next_cmd = {steps[i].step: steps[i + 1].command for i in range(len(steps) - 1)}
    rows: list[dict[str, Any]] = []
    for s in steps:
        if not s.error and s.exit_code in (0, None):
            continue
        err = s.error or {}
        recovery = err.get("recovery") or err.get("recovery_action")
        followed: bool | None = None
        nxt = next_cmd.get(s.step)
        if recovery and nxt is not None:
            # recovery hints quote a command, e.g. "run 'ebrowse outline'" —
            # a follow is the next command appearing inside the hint text
            followed = nxt.split()[:2] != [] and " ".join(nxt.split()[:2]) in recovery
        rows.append(
            {
                "step": s.step,
                "exit_code": s.exit_code,
                "class": err.get("class"),
                "message": err.get("message"),
                "recovery": recovery,
                "next_command": nxt,
                "recovery_followed": followed,
            }
        )
    if as_json:
        return _emit_json(rows)
    if not rows:
        print("no errors")
        return 0
    for r in rows:
        line = f"step {r['step']}  exit={r['exit_code']}"
        if r["class"]:
            line += f"  {r['class']}"
        if r["message"]:
            line += f": {r['message']}"
        print(line)
        if r["recovery"]:
            state = (
                "followed"
                if r["recovery_followed"]
                else ("ignored" if r["next_command"] is not None else "last step")
            )
            print(
                f"  recovery: {r['recovery']}  [{state}"
                + (f" -> {_truncate(r['next_command'], 50)}]" if r["next_command"] else "]")
            )
    return 0


# -- step -------------------------------------------------------------------


def cmd_step(
    run_dir: str, n: int, full: bool = False, debug: bool = False, as_json: bool = False
) -> int:
    reader = open_reader(run_dir)
    recs = reader.for_step(n)
    step = next((r for r in recs if isinstance(r, Step)), None)
    if step is None:
        seen = sorted(s.step or 0 for s in reader.steps())
        rng = f"{seen[0]}..{seen[-1]}" if seen else "none"
        print(
            f"no step {n}; steps in this run: {rng} "
            f"(list them with 'ebrowse-eval overview {run_dir}')",
            file=sys.stderr,
        )
        return 1
    if as_json:
        return _emit_json([_rec_json(r) for r in recs if debug or not _is_debug_log(r)])
    print(f"step {n}  {step.command}")
    if step.agent_text:
        print(f"agent: {_truncate(step.agent_text, 200)}")
    exit_s = "?" if step.exit_code is None else str(step.exit_code)
    lat = f"{step.latency_s}s" if step.latency_s is not None else "?"
    print(f"exit={exit_s} latency={lat} tokens={json.dumps(step.tokens)}")
    if step.timing:
        print("timing: " + " ".join(f"{k}={v}s" for k, v in step.timing.items()))
    if step.browser:
        print("browser: " + " ".join(f"{k}={v}" for k, v in step.browser.items()))
    if step.error:
        print(f"error: {json.dumps(step.error, ensure_ascii=False)}")
    print(
        "blobs: " + f"screenshot={step.screenshot or '-'} dom_snapshot={step.dom_snapshot or '-'}"
    )
    out = step.output if full else _truncate(step.output, 200)
    print("output:")
    for line in out.splitlines():
        print(f"  {line}")
    if not full and len(step.output.replace("\n", " ")) > 200:
        print("  … (--full for untruncated output)")
    for r in recs:
        if isinstance(r, BrowserEvent):
            print(f"browser_event {r.kind}: {json.dumps(r.data, ensure_ascii=False)}")
    for r in recs:
        if isinstance(r, EbrowseLog):
            if _is_debug_log(r) and not debug:
                continue
            print(
                f"log [{r.level}] {r.module}.{r.event}: {json.dumps(r.fields, ensure_ascii=False)}"
            )
    n_debug = sum(1 for r in recs if _is_debug_log(r))
    if n_debug and not debug:
        print(f"({n_debug} debug log record(s) hidden — pass --debug)")
    for r in recs:
        if isinstance(r, Anomaly):
            print(f"anomaly {r.kind}: {r.message}")
    return 0


def _is_debug_log(r: Record) -> bool:
    return isinstance(r, EbrowseLog) and r.level == "debug"


# -- trace-ref / trace-section ----------------------------------------------


def _entity_history(
    reader: TraceReader, token: str, field_keys: tuple[str, ...]
) -> tuple[list[tuple[Record, str]], set[str]]:
    """(matching records with a one-line why, all tokens seen) for @eN / sN.

    Structured fields first (anomaly/ebrowse_log `fields`), then text mentions
    in step command/output; word-boundary match so s1 never matches s12.
    """
    pat = re.compile(re.escape(token) + r"(?![\w])")
    hits: list[tuple[Record, str]] = []
    seen: set[str] = set()
    scan_re = _REF_RE if token.startswith("@e") else _SID_RE
    for r in reader.records():
        blob = json.dumps(r.to_dict(), ensure_ascii=False)
        seen.update(scan_re.findall(blob))
        if isinstance(r, (EbrowseLog, Anomaly)):
            fields = r.fields
            if any(fields.get(k) == token for k in field_keys) or pat.search(
                json.dumps(fields) + (r.message if isinstance(r, Anomaly) else "")
            ):
                if isinstance(r, Anomaly):
                    hits.append((r, f"anomaly {r.kind}: {r.message}"))
                else:
                    hits.append(
                        (r, f"log [{r.level}] {r.module}.{r.event}: {json.dumps(r.fields)}")
                    )
        elif isinstance(r, Step):
            lines = [ln for ln in ([f"$ {r.command}"] + r.output.splitlines()) if pat.search(ln)]
            for ln in lines:
                hits.append((r, ln))
    return hits, seen


def cmd_trace_entity(run_dir: str, token: str, as_json: bool = False) -> int:
    reader = open_reader(run_dir)
    keys = ("ref",) if token.startswith("@") else ("sid", "section")
    hits, seen = _entity_history(reader, token, keys)
    if as_json:
        return _emit_json([{"step": r.step, "type": r.TYPE, "line": why} for r, why in hits])
    if not hits:
        others = ", ".join(sorted(seen, key=_nat_key)) or "none"
        kind = "ref" if token.startswith("@") else "section"
        print(f"no events for {token}; {kind}s seen in this trace: {others}")
        return 1
    for r, why in hits:
        print(f"step {r.step if r.step is not None else '-'}  {why}")
    return 0


def _nat_key(s: str) -> tuple[str, int]:
    m = re.search(r"(\d+)$", s)
    return (s[: m.start()] if m else s, int(m.group(1)) if m else 0)


# -- timing -----------------------------------------------------------------


def cmd_timing(run_dir: str, as_json: bool = False) -> int:
    reader = open_reader(run_dir)
    steps = reader.steps()
    lats = [s.latency_s for s in steps if s.latency_s is not None]
    median = sorted(lats)[len(lats) // 2] if lats else 0.0
    phase_totals: dict[str, float] = {}
    rows = []
    for s in steps:
        for k, v in s.timing.items():
            phase_totals[k] = round(phase_totals.get(k, 0.0) + v, 3)
        phases = " ".join(f"{k}={v}s" for k, v in s.timing.items())
        accounted = round(sum(s.timing.values()), 3)
        outlier = s.latency_s is not None and len(lats) >= 2 and s.latency_s >= 2 * median
        rows.append(
            {
                "step": s.step,
                "latency_s": s.latency_s,
                "phases": s.timing,
                "accounted_s": accounted,
                "outlier": outlier,
                "_line": phases,
            }
        )
    if as_json:
        for r in rows:
            r.pop("_line")
        return _emit_json(
            {"steps": rows, "phase_totals": phase_totals, "total_latency_s": round(sum(lats), 3)}
        )
    for r in rows:
        lat = f"{r['latency_s']}s" if r["latency_s"] is not None else "?"
        flag = "  <-- outlier (>=2x median)" if r["outlier"] else ""
        print(
            f"step {r['step']}  latency={lat}  ({r['_line']})  accounted={r['accounted_s']}s{flag}"
        )
    totals = " ".join(f"{k}={v}s" for k, v in sorted(phase_totals.items()))
    print(f"totals: latency={round(sum(lats), 3)}s  {totals}".rstrip())
    return 0


# -- grep -------------------------------------------------------------------


def cmd_grep(
    run_dir: str,
    pattern: str,
    types: list[str] | None = None,
    step: int | None = None,
    module: str | None = None,
    level: str | None = None,
    as_json: bool = False,
) -> int:
    reader = open_reader(run_dir)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        print(f"error: bad regex: {e} — quote/escape the pattern", file=sys.stderr)
        return 2
    hits = []
    for d in reader.raw():
        if types and d.get("type") not in types:
            continue
        if step is not None and d.get("step") != step:
            continue
        if module is not None and d.get("module") != module:
            continue
        if level is not None and d.get("level") != level:
            continue
        line = json.dumps(d, ensure_ascii=False)
        if rx.search(line):
            hits.append(d)
    if as_json:
        return _emit_json(hits)
    if not hits:
        print("no matches (loosen filters or check the pattern with --json on a wider query)")
        return 1
    for d in hits:
        print(
            f"step {d.get('step', '-')}  {d.get('type', '?')}  "
            f"{_truncate(json.dumps(d, ensure_ascii=False), 160)}"
        )
    return 0
