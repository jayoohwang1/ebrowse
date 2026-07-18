"""Runner: selection, config layering, trace emission, evaluator invocation.

Pure tests — a fake harness stands in for pi; no network, no browser.
"""

import json
from pathlib import Path

from ebrowse_evals.harness import HarnessResult, ParsedStep, parse_pi_session
from ebrowse_evals.runner import (
    HARNESS_DEFAULTS,
    resolve_config,
    run_task,
    run_tasks,
    select_tasks,
)
from ebrowse_evals.tasks import load_benchmark, load_task
from ebrowse_evals.trace.records import Step
from ebrowse_evals.trace.store import TraceReader, TraceWriter

FIXTURES = Path(__file__).parent / "fixtures"
BENCH = FIXTURES / "benchmark"
SAMPLE_SESSION = Path(__file__).parents[2] / "experiments" / "fixtures" / "sample-pi-session.jsonl"


class FakeHarness:
    """Deterministic AgentHarness: canned steps + final answer."""

    def __init__(self, result: HarnessResult | None = None):
        self.result = result or HarnessResult(
            steps=[
                ParsedStep(
                    command="ebrowse open https://example.com",
                    output="PAGE Example Domain",
                    tokens={"input": 100, "output": 20},
                    latency_s=1.0,
                ),
                ParsedStep(command="ebrowse expand s1", output="24 products", is_error=False),
            ],
            final_answer="There are 24 products.",
            totals={"output_tokens": 30, "input_tokens": 250, "peak_context": 280},
        )
        self.calls: list[dict] = []

    def describe(self):
        return {"harness": "fake", "provider": "test", "model": "test-model"}

    def run(self, prompt, workdir, env, timeout_s, run_dir):
        self.calls.append({"prompt": prompt, "workdir": workdir, "timeout_s": timeout_s})
        return self.result


# -- selection ---------------------------------------------------------------


def test_select_tasks_sample_is_seeded():
    bench = load_benchmark(BENCH)
    a = select_tasks(bench, sample=1, seed=7)
    b = select_tasks(bench, sample=1, seed=7)
    assert [t.id for t in a] == [t.id for t in b]
    assert len(a) == 1
    assert select_tasks(bench, sample=10) == bench.tasks  # sample > n is a no-op


def test_select_tasks_patterns_and_tags():
    bench = load_benchmark(BENCH)
    assert [t.id for t in select_tasks(bench, patterns=["list-*"])] == ["list-count"]
    assert [t.id for t in select_tasks(bench, tags=["process"])] == ["custom-eval"]


# -- config layering ---------------------------------------------------------


def test_resolve_config_layering():
    resolved = resolve_config(
        {"fixture_server": "x", "timeout_s": 100},  # benchmark
        {"timeout_s": 50, "tool": "none"},  # task
        {"tool": "ebrowse", "worktree": True, "timeout_s": None},  # CLI (None = unset)
    )
    assert resolved["timeout_s"] == 50  # task beat benchmark; CLI None didn't override
    assert resolved["tool"] == "ebrowse"  # CLI beat task
    assert resolved["worktree"] is True
    assert resolved["fixture_server"] == "x"
    assert resolve_config() == HARNESS_DEFAULTS


# -- trace emission ----------------------------------------------------------


def test_run_task_emits_valid_trace(tmp_path):
    task = load_task(BENCH / "list-count")
    harness = FakeHarness()
    run_dir = tmp_path / "run1"
    result = run_task(
        task, harness, resolve_config({"worktree": True}), run_dir, benchmark="fixtures"
    )
    reader = TraceReader(run_dir)
    assert reader.validate() == []
    meta = reader.meta()
    assert meta is not None
    assert meta.task_id == "list-count"
    assert meta.benchmark == "fixtures"
    assert meta.config["worktree"] is True
    assert meta.ebrowse_mode == "worktree"
    assert meta.agent["harness"] == "fake"
    assert meta.git_sha  # runs inside the repo
    steps = reader.steps()
    assert [s.step for s in steps] == [1, 2]
    assert steps[0].command == "ebrowse open https://example.com"
    assert steps[0].output == "PAGE Example Domain"
    assert steps[0].tokens == {"input": 100, "output": 20}
    assert steps[0].screenshot is None and steps[0].dom_snapshot is None  # capture layer's job
    end = reader.end()
    assert end is not None
    assert end.outcome == "success"  # default_eval: "24" in final answer
    assert end.steps == 2
    assert end.totals["output_tokens"] == 30
    assert end.eval == {"success": True, "score": None, "details": {}}
    assert result.outcome == "success"
    # task-level timeout (120) beat the harness default
    assert harness.calls[0]["timeout_s"] == 120


