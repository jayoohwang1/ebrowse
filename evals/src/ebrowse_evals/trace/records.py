"""Typed trace records.

Every record serializes to a flat JSON object with a "type" discriminator.
Dataclasses carry the *known* fields; anything unknown on read is preserved in
``extra`` and round-trips, so old readers survive new writers and vice versa.

Conventions (binding for all writers):
- ``step``: monotonically increasing agent tool-call index (1-based); records
  not tied to a step (run_meta, run_end, run-scoped summaries) leave it None.
- ``ts`` is wall-clock unix time; ``mono`` is a monotonic clock for durations.
- Large payloads are blob refs ("sha256:<hex>"), never inlined.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Record:
    """Base envelope. ``extra`` holds unknown keys from newer writers."""

    TYPE: ClassVar[str] = ""

    step: int | None = None
    ts: float | None = None
    mono: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra")
        d.update(self.extra)
        d = {k: v for k, v in d.items() if v is not None}
        d["type"] = self.TYPE
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Record:
        known = {f.name for f in fields(cls)} - {"extra"}
        kwargs = {k: v for k, v in d.items() if k in known}
        extra = {k: v for k, v in d.items() if k not in known and k != "type"}
        return cls(**kwargs, extra=extra)


@dataclass(slots=True)
class RunMeta(Record):
    """First record of every trace. Fully resolved config makes runs
    reproducible and comparable long after the flags are forgotten."""

    TYPE: ClassVar[str] = "run_meta"

    schema_version: int = SCHEMA_VERSION
    run_id: str = ""
    task_id: str = ""
    prompt: str = ""
    benchmark: str | None = None
    config: dict[str, Any] = field(default_factory=dict)  # resolved run config
    agent: dict[str, Any] = field(default_factory=dict)  # harness/model/provider
    git_sha: str | None = None
    git_dirty: bool | None = None
    ebrowse_version: str | None = None
    ebrowse_mode: str | None = None  # "worktree" | "installed"


@dataclass(slots=True)
class RunEnd(Record):
    """Last record: terminal outcome + totals for run-comparison tables."""

    TYPE: ClassVar[str] = "run_end"

    outcome: str = "unknown"  # "success" | "failure" | "error" | "timeout" | "unknown"
    steps: int = 0
    totals: dict[str, Any] = field(default_factory=dict)  # tokens, errors-by-class, cost
    eval: dict[str, Any] | None = None  # EvalResult dict, when an evaluator ran


@dataclass(slots=True)
class Step(Record):
    """One agent tool-call: what the agent did/saw + post-action browser state.

    ``browser`` state and the screenshot/dom_snapshot blobs are captured
    unconditionally each step, regardless of what the agent asked for."""

    TYPE: ClassVar[str] = "step"

    command: str = ""  # exact command line invoked
    output: str = ""  # tool output verbatim, as the agent saw it
    exit_code: int | None = None
    agent_text: str | None = None  # assistant text/reasoning for the turn
    tokens: dict[str, Any] = field(default_factory=dict)  # input/output/reasoning/context
    latency_s: float | None = None
    timing: dict[str, float] = field(default_factory=dict)  # phase -> seconds
    browser: dict[str, Any] = field(default_factory=dict)  # url/title/tabs/viewport/scroll
    screenshot: str | None = None  # blob ref
    dom_snapshot: str | None = None  # blob ref
    request_id: str | None = None  # joins ebrowse_log records to this step
    error: dict[str, Any] | None = None  # class/message/recovery action


@dataclass(slots=True)
class BrowserEvent(Record):
    """Page-originated event since the previous step (unrecoverable if not
    captured live): console output, failed requests, dialogs, navigations."""

    TYPE: ClassVar[str] = "browser_event"

    kind: str = ""  # "console" | "network_failure" | "dialog" | "navigation"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EbrowseLog(Record):
    """Structured tier-1 internal event from ebrowse (ref lifecycle, diff
    verdicts, action execution, waits). Structured fields, not prose -- the
    inspection tools filter on event/module/fields."""

    TYPE: ClassVar[str] = "ebrowse_log"

    request_id: str | None = None
    module: str = ""  # e.g. "split", "fingerprint", "locate", "interaction"
    event: str = ""  # e.g. "ref_rebound", "wait_timeout", "section_reshaped"
    level: str = "info"  # "debug" | "info" | "warn"
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Anomaly(Record):
    """The pipeline flagging its own surprises -- the triage layer. A run's
    anomaly list should fit on one screen."""

    TYPE: ClassVar[str] = "anomaly"

    kind: str = ""  # e.g. "ref_rebound", "snapshot_truncated", "element_moved"
    message: str = ""
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Summary(Record):
    """LLM-generated summary of a step range, appended post-hoc."""

    TYPE: ClassVar[str] = "summary"

    step_start: int = 0
    step_end: int = 0
    text: str = ""
    model: str | None = None


RECORD_TYPES: dict[str, type[Record]] = {
    cls.TYPE: cls for cls in (RunMeta, RunEnd, Step, BrowserEvent, EbrowseLog, Anomaly, Summary)
}


def record_from_dict(d: dict[str, Any]) -> Record | None:
    """Parse one events.jsonl object. Unknown types return None -- readers
    must skip them, not fail (forward compatibility)."""
    cls = RECORD_TYPES.get(d.get("type", ""))
    return cls.from_dict(d) if cls else None
