"""Inspection CLI: golden-ish exact output over the committed sample trace,
--json validity, graceful misses, and replay through pure core code."""

import json
from pathlib import Path

import pytest

from ebrowse_evals.cli import main
from ebrowse_evals.trace import RunMeta, Step, TraceWriter

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = str(FIXTURES / "sample-trace")
DOMSNAPSHOTS = Path(__file__).parents[2] / "tests" / "fixtures" / "domsnapshots"


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


# -- overview ---------------------------------------------------------------


def test_overview_golden(capsys):
    code, out, _ = run(capsys, "overview", SAMPLE)
    assert code == 0
    assert out == (
        "run sample-001 task=list-count agent=pi/qwen-test "
        "prompt='Open http://127.0.0.1:8196/list.html and count the products.'\n"
        "outcome=success steps=3 anomalies=1 peak_context=1950 tokens_output=100 "
        "eval_success=True\n"
        "step  exit  latency  badge  command / url\n"
        "1     0     1.4s     -      ebrowse open http://127.0.0.1:8196/list.html"
        "  |  http://127.0.0.1:8196/list.html\n"
        "2     0     0.9s     -      ebrowse expand s2  |  http://127.0.0.1:8196/list.html\n"
        "3     0     2.1s     A      ebrowse click @e1  |  http://127.0.0.1:8196/detail.html\n"
        "summary 1-3: Opened the product list, expanded it, clicked into Widget A.\n"
    )


def test_overview_json(capsys):
    code, out, _ = run(capsys, "overview", SAMPLE, "--json")
    assert code == 0
    data = json.loads(out)
    assert data["meta"]["run_id"] == "sample-001"
    assert data["end"]["outcome"] == "success"
    assert [s["step"] for s in data["steps"]] == [1, 2, 3]
    assert data["steps"][2]["anomaly"] is True


# -- anomalies / errors -----------------------------------------------------


def test_anomalies_golden(capsys):
    code, out, _ = run(capsys, "anomalies", SAMPLE)
    assert code == 0
    assert out == (
        "step 3  element_moved: @e1 moved 48px between snapshot and click; "
        "re-scrolled before clicking\n"
    )
    code, out, _ = run(capsys, "anomalies", SAMPLE, "--json")
    assert json.loads(out)[0]["kind"] == "element_moved"


def test_errors_none(capsys):
    code, out, _ = run(capsys, "errors", SAMPLE)
    assert code == 0 and out == "no errors\n"


@pytest.fixture()
def error_trace(tmp_path):
    """Step 1 fails with a recovery hint; step 2 follows it; step 3 fails and
    step 4 ignores the hint."""
    w = TraceWriter(tmp_path / "run")
    w.write(RunMeta(ts=0, mono=0, run_id="err-run"))
    w.write(
        Step(
            step=1,
            ts=1,
            mono=1,
            command="ebrowse click @e7",
            exit_code=1,
            error={
                "class": "stale_ref",
                "message": "@e7 not on page",
                "recovery": "run 'ebrowse outline' to re-observe",
            },
        )
    )
    w.write(Step(step=2, ts=2, mono=2, command="ebrowse outline", exit_code=0))
    w.write(
        Step(
            step=3,
            ts=3,
            mono=3,
            command="ebrowse click @e8",
            exit_code=1,
            error={
                "class": "stale_ref",
                "message": "@e8 not on page",
                "recovery": "run 'ebrowse outline' to re-observe",
            },
        )
    )
    w.write(Step(step=4, ts=4, mono=4, command="ebrowse click @e9", exit_code=0))
    return str(tmp_path / "run")


def test_errors_recovery_join(capsys, error_trace):
    code, out, _ = run(capsys, "errors", error_trace)
    assert code == 0
    assert "step 1  exit=1  stale_ref: @e7 not on page" in out
    assert "[followed -> ebrowse outline]" in out
    assert "[ignored -> ebrowse click @e9]" in out
    _, out, _ = run(capsys, "errors", error_trace, "--json")
    rows = json.loads(out)
    assert [r["recovery_followed"] for r in rows] == [True, False]


