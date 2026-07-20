"""Post-hoc LLM trace annotation: cheap local model -> `summary` records.

Two passes over a finished run (principle 1: labels/captions only, never
load-bearing — a trace is fully usable without annotations):

1. Text pass: the full trajectory (task + every step's agent text, command,
   output) in one prompt -> a one-line VERDICT, per-incident ISSUE lines with
   contiguous step spans, and STUCK_SPANS where the agent flailed.
2. Vision pass: for each stuck span / high-severity issue span (capped), the
   step screenshot + the outline text the agent actually saw -> anything
   visible and goal-relevant that the text view missed or mislabeled.

Every annotation cites step spans so `ebrowse-eval issues` can print
executable drill-downs (`step N`, blob refs). Prompt-design notes: thinking
is disabled (2x faster, answers otherwise land in reasoning_content), one
incident = one contiguous span, severity is defined mechanically.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ebrowse_evals.trace.records import Step, Summary
from ebrowse_evals.trace.store import EVENTS_FILE, TraceReader, TraceWriter

DEFAULT_API_BASE = "http://localhost:5001/v1"

TEXT_SYSTEM = """You are auditing the trajectory of a web-browsing agent that uses the `ebrowse` CLI \
(pages render as compact section outlines; the agent expands sections s1..sN and interacts with \
element refs like @e123).

Produce exactly this structure:

VERDICT: one sentence — the task, what the agent did overall, and how the run ended.

ISSUES:
Each incident on its own line. One incident = ONE contiguous step span where one specific thing \
went wrong; never merge similar-looking problems from different parts of the run into one line. \
Format:
steps <start>-<end> | <category> | <severity> | agent tried X, ran into Y, resolved/not resolved via Z
Categories:
- tool_bug: an ebrowse action failed or its output was wrong/misleading for a valid target
- agent_confusion: the agent misread output that was actually correct
- site_behavior: the site itself blocked, redirected, changed, or hid things (overlays, popups)
- inefficiency: repeated or circular actions with no progress
Severity is exactly `high` (cost the agent more than 3 steps, or blocked the task) or `low` \
(recovered quickly). Max 8 issues, most severe first. Cite only step numbers present in the \
trajectory. Do not narrate normal successful steps. If nothing noteworthy happened, write `none`.