def test_run_task_outcomes(tmp_path):
    task = load_task(BENCH / "list-count")
    wrong = FakeHarness(HarnessResult(final_answer="no idea"))
    assert run_task(task, wrong, {}, tmp_path / "a").outcome == "failure"
    crashed = FakeHarness(HarnessResult(exit_code=1))
    assert run_task(task, crashed, {}, tmp_path / "b").outcome == "error"
    hung = FakeHarness(HarnessResult(timed_out=True, exit_code=-1))
    assert run_task(task, hung, {}, tmp_path / "c").outcome == "timeout"


def test_run_task_invokes_custom_evaluator_on_trace(tmp_path):
    task = load_task(BENCH / "custom-eval")
    harness = FakeHarness(
        HarnessResult(
            steps=[ParsedStep(command="ebrowse open detail.html", output="ok")],
            final_answer="opened",
        )
    )

    # custom-eval's eval.py checks step.browser urls; enrich via the capture hook
    class Capture:
        def on_step(self, writer: TraceWriter, step: Step) -> None:
            step.browser = {"url": "http://127.0.0.1:8196/detail.html"}

    result = run_task(task, harness, {}, tmp_path / "r", capture=Capture())
    assert result.outcome == "success"
    assert result.eval.details == {"steps": 1, "errors": 0}
    reader = TraceReader(tmp_path / "r")
    assert reader.validate() == []
    assert reader.steps()[0].browser["url"].endswith("detail.html")


def test_run_task_survives_broken_evaluator(tmp_path):
    task = load_task(BENCH / "custom-eval")
    task.path = tmp_path / "task"
    task.path.mkdir()
    (task.path / "eval.py").write_text("def evaluate(trace):\n    raise RuntimeError('boom')\n")
    result = run_task(task, FakeHarness(), {}, tmp_path / "r")
    assert result.outcome == "unknown"
    assert "boom" in result.eval.details["evaluator_error"]
    assert TraceReader(tmp_path / "r").validate() == []


def test_run_tasks_layers_benchmark_and_task_config(tmp_path):
    bench = load_benchmark(BENCH)
    harness = FakeHarness()
    results = run_tasks(bench.tasks, harness, bench, {"tool": "none"}, tmp_path)
    assert len(results) == 2
    for r in results:
        meta = TraceReader(r.run_dir).meta()
        assert meta is not None
        assert meta.config["fixture_server"] == "127.0.0.1:8196"  # from benchmark [config]
        assert meta.config["tool"] == "none"  # CLI layer
        assert meta.benchmark == "fixtures"


# -- pi session parsing ------------------------------------------------------


def test_parse_pi_session_sample_fixture():
    entries = [json.loads(line) for line in SAMPLE_SESSION.read_text().splitlines() if line.strip()]
    result = parse_pi_session(entries)
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.command == "ebrowse open https://example.com"
    assert step.output.startswith("PAGE Example Domain")
    assert step.is_error is False
    assert step.agent_text == "I will open the page."
    assert step.tokens == {"input": 100, "output": 20, "totalTokens": 120}
    assert step.latency_s == 1.0  # 00:00:02 -> 00:00:03
    assert result.final_answer == "The page title is Example Domain."
    assert result.totals == {
        "turns": 2,
        "tool_calls": 1,
        "output_tokens": 30,
        "input_tokens": 250,
        "peak_context": 160,
    }
