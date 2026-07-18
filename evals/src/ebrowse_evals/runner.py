"""Task runner: select tasks, drive an agent harness, emit valid traces.

Config layering (later overrides earlier, None means "unset"):
harness defaults → benchmark [config] → task [config] → CLI flags. The fully
resolved dict is persisted in run_meta so a run stays reproducible after the
flags are forgotten.

Browser-state capture (screenshots/DomSnapshots/browser events) is NOT done
here — a ``StepCapture`` hook is invoked per step so the capture layer can
enrich each Step record and append its own records before the step is written.
"""

from __future__ import annotations

import random
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ebrowse_evals.harness import AgentHarness, HarnessResult
from ebrowse_evals.tasks import Benchmark, EvalResult, Task
from ebrowse_evals.trace.records import RunEnd, RunMeta, Step
from ebrowse_evals.trace.store import TraceReader, TraceWriter

HARNESS_DEFAULTS: dict[str, Any] = {
    "tool": "ebrowse",
    "worktree": False,
    "timeout_s": 600.0,
}


@runtime_checkable
class StepCapture(Protocol):
    """Hook point for the browser-state capture layer. Called once per agent
    tool-call with the Step *before* it is written; implementations may fill
    ``browser``/``screenshot``/``dom_snapshot`` and append extra records via
    the writer (browser_event, ebrowse_log, anomaly)."""

    def on_step(self, writer: TraceWriter, step: Step) -> None: ...


def resolve_config(*layers: dict[str, Any] | None) -> dict[str, Any]:
    """Merge config layers left-to-right; None values never override."""
    out = dict(HARNESS_DEFAULTS)
    for layer in layers:
        for k, v in (layer or {}).items():
            if v is not None:
                out[k] = v
    return out


def select_tasks(
    bench: Benchmark,
    patterns: list[str] | None = None,
    tags: list[str] | None = None,
    sample: int | None = None,
    seed: int | None = None,
) -> list[Task]:
    """Glob/tag filtering, then optional seeded sampling (stable order)."""
    tasks = bench.select(patterns=patterns, tags=tags)
    if sample is not None and sample < len(tasks):
        picked = set(random.Random(seed).sample(range(len(tasks)), sample))
        tasks = [t for i, t in enumerate(tasks) if i in picked]
    return tasks


def _git_state(repo_root: Path) -> tuple[str | None, bool | None]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
        return sha, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def _ebrowse_version() -> str | None:
    try:
        return metadata.version("ebrowse")
    except metadata.PackageNotFoundError:
        return None


@dataclass(slots=True)
class RunResult:
    task_id: str
    run_dir: Path
    outcome: str
    eval: EvalResult


def _outcome(result: HarnessResult, eval_result: EvalResult) -> str:
    if result.timed_out:
        return "timeout"
    if result.exit_code != 0:
        return "error"
    if eval_result.success is True:
        return "success"
    if eval_result.success is False:
        return "failure"
    return "unknown"


def run_task(
    task: Task,
    harness: AgentHarness,
    config: dict[str, Any],
    run_dir: Path,
    benchmark: str | None = None,
    repo_root: Path | None = None,
    capture: StepCapture | None = None,
) -> RunResult:
    """Execute one task through the harness and write a complete trace."""
    repo_root = repo_root or Path.cwd()
    writer = TraceWriter(run_dir)
    git_sha, git_dirty = _git_state(repo_root)
    writer.write(
        RunMeta(
            run_id=run_dir.name,
            task_id=task.id,
            prompt=task.prompt,
            benchmark=benchmark,
            config=config,
            agent=harness.describe(),
            git_sha=git_sha,
            git_dirty=git_dirty,
            ebrowse_version=_ebrowse_version(),
            ebrowse_mode="worktree" if config.get("worktree") else "installed",
        )
    )
    timeout_s = task.timeout_s if task.timeout_s is not None else config.get("timeout_s")
    workdir = run_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    result = harness.run(task.prompt, workdir=workdir, env={}, timeout_s=timeout_s, run_dir=run_dir)
    for i, ps in enumerate(result.steps, 1):
        step = Step(
            step=i,
            command=ps.command,
            output=ps.output,
            agent_text=ps.agent_text,
            tokens=ps.tokens,
            latency_s=ps.latency_s,
            error={"class": "tool_error"} if ps.is_error else None,
        )
        if capture is not None:
            capture.on_step(writer, step)
        writer.write(step)
    evaluator = task.load_evaluator()
    try:
        if evaluator is not None:
            eval_result = evaluator(TraceReader(run_dir))
        else:
            eval_result = task.default_eval(result.final_answer)
    except Exception as e:  # an evaluator bug must not lose the trace
        eval_result = EvalResult(success=None, details={"evaluator_error": str(e)})
    writer.write(
        RunEnd(
            outcome=_outcome(result, eval_result),
            steps=len(result.steps),
            totals=result.totals,
            eval=eval_result.to_dict(),
        )
    )
    return RunResult(
        task_id=task.id,
        run_dir=run_dir,
        outcome=_outcome(result, eval_result),
        eval=eval_result,
    )


def run_tasks(
    tasks: list[Task],
    harness: AgentHarness,
    benchmark: Benchmark | None,
    cli_config: dict[str, Any],
    runs_root: Path,
    repo_root: Path | None = None,
    capture: StepCapture | None = None,
    name: str | None = None,
) -> list[RunResult]:
    """Run each selected task in its own run directory under runs_root."""
    results: list[RunResult] = []
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for task in tasks:
        config = resolve_config(benchmark.config if benchmark else None, task.config, cli_config)
        base = f"{name}-{task.id}" if name else f"{task.id}-{stamp}"
        run_dir = runs_root / base
        n = 2
        while run_dir.exists():
            run_dir = runs_root / f"{base}-{n}"
            n += 1
        results.append(
            run_task(
                task,
                harness,
                config,
                run_dir,
                benchmark=benchmark.name if benchmark else None,
                repo_root=repo_root,
                capture=capture,
            )
        )
    return results


__all__ = [
    "HARNESS_DEFAULTS",
    "RunResult",
    "StepCapture",
    "resolve_config",
    "run_task",
    "run_tasks",
    "select_tasks",
]