STUCK_SPANS: comma-separated step ranges where the agent was flailing (repeating similar actions \
without progress), or `none`. /no_think"""

VISION_SYSTEM = """You audit a text-only web-browsing agent. The agent sees pages ONLY as the text \
outline shown below — it cannot see the screenshot; you can. Compare the screenshot against what \
the agent was shown. Report anything visible in the screenshot that is relevant to the agent's \
goal but missing, unlabeled, or misleading in the agent's text view — especially controls whose \
purpose is only communicated visually (placement, icons, headings). Be specific about which \
on-screen element corresponds to which text token. If the text view is adequate for the goal, \
reply with the single word ADEQUATE. /no_think"""

_ISSUE_RE = re.compile(
    r"steps?\s+(\d+)\s*(?:-|–|to)\s*(\d+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(.+)", re.IGNORECASE
)
_SPAN_RE = re.compile(r"(\d+)\s*(?:-|–|to)\s*(\d+)|\b(\d+)\b")


@dataclass
class Issue:
    step_start: int
    step_end: int
    category: str
    severity: str
    text: str


@dataclass
class Annotation:
    verdict: str
    issues: list[Issue]
    stuck_spans: list[tuple[int, int]]


class Completer(Protocol):
    """complete(system, user_blocks) -> text. user_blocks are OpenAI-style
    content blocks; tests substitute a stub, prod uses LlamaClient."""

    def __call__(self, system: str, user: list[dict[str, Any]]) -> str: ...


class LlamaClient:
    """Minimal OpenAI-compatible chat client (llama-server; no SDK dep).

    Thinking is disabled via chat_template_kwargs AND a /no_think prompt tag —
    with thinking on, this model buries answers in reasoning_content."""

    def __init__(self, api_base: str = DEFAULT_API_BASE, model: str = "", timeout_s: float = 900):
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def __call__(self, system: str, user: list[dict[str, Any]]) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 3000,
                "temperature": 0.3,
                "seed": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.api_base}/chat/completions", body, {"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                resp = json.load(r)
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(
                f"annotation model unreachable at {self.api_base} ({e}) — "
                f"start llama-server or pass --api-base"
            ) from None
        msg = resp["choices"][0]["message"]
        # Fallback: some templates still emit into reasoning_content.
        return (msg.get("content") or msg.get("reasoning_content") or "").strip()


# -- trajectory rendering ---------------------------------------------------


def render_trajectory(reader: TraceReader, max_output_chars: int = 4000) -> str:
    parts: list[str] = []
    for s in reader.steps():
        lines = []
        if s.agent_text:
            lines.append(f"[agent] {s.agent_text}")
        lines.append(f"[command] {s.command}")
        out = (s.output or "").strip()
        if len(out) > max_output_chars:
            out = out[:max_output_chars] + f"\n…[truncated, {len(out)} chars total]"
        lines.append(f"[output]\n{out}")
        url = (s.browser or {}).get("url")
        if url:
            lines.append(f"[url after] {url}")
        parts.append(f"=== step {s.step} ===\n" + "\n".join(lines))
    end = reader.end()
    if end is not None:
        parts.append(f"=== run end === outcome={end.outcome} steps={end.steps}")
    return "\n\n".join(parts)


# -- response parsing -------------------------------------------------------


def parse_spans(text: str, max_step: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    if "none" in text.lower() and not re.search(r"\d", text):
        return spans
    for m in _SPAN_RE.finditer(text):
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(3))
        lo, hi = sorted((int(a), int(b)))
        if lo >= 1 and lo <= max_step:  # clamp hallucinated upper bounds, drop fully-invented spans
            spans.append((lo, min(hi, max_step)))
    return spans


def parse_annotation(text: str, max_step: int) -> Annotation:
    verdict = ""
    issues: list[Issue] = []
    stuck: list[tuple[int, int]] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("VERDICT:"):
            verdict = line.split(":", 1)[1].strip()
            section = "verdict"
            continue
        if upper.startswith("ISSUES"):
            section = "issues"
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            line = rest
            if not line:
                continue
        if upper.startswith("STUCK_SPANS"):
            stuck = parse_spans(line.split(":", 1)[1] if ":" in line else "", max_step)
            section = "stuck"
            continue
        m = _ISSUE_RE.search(line)
        if m and section in ("", "issues"):
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            if lo > max_step:  # citation invented from thin air — drop it
                continue
            issues.append(
                Issue(
                    step_start=lo,
                    step_end=min(hi, max_step),
                    category=m.group(3).lower(),
                    severity="high" if m.group(4).lower() == "high" else "low",
                    text=m.group(5).strip(),
                )
            )
        elif section == "verdict" and not verdict:
            verdict = line
    return Annotation(verdict=verdict, issues=issues, stuck_spans=stuck)


# -- vision pass ------------------------------------------------------------


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def vision_targets(ann: Annotation, max_targets: int) -> list[tuple[int, int]]:
    """Stuck spans first (that's where belief/reality diverged), then
    high-severity issue spans not already covered."""
    spans = list(ann.stuck_spans)
    spans += [(i.step_start, i.step_end) for i in ann.issues if i.severity == "high"]
    return _merge_spans(spans)[:max_targets]


def _span_screenshot(steps: list[Step], lo: int, hi: int) -> str | None:
    for s in reversed(steps):
        if s.step is not None and lo <= s.step <= hi and s.screenshot:
            return s.screenshot
    return None


def _span_text(steps: list[Step], lo: int, hi: int, limit: int = 6, chars: int = 2500) -> str:
    picked = [s for s in steps if s.step is not None and lo <= s.step <= hi][-limit:]
    return "\n\n".join(
        f"=== step {s.step} ===\n[command] {s.command}\n[output]\n{(s.output or '')[:chars]}"
        for s in picked
    )


# -- orchestration ----------------------------------------------------------


def annotate_run(
    run_dir: Path,
    complete: Completer,
    model_name: str,
    vision: bool = True,
    max_vision: int = 4,
    log: Callable[[str], None] = lambda _: None,
) -> list[Summary]:
    """Run both passes and append `summary` records to the trace.

    Returns the records written. Refuses nothing: idempotency is handled by
    the caller (cli strips prior summaries with --force)."""
    reader = TraceReader(run_dir)
    meta = reader.meta()
    steps = reader.steps()
    if not steps:
        raise ValueError(f"no step records in {run_dir} — nothing to annotate")
    max_step = max(s.step or 0 for s in steps)
    task = meta.prompt if meta else ""

    traj = render_trajectory(reader)
    log(f"text pass: {len(traj)} chars, {len(steps)} steps")
    raw = complete(
        TEXT_SYSTEM,
        [{"type": "text", "text": f"TASK GIVEN TO THE AGENT:\n{task}\n\nTRAJECTORY:\n{traj}"}],
    )
    ann = parse_annotation(raw, max_step)
    if not ann.verdict:
        raise ValueError(f"annotation response had no VERDICT line — raw response:\n{raw[:500]}")

    records: list[Summary] = [
        Summary(step_start=1, step_end=max_step, text=ann.verdict, model=model_name, kind="verdict")
    ]
    records += [
        Summary(
            step_start=i.step_start,
            step_end=i.step_end,
            text=i.text,
            model=model_name,
            kind="issue",
            category=i.category,
            severity=i.severity,
        )
        for i in ann.issues
    ]
    records += [
        Summary(
            step_start=lo,
            step_end=hi,
            model=model_name,
            text="agent repeated similar actions without progress",
            kind="stuck_span",
        )
        for lo, hi in ann.stuck_spans
    ]

    if vision:
        for lo, hi in vision_targets(ann, max_vision):
            ref = _span_screenshot(steps, lo, hi)
            if ref is None:
                log(f"vision {lo}-{hi}: no screenshot, skipped")
                continue
            try:
                png = reader.blobs.get(ref)
            except FileNotFoundError:
                log(f"vision {lo}-{hi}: blob {ref} missing, skipped")
                continue
            b64 = base64.b64encode(png).decode()
            log(f"vision pass: steps {lo}-{hi} ({ref[:19]}…)")
            out = complete(
                VISION_SYSTEM,
                [
                    {
                        "type": "text",
                        "text": f"AGENT'S GOAL: {task}\n\n"
                        f"WHAT THE AGENT SAW (steps {lo}-{hi}):\n{_span_text(steps, lo, hi)}",
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": "SCREENSHOT of the page at this point is attached."},
                ],
            )
            if out.strip().upper().startswith("ADEQUATE"):
                continue
            records.append(
                Summary(
                    step_start=lo,
                    step_end=hi,
                    text=out.strip(),
                    model=model_name,
                    kind="vision",
                    screenshot=ref,
                )
            )

    writer = TraceWriter(run_dir)
    for rec in records:
        writer.write(rec)
    return records


def strip_summaries(run_dir: Path) -> int:
    """Remove prior summary records so a re-annotate replaces, not duplicates.
    The one sanctioned rewrite of events.jsonl — annotations are post-hoc
    labels, not ground truth."""
    events = run_dir / EVENTS_FILE
    lines = events.read_text(encoding="utf-8").splitlines(keepends=True)
    kept, dropped = [], 0
    for line in lines:
        try:
            if json.loads(line).get("type") == "summary":
                dropped += 1
                continue
        except json.JSONDecodeError:
            pass
        kept.append(line)
    if dropped:
        tmp = events.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(kept), encoding="utf-8")
        tmp.rename(events)
    return dropped
