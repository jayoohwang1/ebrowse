"""Annotation pipeline: response parsing, record writing, idempotent replace,
and the `issues` lens — all against a stub completer (no network, no LLM)."""

import json
import shutil
from pathlib import Path

from ebrowse_evals.annotate import (
    annotate_run,
    parse_annotation,
    parse_spans,
    strip_summaries,
    vision_targets,
)
from ebrowse_evals.cli import main
from ebrowse_evals.trace.store import TraceReader

FIXTURES = Path(__file__).parent / "fixtures"

RESPONSE = """VERDICT: The agent counted the products on the list page and answered 3.

ISSUES:
steps 2-3 | tool_bug | high | agent tried to click @e1, ebrowse mislocated it, resolved via refresh
steps 1-1 | inefficiency | low | agent re-opened the same page twice
steps 90-99 | tool_bug | high | hallucinated citation beyond the last step

STUCK_SPANS: 2-3
"""


def _copy_sample(tmp_path: Path) -> Path:
    dst = tmp_path / "sample-trace"
    shutil.copytree(FIXTURES / "sample-trace", dst)
    return dst


def _stub(system: str, user: list) -> str:
    if "screenshot" in system.lower():
        return "The outline omits the visible 'Add to cart' button next to Widget A."
    return RESPONSE


# -- parsing ----------------------------------------------------------------


def test_parse_annotation_clamps_and_orders():
    ann = parse_annotation(RESPONSE, max_step=3)
    assert ann.verdict.startswith("The agent counted")
    # the fully-invented steps 90-99 citation is dropped, not clamped into range
    assert [(i.step_start, i.step_end, i.category, i.severity) for i in ann.issues] == [
        (2, 3, "tool_bug", "high"),
        (1, 1, "inefficiency", "low"),
    ]
    assert ann.stuck_spans == [(2, 3)]


def test_parse_spans():
    assert parse_spans("none", 10) == []
    assert parse_spans("2-5, 8", 10) == [(2, 5), (8, 8)]
    assert parse_spans("7-99", 10) == [(7, 10)]  # upper bound clamped
    assert parse_spans("50-60", 10) == []  # fully out of range


def test_vision_targets_merge_and_cap():
    ann = parse_annotation(RESPONSE, max_step=3)
    # stuck span (2,3) and the high issue (2,3) merge into one target;
    # the low-severity issue is not a vision trigger
    assert vision_targets(ann, max_targets=4) == [(2, 3)]
    ann.stuck_spans.append((1, 1))
    assert vision_targets(ann, max_targets=1) == [(1, 3)]  # adjacent spans merge


# -- annotate_run -----------------------------------------------------------


def test_annotate_run_writes_summary_records(tmp_path):
    run_dir = _copy_sample(tmp_path)
    recs = annotate_run(run_dir, _stub, "stub-model")
    kinds = [r.kind for r in recs]
    assert kinds.count("verdict") == 1
    assert kinds.count("issue") == 2
    assert kinds.count("stuck_span") == 1
    # re-read through the store: records round-trip as typed summaries
    reader = TraceReader(run_dir)
    from ebrowse_evals.trace.records import Summary

    stored = [r for r in reader.records() if isinstance(r, Summary) and r.kind]
    assert len(stored) == len(recs)
    issue = next(r for r in stored if r.kind == "issue")
    assert issue.category == "tool_bug" and issue.severity == "high"
    assert issue.model == "stub-model"


def test_annotate_run_vision_uses_span_screenshot(tmp_path):
    run_dir = _copy_sample(tmp_path)
    recs = annotate_run(run_dir, _stub, "stub-model")
    vision = [r for r in recs if r.kind == "vision"]
    assert vision, "stuck span with a screenshot should produce a vision record"
    assert vision[0].screenshot and vision[0].screenshot.startswith("sha256:")
    assert "Add to cart" in vision[0].text


def test_annotate_run_vision_adequate_writes_nothing(tmp_path):
    run_dir = _copy_sample(tmp_path)

    def stub(system, user):
        return "ADEQUATE" if "screenshot" in system.lower() else RESPONSE

    recs = annotate_run(run_dir, stub, "stub-model")
    assert not [r for r in recs if r.kind == "vision"]