# -- step -------------------------------------------------------------------


def test_step_golden(capsys):
    code, out, _ = run(capsys, "step", SAMPLE, "3")
    assert code == 0
    assert out.startswith("step 3  ebrowse click @e1\n")
    assert 'exit=0 latency=2.1s tokens={"input": 1700, "output": 25, "context": 1950}' in out
    assert "timing: locate=0.1s click=0.05s settle=1.2s snapshot=0.3s" in out
    assert "browser: url=http://127.0.0.1:8196/detail.html title=Widget A" in out
    assert "dom_snapshot=sha256:3f91bfb5" in out
    assert 'browser_event navigation: {"from"' in out
    assert 'log [warn] interaction.element_moved: {"ref": "@e1", "dy": 48' in out
    assert "anomaly element_moved: @e1 moved 48px" in out


def test_step_debug_gating(capsys):
    _, out, _ = run(capsys, "step", SAMPLE, "1")
    assert "page_split" not in out
    assert "(1 debug log record(s) hidden — pass --debug)" in out
    _, out, _ = run(capsys, "step", SAMPLE, "1", "--debug")
    assert 'log [debug] split.page_split: {"sections": 4' in out


def test_step_missing(capsys):
    code, _, err = run(capsys, "step", SAMPLE, "9")
    assert code == 1
    assert "no step 9; steps in this run: 1..3" in err


def test_step_json(capsys):
    _, out, _ = run(capsys, "step", SAMPLE, "3", "--json")
    recs = json.loads(out)
    assert {r["type"] for r in recs} == {"step", "browser_event", "ebrowse_log", "anomaly"}


# -- trace-ref / trace-section ----------------------------------------------


def test_trace_ref_golden(capsys):
    code, out, _ = run(capsys, "trace-ref", SAMPLE, "@e1")
    assert code == 0
    assert out == (
        "step 2  - Widget A @e1\n"
        "step 3  $ ebrowse click @e1\n"
        "step 3  clicked @e1 -> navigated\n"
        "step 3  log [warn] interaction.element_moved: "
        '{"ref": "@e1", "dy": 48, "rescrolled": true}\n'
        "step 3  anomaly element_moved: @e1 moved 48px between snapshot and click; "
        "re-scrolled before clicking\n"
    )


def test_trace_ref_missing_names_seen_refs(capsys):
    code, out, _ = run(capsys, "trace-ref", SAMPLE, "@e9")
    assert code == 1
    assert out == "no events for @e9; refs seen in this trace: @e1, @e2\n"


def test_trace_section(capsys):
    code, out, _ = run(capsys, "trace-section", SAMPLE, "s2")
    assert code == 0
    assert "step 2  $ ebrowse expand s2" in out
    assert "s12" not in out  # word-boundary: s2 must not be a prefix match
    code, out, _ = run(capsys, "trace-section", SAMPLE, "s9")
    assert code == 1 and "sections seen in this trace: s1, s2" in out


# -- timing -----------------------------------------------------------------


def test_timing_golden(capsys):
    code, out, _ = run(capsys, "timing", SAMPLE)
    assert code == 0
    assert out == (
        "step 1  latency=1.4s  (navigate=0.6s settle=0.3s snapshot=0.2s render=0.05s)"
        "  accounted=1.15s\n"
        "step 2  latency=0.9s  (snapshot=0.2s render=0.1s)  accounted=0.3s\n"
        "step 3  latency=2.1s  (locate=0.1s click=0.05s settle=1.2s snapshot=0.3s)"
        "  accounted=1.65s\n"
        "totals: latency=4.4s  click=0.05s locate=0.1s navigate=0.6s render=0.15s "
        "settle=1.5s snapshot=0.7s\n"
    )
    _, out, _ = run(capsys, "timing", SAMPLE, "--json")
    data = json.loads(out)
    assert data["total_latency_s"] == 4.4
    assert data["phase_totals"]["settle"] == 1.5


