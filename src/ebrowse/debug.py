"""Structured debug-event channel (tier 1): per-request JSONL, OFF by default.

Design (see docs/adr/0013-debug-event-channel.md):

- A ``DebugRecorder`` is installed in a contextvar for the duration of one
  daemon request (daemon/server.py). ``emit()`` anywhere in the codebase —
  including pure ``core/`` code — appends a plain event to the recorder's
  in-memory list. No I/O happens at emit time, so core stays pure; the daemon
  flushes the collected events to a JSONL file after the response is built.
- When no recorder is installed (the default: config key ``debug.log`` unset),
  ``emit()`` is a single contextvar read + None check — zero behavior change,
  no file is ever created. Expensive-to-compute fields must be guarded by
  ``enabled()`` at the call site.
- Event shape matches the eval harness's trace schema (`ebrowse_log` records):
  ``{request_id, module, event, level, fields, ts, mono}``. ``level="warn"``
  events with an anomaly event name (ref_rebound, ref_gone, snapshot_truncated,
  element_moved, wait_timeout, section_reshaped) are the anomaly channel —
  they should be rare and surprising by design.

This module is additive-only: no frozen format or model.py type is touched.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Anomaly event names (level=warn). The harness maps these to Anomaly records.
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


@dataclass(slots=True)
class DebugEvent:
    request_id: str
    module: str
    event: str
    level: str
    fields: dict[str, Any]
    ts: float
    mono: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "module": self.module,
            "event": self.event,
            "level": self.level,
            "fields": self.fields,
            "ts": self.ts,
            "mono": self.mono,
        }


@dataclass(slots=True)
class DebugRecorder:
    """Collects events for ONE request. Pure in-memory; the owner flushes."""

    request_id: str
    events: list[DebugEvent] = field(default_factory=list)

    def emit(self, module: str, event: str, level: str = "info", **fields: Any) -> None:
        self.events.append(
            DebugEvent(
                request_id=self.request_id,
                module=module,
                event=event,
                level=level,
                fields=fields,
                ts=time.time(),
                mono=time.monotonic(),
            )
        )


_recorder: ContextVar[DebugRecorder | None] = ContextVar("ebrowse_debug_recorder", default=None)


def enabled() -> bool:
    """True when a recorder is active. Guard expensive field computation."""
    return _recorder.get() is not None


def emit(module: str, event: str, level: str = "info", **fields: Any) -> None:
    """Record one event on the active recorder; no-op (one contextvar read)
    when instrumentation is off. Never raises."""
    rec = _recorder.get()
    if rec is not None:
        rec.emit(module, event, level=level, **fields)


@contextlib.contextmanager
def timed(module: str, phase: str, **fields: Any) -> Iterator[None]:
    """Emit a per-phase timing event ``{event: "phase", phase, dur_ms}`` on
    exit. When off, cost is one monotonic() call and a no-op emit."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        emit(
            module, "phase", phase=phase, dur_ms=round((time.monotonic() - t0) * 1000, 1), **fields
        )


@contextlib.contextmanager
def recording(request_id: str) -> Iterator[DebugRecorder]:
    """Install a recorder for the current async task context; restore on exit."""
    rec = DebugRecorder(request_id=request_id)
    token = _recorder.set(rec)
    try:
        yield rec
    finally:
        _recorder.reset(token)


def resolve_log_path(configured: str, session: str) -> Path:
    """Per-session log file: a literal ``{session}`` in the configured path is
    substituted with the session name; otherwise all sessions share the file
    (events still join on request_id via the daemon's request events)."""
    return Path(configured.replace("{session}", session)).expanduser()


def write_jsonl(path: Path, events: list[DebugEvent]) -> None:
    """Append events as JSONL. Best-effort: a failing debug sink must never
    fail the request (instrumentation is observability, not behavior)."""
    if not events:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev.to_dict(), default=str) + "\n")
    except OSError:
        pass