def test_strip_summaries_replaces(tmp_path):
    run_dir = _copy_sample(tmp_path)
    before = sum(1 for line in (run_dir / "events.jsonl").open())
    annotate_run(run_dir, _stub, "stub-model")
    dropped = strip_summaries(run_dir)
    # drops the new annotations AND the fixture's own plain summary record
    after = sum(1 for line in (run_dir / "events.jsonl").open())
    assert dropped >= 4
    assert after == before - 1  # fixture summary gone, everything else intact
    assert TraceReader(run_dir).meta() is not None


# -- windowing --------------------------------------------------------------


def test_plan_windows_fill_and_overlap():
    from ebrowse_evals.annotate import plan_windows

    blocks = [(i, "x" * 100) for i in range(1, 11)]  # 10 steps, 100 chars each
    assert plan_windows(blocks, budget_chars=2000) == [(0, 9)]  # fits in one
    wins = plan_windows(blocks, budget_chars=400, overlap=2)
    assert wins[0] == (0, 3)
    for (alo, ahi), (blo, _) in zip(wins, wins[1:], strict=False):
        assert blo == ahi - 1  # 2-step overlap
        assert blo > alo  # always advances
    assert wins[-1][1] == 9  # covers the tail
    # a single oversized block still gets a window of its own
    assert plan_windows([(1, "y" * 999)], budget_chars=10) == [(0, 0)]
    assert plan_windows([], budget_chars=100) == []


def test_windowed_text_pass_merges(tmp_path):
    run_dir = _copy_sample(tmp_path)
    calls = []

    def stub(system, user):
        calls.append(system)
        if "consolidating" in system:  # merge call
            return (
                "VERDICT: merged view of the whole run.\n\nISSUES:\n"
                "steps 1-3 | inefficiency | low | merged incident\n\nSTUCK_SPANS: none"
            )
        if "screenshot" in system.lower():
            return "ADEQUATE"
        return (
            "VERDICT: window view.\n\nISSUES:\n"
            "steps 2-3 | tool_bug | low | window incident\n\nSTUCK_SPANS: none"
        )

    # tiny budget forces windowing on the 3-step sample trace
    recs = annotate_run(run_dir, stub, "stub-model", context_tokens=8_100)
    windows = [s for s in calls if "auditing" in s]
    merges = [s for s in calls if "consolidating" in s]
    assert len(windows) >= 2 and len(merges) == 1
    verdict = next(r for r in recs if r.kind == "verdict")
    assert verdict.text == "merged view of the whole run."


def test_windowed_mechanical_fallback(tmp_path):
    run_dir = _copy_sample(tmp_path)

    def stub(system, user):
        if "consolidating" in system:
            return "garbage with no structure"
        if "screenshot" in system.lower():
            return "ADEQUATE"
        return (
            "VERDICT: window view.\n\nISSUES:\n"
            "steps 2-3 | tool_bug | high | window incident\n\nSTUCK_SPANS: 2-3"
        )

    recs = annotate_run(run_dir, stub, "stub-model", context_tokens=8_100)
    verdict = next(r for r in recs if r.kind == "verdict")
    assert "window view" in verdict.text
    issues = [r for r in recs if r.kind == "issue"]
    # identical issues from overlapping windows dedupe to one
    assert len(issues) == 1 and issues[0].severity == "high"
    assert [r for r in recs if r.kind == "stuck_span"]


# -- issues lens ------------------------------------------------------------


def run_cli(capsys, *argv):
    code = main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_issues_lens_unannotated(capsys, tmp_path):
    run_dir = _copy_sample(tmp_path)
    code, out, _ = run_cli(capsys, "issues", str(run_dir))
    assert code == 0
    assert "no annotations" in out and "annotate" in out


def test_issues_lens_golden(capsys, tmp_path):
    run_dir = _copy_sample(tmp_path)
    annotate_run(run_dir, _stub, "stub-model")
    code, out, _ = run_cli(capsys, "issues", str(run_dir))
    assert code == 0
    assert "verdict: The agent counted" in out
    assert "steps 2-3" in out and "tool_bug" in out and "high" in out
    assert f"> ebrowse-eval step {run_dir} 2" in out
    assert "stuck spans: 2-3" in out
    assert "vision steps" in out and "Add to cart" in out


def test_issues_lens_json(capsys, tmp_path):
    run_dir = _copy_sample(tmp_path)
    annotate_run(run_dir, _stub, "stub-model")
    code, out, _ = run_cli(capsys, "issues", str(run_dir), "--json")
    assert code == 0
    data = json.loads(out)
    assert {d["kind"] for d in data} >= {"verdict", "issue", "stuck_span"}
