"""Task/benchmark loading, selection, and eval hooks."""

from pathlib import Path

import pytest

from ebrowse_evals.tasks import load_benchmark, load_task
from ebrowse_evals.trace.store import TraceReader

FIXTURES = Path(__file__).parent / "fixtures"
BENCH = FIXTURES / "benchmark"


def test_load_benchmark():
    bench = load_benchmark(BENCH)
    assert bench.name == "fixtures"
    assert bench.config["fixture_server"] == "127.0.0.1:8196"
    assert [t.id for t in bench.tasks] == ["custom-eval", "list-count"]


def test_selection():
    bench = load_benchmark(BENCH)
    assert [t.id for t in bench.select(patterns=["list-*"])] == ["list-count"]
    assert [t.id for t in bench.select(tags=["process"])] == ["custom-eval"]
    assert bench.select(tags=["fixture", "read-only"]) == bench.tasks
    assert bench.select(patterns=["nope-*"]) == []


def test_default_eval_expected_contains():
    task = load_task(BENCH / "list-count")
    assert task.load_evaluator() is None
    assert task.default_eval("There are 24 products.").success is True
    assert task.default_eval("There are 23 products.").success is False


def test_default_eval_without_expected_is_unjudged():
    task = load_task(BENCH / "custom-eval")
    assert task.default_eval("done").success is None


def test_custom_evaluator_scores_process():
    task = load_task(BENCH / "custom-eval")
    evaluate = task.load_evaluator()
    assert evaluate is not None
    result = evaluate(TraceReader(FIXTURES / "sample-trace"))
    assert result.success is True  # sample trace reaches detail.html
    assert result.details == {"steps": 3, "errors": 0}


def test_missing_prompt_rejected(tmp_path):
    (tmp_path / "task.toml").write_text("[task]\nurl = 'http://x'\n")
    with pytest.raises(ValueError, match="prompt"):
        load_task(tmp_path)
