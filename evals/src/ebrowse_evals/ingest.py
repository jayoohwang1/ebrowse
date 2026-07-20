"""Post-run join: browser-tool capture spool + daemon debug log -> trace records.

The trusted policy launcher numbers every executed ebrowse invocation: call n spools its
debug-capture payload to capture/<n>.json and stamps EBROWSE_REQUEST_ID=call-<n>
on the daemon request. The join back to trace steps is therefore *ordinal and
deterministic*: the n-th executed ebrowse step in the parsed session is call n.
No timestamp heuristics — a count mismatch is reported as a ``join_mismatch``
anomaly rather than silently mis-attributed.

Policy-blocked custom-tool calls and non-ebrowse steps get no capture fields:
the browser state cannot have changed, and the viewer carries the previous
step's screenshot forward visually.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ebrowse_evals.capture import ingest_payload, write_capture_anomaly
from ebrowse_evals.trace.records import Anomaly, EbrowseLog, Step
from ebrowse_evals.trace.store import TraceWriter

# Matches an `ebrowse` invocation at a command position (start, or after a
# shell operator) — not the word appearing inside paths or arguments.
_EBROWSE_CMD = re.compile(r"(?:^|[;&|(]|\$\(|`)\s*(?:[\w./\-]*/)?ebrowse\b")

# Anomaly event names from ebrowse's debug channel (kept in sync with
# ebrowse.debug.ANOMALY_EVENTS; a superset here is harmless).
ANOMALY_EVENTS = frozenset(
    {
        "ref_rebound",
        "ref_gone",
        "snapshot_truncated",
        "element_moved",
        "wait_timeout",
        "section_reshaped",
    }
)

_CALL_ID = re.compile(r"^call-(\d+)$")


def ebrowse_call_steps(steps: list[Step]) -> dict[int, Step]:
    """Map shim call number (1-based) -> the Step that made that call."""
    out: dict[int, Step] = {}
    n = 0
    for step in steps:
        custom_call = step.tool_name == "ebrowse" and (
            not step.error or step.error.get("class") != "policy_block"
        )
        # Legacy traces used bash. JSON arguments to edit/write tools can contain
        # quoted `ebrowse ...` examples and must not consume a capture slot.
        legacy_call = step.tool_name in (None, "bash") and _EBROWSE_CMD.search(step.command)
        if custom_call or legacy_call:
            n += 1
            out[n] = step
    return out


def attach_spool(writer: TraceWriter, steps: list[Step], spool_dir: Path) -> None:
    """Fill capture fields on each ebrowse step from its spool payload."""
    calls = ebrowse_call_steps(steps)
    spooled: dict[int, Path] = {}
    for p in spool_dir.glob("*.json"):
        try:
            spooled[int(p.stem)] = p
        except ValueError:
            continue
    if spooled and set(spooled) != set(calls):
        write_capture_anomaly(
            writer,
            None,
            "join_mismatch",
            f"{len(spooled)} spooled capture(s) vs {len(calls)} ebrowse step(s) — "
            "capture fields attached only where call numbers line up",
            fields={"spooled": sorted(spooled), "ebrowse_steps": sorted(calls)},
        )
    for n, step in calls.items():
        path = spooled.get(n)
        if path is None:
            continue
        step_id = step.step or 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            write_capture_anomaly(writer, step_id, "capture_failed", f"bad spool {path.name}: {e}")
            continue
        if "hook_error" in payload:
            write_capture_anomaly(writer, step_id, "capture_failed", str(payload["hook_error"]))
            continue
        fields = ingest_payload(writer, step_id, payload)
        step.browser = fields["browser"]
        step.screenshot = fields["screenshot"]
        step.dom_snapshot = fields["dom_snapshot"]


def attach_debug_log(writer: TraceWriter, steps: list[Step], log_path: Path) -> None:
    """Re-emit the daemon's tier-1 events as ebrowse_log trace records, joined
    to steps via the shim's call-<n> request ids; promote anomaly events."""
    calls = ebrowse_call_steps(steps)
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        request_id = str(ev.get("request_id", ""))
        m = _CALL_ID.match(request_id)
        step = calls.get(int(m.group(1))) if m else None
        step_id = step.step if step else None
        fields = ev.get("fields") or {}
        writer.write(
            EbrowseLog(
                step=step_id,
                ts=ev.get("ts"),
                mono=ev.get("mono"),
                request_id=request_id or None,
                module=str(ev.get("module", "")),
                event=str(ev.get("event", "")),
                level=str(ev.get("level", "info")),
                fields=fields,
            )
        )
        if ev.get("level") == "warn" and ev.get("event") in ANOMALY_EVENTS:
            detail = ", ".join(f"{k}={v}" for k, v in fields.items())
            writer.write(
                Anomaly(
                    step=step_id,
                    ts=ev.get("ts"),
                    mono=ev.get("mono"),
                    kind=str(ev["event"]),
                    message=f"{ev.get('module', '?')}: {ev['event']}"
                    + (f" ({detail})" if detail else ""),
                    fields={**fields, "request_id": request_id},
                )
            )
        # phase timings roll up onto the step for `ebrowse-eval timing`
        if step is not None and ev.get("event") == "phase" and "dur_ms" in fields:
            phase = str(fields.get("phase", ev.get("module", "?")))
            step.timing[phase] = step.timing.get(phase, 0.0) + float(fields["dur_ms"]) / 1000.0
