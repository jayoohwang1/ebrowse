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

import hashlib
import json
import random
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ebrowse_evals.harness import AgentHarness, HarnessResult
from ebrowse_evals.tasks import Benchmark, EvalResult, Task
from ebrowse_evals.trace.records import AgentMessage, PromptSnapshot, RunEnd, RunMeta, Step
from ebrowse_evals.trace.store import TraceReader, TraceWriter

HARNESS_DEFAULTS: dict[str, Any] = {
    "tool": "ebrowse",
    "worktree": False,
    "timeout_s": 600.0,
    "tool_call_limit": 200,
    "jobs": 1,
    "capture": True,  # instrument the ebrowse shim (spool + debug log); ebrowse-only
}
_MAX_INLINE_TRANSCRIPT_BYTES = 200_000


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
    if result.tool_limit_hit:
        return "tool_limit"
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
    run_dir = run_dir.resolve()  # see run_tasks: paths embed in shim + subprocess args
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
    result = harness.run(
        task.prompt,
        workdir=workdir,
        env={},
        timeout_s=timeout_s,
        run_dir=run_dir,
        start_url=task.url,
        tool_call_limit=config.get("tool_call_limit"),
    )
    if result.start_prompt:
        start_bytes = result.start_prompt.encode()
        writer.write(
            PromptSnapshot(
                kind="start",
                text=result.start_prompt
                if len(start_bytes) <= _MAX_INLINE_TRANSCRIPT_BYTES
                else "",
                text_ref=(
                    writer.put_blob(start_bytes, ".txt")
                    if len(start_bytes) > _MAX_INLINE_TRANSCRIPT_BYTES
                    else None
                ),
                sha256=hashlib.sha256(start_bytes).hexdigest(),
            )
        )
    for sequence, system_prompt in enumerate(result.system_prompts, 1):
        system_bytes = system_prompt.encode()
        writer.write(
            PromptSnapshot(
                kind="system",
                text=system_prompt if len(system_bytes) <= _MAX_INLINE_TRANSCRIPT_BYTES else "",
                text_ref=(
                    writer.put_blob(system_bytes, ".txt")
                    if len(system_bytes) > _MAX_INLINE_TRANSCRIPT_BYTES
                    else None
                ),
                sha256=hashlib.sha256(system_bytes).hexdigest(),
                sequence=sequence,
            )
        )
    for message in result.messages:
        content_bytes = json.dumps(message.content, ensure_ascii=False).encode()
        writer.write(
            AgentMessage(
                ts=message.ts,
                sequence=message.sequence,
                message_id=message.message_id,
                parent_id=message.parent_id,
                turn=message.turn,
                role=message.role,
                content=message.content
                if len(content_bytes) <= _MAX_INLINE_TRANSCRIPT_BYTES
                else [],
                content_ref=(
                    writer.put_blob(content_bytes, ".json")
                    if len(content_bytes) > _MAX_INLINE_TRANSCRIPT_BYTES
                    else None
                ),
                tool_call_id=message.tool_call_id,
                tool_name=message.tool_name,
                model=message.model,
                provider=message.provider,
                usage=message.usage,
                metadata=message.metadata,
                stop_reason=message.stop_reason,
                is_error=message.is_error,
                is_start=message.is_start,
            )
        )
    steps = [
        Step(
            step=i,
            command=ps.command,
            output=ps.output,
            agent_text=ps.agent_text,
            tokens=ps.tokens,
            latency_s=ps.latency_s,
            error={"class": "tool_error"} if ps.is_error else None,
            message_id=ps.message_id,
            tool_call_id=ps.tool_call_id,
            tool_name=ps.tool_name,
            call_index=ps.call_index,
        )
        for i, ps in enumerate(result.steps, 1)
    ]
    # Instrumented-shim artifacts (harness.py): per-call capture payloads and
    # the daemon's tier-1 debug log, joined to steps by shim call number.
    # Both attachers mutate the Step records, so they run before the write.
    from ebrowse_evals import ingest
    from ebrowse_evals.harness import DEBUG_LOG_FILE, SPOOL_DIR

    spool_dir = run_dir / SPOOL_DIR
    if spool_dir.is_dir() and any(spool_dir.glob("*.json")):
        ingest.attach_spool(writer, steps, spool_dir)
    debug_log = run_dir / DEBUG_LOG_FILE
    if debug_log.is_file():
        ingest.attach_debug_log(writer, steps, debug_log)
    for step in steps:
        if capture is not None:  # live hook for harnesses that support it
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
    # Absolute: run_dir paths get embedded in the shim script and passed to the
    # agent subprocess (--session-dir, EBROWSE_DEBUG_LOG), whose cwd is the
    # run's workdir — a relative runs_root would scatter artifacts under it.
    runs_root = runs_root.resolve()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prepared: list[tuple[Task, dict[str, Any], Path]] = []
    reserved: set[Path] = set()
    for task in tasks:
        config = resolve_config(benchmark.config if benchmark else None, task.config, cli_config)
        base = f"{name}-{task.id}" if name else f"{task.id}-{stamp}"
        run_dir = runs_root / base
        n = 2
        while run_dir.exists() or run_dir in reserved:
            run_dir = runs_root / f"{base}-{n}"
            n += 1
        reserved.add(run_dir)
        prepared.append((task, config, run_dir))

    def execute(item: tuple[Task, dict[str, Any], Path]) -> RunResult:
        task, config, run_dir = item
        return run_task(
            task,
            harness,
            config,
            run_dir,
            benchmark=benchmark.name if benchmark else None,
            repo_root=repo_root,
            capture=capture,
        )

    jobs = max(1, int(cli_config.get("jobs") or 1))
    if jobs == 1:
        return [execute(item) for item in prepared]
    by_index: dict[int, RunResult] = {}
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="ebrowse-eval") as pool:
        futures = {pool.submit(execute, item): i for i, item in enumerate(prepared)}
        for future in as_completed(futures):
            by_index[futures[future]] = future.result()
    return [by_index[i] for i in range(len(prepared))]


__all__ = [
    "HARNESS_DEFAULTS",
    "RunResult",
    "StepCapture",
    "resolve_config",
    "run_task",
    "run_tasks",
    "select_tasks",
]
