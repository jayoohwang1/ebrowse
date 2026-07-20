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


def render_steps(reader: TraceReader, max_output_chars: int = 4000) -> list[tuple[int, str]]:
    """Per-step rendered blocks, keyed by step id (windowing splits on these)."""
    parts: list[tuple[int, str]] = []
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
        parts.append((s.step or 0, f"=== step {s.step} ===\n" + "\n".join(lines)))
    return parts


def _end_block(reader: TraceReader) -> str:
    end = reader.end()
    return f"=== run end === outcome={end.outcome} steps={end.steps}" if end else ""


def render_trajectory(reader: TraceReader, max_output_chars: int = 4000) -> str:
    blocks = [text for _, text in render_steps(reader, max_output_chars)]
    if end := _end_block(reader):
        blocks.append(end)
    return "\n\n".join(blocks)


# -- windowing (trajectories larger than the annotator's context) -----------

# Conservative chars-per-token for Qwen-class tokenizers; the budget also
# reserves room for the system prompt, response, and windowing overhead.
CHARS_PER_TOKEN = 3
DEFAULT_CONTEXT_TOKENS = 110_000
WINDOW_OVERLAP_STEPS = 5


def plan_windows(
    blocks: list[tuple[int, str]], budget_chars: int, overlap: int = WINDOW_OVERLAP_STEPS
) -> list[tuple[int, int]]:
    """Greedy fill: contiguous index ranges [lo, hi] over `blocks`, each within
    budget_chars, consecutive windows overlapping by `overlap` steps so an
    incident on a boundary is fully visible to at least one window. A single
    oversized block still gets its own window (it was already truncated)."""
    if not blocks:
        return []
    windows: list[tuple[int, int]] = []
    i = 0
    while i < len(blocks):
        size = 0
        j = i
        while j < len(blocks) and (size + len(blocks[j][1]) <= budget_chars or j == i):
            size += len(blocks[j][1])
            j += 1
        windows.append((i, j - 1))
        if j >= len(blocks):
            break
        i = max(i + 1, j - overlap)
    return windows


MERGE_SYSTEM = """You are consolidating audit reports from several overlapping windows of one \
web-browsing agent trajectory into a single report. Windows overlap, so the same incident may \
appear twice with slightly different spans — merge duplicates, keep the widest accurate span. \
Produce exactly the structure below (same format as the window reports):

VERDICT: one sentence for the WHOLE run — the task, what the agent did overall, how it ended.

ISSUES:
steps <start>-<end> | <category> | <severity> | one-sentence description
(categories tool_bug|agent_confusion|site_behavior|inefficiency; severity high|low; max 8, most \
severe first; only step numbers that appear in the window reports)

STUCK_SPANS: comma-separated step ranges, or `none`. If the same flailing behavior resumes across \
windows, report it as one span. /no_think"""


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


def _text_call(complete: Completer, task: str, traj: str, header: str = "") -> str:
    body = f"TASK GIVEN TO THE AGENT:\n{task}\n\n{header}TRAJECTORY:\n{traj}"
    return complete(TEXT_SYSTEM, [{"type": "text", "text": body}])


def _mechanical_merge(window_anns: list[Annotation], max_step: int) -> Annotation:
    """Fallback when the merge call produces nothing parseable: concat issue
    lists (severity-major, span order), union stuck spans, join verdicts."""
    issues = [i for a in window_anns for i in a.issues]
    issues.sort(key=lambda i: (i.severity != "high", i.step_start))
    seen: set[tuple[int, int, str]] = set()
    deduped = []
    for i in issues:
        key = (i.step_start, i.step_end, i.category)
        if key not in seen:
            seen.add(key)
            deduped.append(i)
    return Annotation(
        verdict=" / ".join(a.verdict for a in window_anns if a.verdict),
        issues=deduped[:8],
        stuck_spans=_merge_spans([s for a in window_anns for s in a.stuck_spans]),
    )


def run_text_pass(
    reader: TraceReader,
    complete: Completer,
    task: str,
    max_step: int,
    context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    log: Callable[[str], None] = lambda _: None,
) -> Annotation:
    """Single-pass when the trajectory fits the annotator's context, else
    overlapping windows + a merge call (mechanical fallback if that fails)."""
    blocks = render_steps(reader)
    end = _end_block(reader)
    # 8k tokens reserved for system prompt + window header + response
    budget_chars = max(context_tokens - 8_000, 100) * CHARS_PER_TOKEN
    total = sum(len(t) for _, t in blocks) + len(end)
    if total <= budget_chars:
        traj = "\n\n".join([t for _, t in blocks] + ([end] if end else []))
        log(f"text pass: {total} chars, {len(blocks)} steps, single window")
        return parse_annotation(_text_call(complete, task, traj), max_step)

    windows = plan_windows(blocks, budget_chars)
    log(f"text pass: {total} chars > budget {budget_chars}, {len(windows)} windows")
    reports: list[str] = []
    window_anns: list[Annotation] = []
    for n, (lo, hi) in enumerate(windows, 1):
        a_step, b_step = blocks[lo][0], blocks[hi][0]
        parts = [t for _, t in blocks[lo : hi + 1]]
        if hi == len(blocks) - 1 and end:
            parts.append(end)
        header = (
            f"NOTE: you are seeing WINDOW {n}/{len(windows)} of a long run — "
            f"steps {a_step}-{b_step} of {max_step}. Report only what this window shows; "
            f"the reports will be merged afterwards.\n\n"
        )
        raw = _text_call(complete, task, "\n\n".join(parts), header)
        log(f"  window {n}/{len(windows)} (steps {a_step}-{b_step}): {len(raw)} chars back")
        reports.append(f"--- window {n} (steps {a_step}-{b_step}) ---\n{raw}")
        window_anns.append(parse_annotation(raw, max_step))

    merged_raw = complete(
        MERGE_SYSTEM,
        [{"type": "text", "text": f"TASK GIVEN TO THE AGENT:\n{task}\n\n" + "\n\n".join(reports)}],
    )
    merged = parse_annotation(merged_raw, max_step)
    if not merged.verdict and not merged.issues:
        log("merge response unparseable — falling back to mechanical merge")
        return _mechanical_merge(window_anns, max_step)
    return merged


def annotate_run(
    run_dir: Path,
    complete: Completer,
    model_name: str,
    vision: bool = True,
    max_vision: int = 4,
    context_tokens: int = DEFAULT_CONTEXT_TOKENS,
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

    ann = run_text_pass(reader, complete, task, max_step, context_tokens, log)
    if not ann.verdict:
        raise ValueError("annotation text pass produced no VERDICT line")

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
