"""Task and benchmark model.

A task is a directory:
  task.toml   -- prompt + metadata (see evals/docs/tasks.md)
  eval.py     -- optional: ``evaluate(trace: TraceReader) -> EvalResult``

A benchmark is a directory of task directories, optionally with a
benchmark.toml carrying a name and config defaults. External benchmark
formats (e.g. Online-Mind2Web) are converted into this model by adapters at
load time -- the runner never special-cases a source format.

Evaluators receive the *trace*, not just the final answer, so they can score
process (step count, error recovery, which verbs were used) as well as outcome.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from ebrowse_evals.trace.store import TraceReader

TASK_FILE = "task.toml"
EVAL_FILE = "eval.py"
BENCHMARK_FILE = "benchmark.toml"


@dataclass(slots=True)
class EvalResult:
    success: bool | None = None  # None = evaluator couldn't judge
    score: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"success": self.success, "score": self.score, "details": self.details}


class Evaluator(Protocol):
    def __call__(self, trace: TraceReader) -> EvalResult: ...


@dataclass(slots=True)
class Task:
    id: str
    prompt: str
    path: Path | None = None
    url: str | None = None
    tags: list[str] = field(default_factory=list)
    timeout_s: float | None = None
    # Declarative fallback check applied to the agent's final answer when no
    # eval.py is present: {"contains": ...} or {"equals": ...}.
    expected: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)  # task-level overrides

    def load_evaluator(self) -> Evaluator | None:
        if self.path is None or not (self.path / EVAL_FILE).is_file():
            return None
        spec = importlib.util.spec_from_file_location(
            f"ebrowse_evals.tasks._eval_{self.id}", self.path / EVAL_FILE
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        evaluate = getattr(module, "evaluate", None)
        if not callable(evaluate):
            raise ValueError(f"{self.path / EVAL_FILE} defines no evaluate(trace) function")
        return cast(Evaluator, evaluate)

    def default_eval(self, final_answer: str) -> EvalResult:
        if "equals" in self.expected:
            return EvalResult(success=final_answer.strip() == self.expected["equals"].strip())
        if "contains" in self.expected:
            return EvalResult(
                success=self.expected["contains"].casefold() in final_answer.casefold()
            )
        return EvalResult(success=None, details={"reason": "no evaluator or expected clause"})


@dataclass(slots=True)
class Benchmark:
    name: str
    path: Path
    tasks: list[Task]
    config: dict[str, Any] = field(default_factory=dict)  # benchmark-level defaults

    def select(
        self, patterns: list[str] | None = None, tags: list[str] | None = None
    ) -> list[Task]:
        """Filter by task-id globs (OR) and required tags (AND)."""
        out = self.tasks
        if patterns:
            out = [t for t in out if any(fnmatch.fnmatch(t.id, p) for p in patterns)]
        if tags:
            out = [t for t in out if all(tag in t.tags for tag in tags)]
        return out


def load_task(task_dir: Path) -> Task:
    data = tomllib.loads((task_dir / TASK_FILE).read_text(encoding="utf-8"))
    t = data.get("task", {})
    if "prompt" not in t:
        raise ValueError(f"{task_dir / TASK_FILE}: [task] requires a 'prompt'")
    return Task(
        id=t.get("id", task_dir.name),
        prompt=t["prompt"],
        path=task_dir,
        url=t.get("url"),
        tags=list(t.get("tags", [])),
        timeout_s=t.get("timeout_s"),
        expected=dict(t.get("expected", {})),
        config=dict(data.get("config", {})),
    )


def load_benchmark(bench_dir: Path) -> Benchmark:
    if not bench_dir.is_dir():
        raise FileNotFoundError(f"benchmark directory not found: {bench_dir}")
    name, config = bench_dir.name, {}
    bench_file = bench_dir / BENCHMARK_FILE
    if bench_file.is_file():
        data = tomllib.loads(bench_file.read_text(encoding="utf-8"))
        name = data.get("benchmark", {}).get("name", name)
        config = dict(data.get("config", {}))
    tasks = [load_task(d) for d in sorted(bench_dir.iterdir()) if (d / TASK_FILE).is_file()]
    if not tasks:
        raise ValueError(f"no task directories (containing {TASK_FILE}) under {bench_dir}")
    return Benchmark(name=name, path=bench_dir, tasks=tasks, config=config)
