"""Replay a step's stored DomSnapshot through pure core code.

Tier-2 detail is not logged (trace-schema rule 4); it is regenerated on demand:
DomSnapshot JSON blob -> ebrowse.core.pipeline.build_page -> rendered outline
(or one section's markdown with --section). No browser, no daemon — the same
pure path the daemon uses, so what you see is what the agent saw.
"""

from __future__ import annotations

import json
import sys

from ebrowse.config import ObserveConfig
from ebrowse.core import render
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import DomSnapshot
from ebrowse_evals.inspect import open_reader


def cmd_replay(run_dir: str, n: int, section: str | None = None) -> int:
    reader = open_reader(run_dir)
    step = next((s for s in reader.steps() if s.step == n), None)
    if step is None:
        seen = sorted(s.step or 0 for s in reader.steps())
        rng = f"{seen[0]}..{seen[-1]}" if seen else "none"
        print(f"no step {n}; steps in this run: {rng}", file=sys.stderr)
        return 1
    if not step.dom_snapshot:
        print(
            f"step {n} has no dom_snapshot blob — inspect what was captured "
            f"with 'ebrowse-eval step {run_dir} {n}'",
            file=sys.stderr,
        )
        return 1
    try:
        data = reader.blobs.get(step.dom_snapshot)
    except FileNotFoundError as e:
        print(
            f"error: {e} — the run dir is incomplete; re-run 'ebrowse-eval validate {run_dir}'",
            file=sys.stderr,
        )
        return 2
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict) or "root" not in payload or "url" not in payload:
        print(
            f"blob {step.dom_snapshot} is not a DomSnapshot payload (stub or "
            f"foreign blob) — replay needs a trace captured by the runner, or "
            f"a snapshot saved via 'python -m ebrowse.dev <url> capture'",
            file=sys.stderr,
        )
        return 2

    snap = DomSnapshot.from_dict(payload)
    # Defaults, not user config: replay must be deterministic and byte-stable
    # regardless of the inspecting machine's ~/.config/ebrowse settings.
    page, raw_by_sid = build_page(snap, RefRegistry(), ObserveConfig(), captured_at=0.0)
    if section is None:
        print(render.render_outline(page))
        return 0
    raw = raw_by_sid.get(section)
    if raw is None:
        print(
            f"no section {section}; sections: {', '.join(raw_by_sid)} "
            f"(run replay without --section for the outline)",
            file=sys.stderr,
        )
        return 1
    sec = next(s for s in page.sections if s.sid == section)
    print(render.render_section_markdown(sec, raw, ObserveConfig()))
    return 0