def test_timing_outlier_flag(capsys, tmp_path):
    w = TraceWriter(tmp_path / "run")
    w.write(RunMeta(ts=0, mono=0))
    for i, lat in enumerate([1.0, 1.1, 5.0], start=1):
        w.write(Step(step=i, ts=i, mono=i, command="c", latency_s=lat))
    _, out, _ = run(capsys, "timing", str(tmp_path / "run"))
    assert (
        out.count("<-- outlier") == 1
        and "step 3  latency=5.0s" in out.split("outlier")[0] + "outlier"
    )


# -- grep -------------------------------------------------------------------


def test_grep_filters(capsys):
    code, out, _ = run(capsys, "grep", SAMPLE, "element_moved", "--type", "anomaly")
    assert code == 0
    assert out.startswith("step 3  anomaly  ") and out.count("\n") == 1
    code, out, _ = run(capsys, "grep", SAMPLE, "no-such-thing")
    assert code == 1 and out.startswith("no matches")
    _, out, _ = run(capsys, "grep", SAMPLE, "Widget", "--step", "2", "--json")
    assert [d["step"] for d in json.loads(out)] == [2]


def test_grep_bad_regex(capsys):
    code, _, err = run(capsys, "grep", SAMPLE, "(")
    assert code == 2 and "bad regex" in err


# -- replay -----------------------------------------------------------------


def _real_snapshot_trace(tmp_path) -> str:
    """A minimal trace whose dom_snapshot blob is a REAL DomSnapshot capture
    (the committed core fixture tests/fixtures/domsnapshots/list.json)."""
    w = TraceWriter(tmp_path / "run")
    ref = w.put_blob((DOMSNAPSHOTS / "list.json").read_bytes(), ".json")
    w.write(RunMeta(ts=0, mono=0, run_id="replay-run"))
    w.write(Step(step=1, ts=1, mono=1, command="ebrowse open list", dom_snapshot=ref))
    w.write(Step(step=2, ts=2, mono=2, command="ebrowse noop"))
    return str(tmp_path / "run")


def test_replay_real_snapshot(capsys, tmp_path):
    run_dir = _real_snapshot_trace(tmp_path)
    code, out, _ = run(capsys, "replay", run_dir, "--step", "1")
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "PAGE Espresso Gear — Fixture Shop — http://127.0.0.1:8901/list.html"
    assert any(line.startswith("s4 list") and "32 items" in line for line in lines)


def test_replay_section(capsys, tmp_path):
    run_dir = _real_snapshot_trace(tmp_path)
    code, out, _ = run(capsys, "replay", run_dir, "--step", "1", "--section", "s3")
    assert code == 0
    assert out.startswith("## s3 content — Espresso gear (32 results)")
    code, _, err = run(capsys, "replay", run_dir, "--step", "1", "--section", "s99")
    assert code == 1 and "no section s99; sections: s1, s2, s3, s4, s5" in err


def test_replay_stub_blob_errors(capsys):
    code, _, err = run(capsys, "replay", SAMPLE, "--step", "1")
    assert code == 2
    assert "is not a DomSnapshot payload" in err
    assert "python -m ebrowse.dev" in err  # names the recovery action


def test_replay_no_snapshot(capsys, tmp_path):
    run_dir = _real_snapshot_trace(tmp_path)
    code, _, err = run(capsys, "replay", run_dir, "--step", "2")
    assert code == 1 and "step 2 has no dom_snapshot blob" in err
    code, _, err = run(capsys, "replay", run_dir, "--step", "7")
    assert code == 1 and "no step 7; steps in this run: 1..2" in err


# -- misc -------------------------------------------------------------------


def test_missing_run_dir(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["overview", "/nonexistent/run"])
    assert exc.value.code == 2
    assert "ebrowse-eval validate" in capsys.readouterr().err
