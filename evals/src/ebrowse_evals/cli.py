"""ebrowse-eval CLI. Phase 0 scope: schema/trace utilities and task listing.

The runner (`run`), viewer (`view`), and inspection queries (`anomalies`,
`trace-ref`, ...) land as subcommands here in later phases -- one entry point,
each command a thin reader/writer of the trace schema.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ebrowse_evals.tasks import load_benchmark
from ebrowse_evals.trace.store import TraceReader

REPO_ROOT = Path(__file__).resolve().parents[3]


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        reader = TraceReader(Path(args.run_dir))
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    problems = reader.validate()
    if problems:
        for p in problems:
            print(f"invalid: {p}")
        return 1
    meta = reader.meta()
    steps = reader.steps()
    anomalies = reader.anomalies()
    assert meta is not None  # validate() guarantees one run_meta
    print(
        f"ok: run {meta.run_id or '?'} task={meta.task_id or '?'} "
        f"schema=v{meta.schema_version} steps={len(steps)} anomalies={len(anomalies)}"
    )
    return 0


def _cmd_tasks(args: argparse.Namespace) -> int:
    try:
        bench = load_benchmark(Path(args.benchmark_dir))
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    tasks = bench.select(patterns=args.task or None, tags=args.tag or None)
    print(f"benchmark {bench.name}: {len(tasks)}/{len(bench.tasks)} tasks selected")
    for t in tasks:
        tags = f"  [{', '.join(t.tags)}]" if t.tags else ""
        print(f"  {t.id}{tags}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import os

    from ebrowse_evals.harness import PiHarness, load_env_file
    from ebrowse_evals.runner import resolve_config, run_tasks, select_tasks
    from ebrowse_evals.tasks import TASK_FILE, load_task

    target = Path(args.target)
    try:
        if (target / TASK_FILE).is_file():
            bench = None
            tasks = [load_task(target)]
        else:
            bench = load_benchmark(target)
            tasks = select_tasks(
                bench,
                patterns=args.task or None,
                tags=args.tag or None,
                sample=args.sample,
                seed=args.seed,
            )
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not tasks:
        print("error: no tasks selected — run 'ebrowse-eval tasks' to inspect", file=sys.stderr)
        return 2

    # experiments/.env supplies PI_PROVIDER/PI_MODEL defaults; flags override.
    env_defaults = load_env_file(REPO_ROOT / "experiments" / ".env")
    provider = args.provider or os.environ.get("PI_PROVIDER") or env_defaults.get("PI_PROVIDER")
    model = args.model or os.environ.get("PI_MODEL") or env_defaults.get("PI_MODEL")
    if not provider or not model:
        print(
            "error: no provider/model — pass --provider/--model, or set PI_PROVIDER/PI_MODEL "
            "(experiments/.env works; see experiments/README.md)",
            file=sys.stderr,
        )
        return 2

    cli_config = {
        "provider": provider,
        "model": model,
        "tool": args.tool,
        "worktree": True if args.worktree else None,
        "timeout_s": args.timeout,
    }
    # Harness identity (provider/model/tool/worktree) is run-level, not per-task,
    # so resolve it once from harness defaults + benchmark config + flags.
    base = resolve_config(bench.config if bench else None, cli_config)
    harness = PiHarness(
        provider=str(base["provider"]),
        model=str(base["model"]),
        tool=str(base.get("tool", "none")),
        repo_root=REPO_ROOT,
        worktree=bool(base.get("worktree")),
    )
    results = run_tasks(
        tasks,
        harness,
        bench,
        cli_config,
        runs_root=Path(args.runs_dir),
        repo_root=REPO_ROOT,
        name=args.name,
    )
    failed = 0
    for r in results:
        print(f"{r.outcome:8s} {r.task_id}  -> {r.run_dir}")
        if r.outcome not in ("success", "unknown"):
            failed += 1
    print(f"{len(results)} run(s), {failed} not successful")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ebrowse-eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="check a trace run directory against the schema")
    p_validate.add_argument("run_dir")
    p_validate.set_defaults(func=_cmd_validate)

    p_tasks = sub.add_parser("tasks", help="list tasks in a benchmark directory")
    p_tasks.add_argument("benchmark_dir")
    p_tasks.add_argument("--task", action="append", help="task-id glob (repeatable, OR)")
    p_tasks.add_argument("--tag", action="append", help="required tag (repeatable, AND)")
    p_tasks.set_defaults(func=_cmd_tasks)

    p_run = sub.add_parser("run", help="run tasks through the agent harness, writing traces")
    p_run.add_argument("target", help="benchmark directory or single task directory")
    p_run.add_argument("--task", action="append", help="task-id glob (repeatable, OR)")
    p_run.add_argument("--tag", action="append", help="required tag (repeatable, AND)")
    p_run.add_argument("--sample", type=int, help="run a random sample of N selected tasks")
    p_run.add_argument("--seed", type=int, help="seed for --sample")
    p_run.add_argument("--provider", help="pi provider (default: $PI_PROVIDER / experiments/.env)")
    p_run.add_argument("--model", help="model id (default: $PI_MODEL / experiments/.env)")
    p_run.add_argument(
        "--tool",
        choices=["ebrowse", "agent-browser", "none"],
        default=None,
        help="prepend that tool's operating guide to the prompt (default: ebrowse)",
    )
    p_run.add_argument(
        "--worktree",
        action="store_true",
        help="shim `ebrowse` to this checkout's .venv (stops any running daemon first)",
    )
    p_run.add_argument("--timeout", type=float, help="per-task timeout in seconds")
    p_run.add_argument("--runs-dir", default="runs", help="where run directories go")
    p_run.add_argument("--name", help="run-name prefix (default: <task-id>-<utc timestamp>)")
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
