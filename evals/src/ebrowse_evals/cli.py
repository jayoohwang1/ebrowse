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
        "tool_call_limit": args.tool_call_limit,
        "jobs": args.jobs,
        "capture": False if args.no_capture else None,
    }
    # Harness identity (provider/model/tool/worktree) is run-level, not per-task,
    # so resolve it once from harness defaults + benchmark config + flags.
    base = resolve_config(bench.config if bench else None, cli_config)
    tool = str(base.get("tool", "none"))
    # Capture instruments the ebrowse shim, so it only applies to ebrowse runs;
    # default on there (rich traces are the point) unless explicitly disabled.
    capture = bool(base.get("capture", True)) and tool == "ebrowse"
    cli_config["capture"] = capture
    cli_config["jobs"] = int(base.get("jobs", 1))
    harness = PiHarness(
        provider=str(base["provider"]),
        model=str(base["model"]),
        tool=tool,
        repo_root=REPO_ROOT,
        worktree=bool(base.get("worktree")),
        capture=capture,
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


def _cmd_view(args: argparse.Namespace) -> int:
    from ebrowse_evals.viewer import render_run

    run_dir = Path(args.run_dir)
    try:
        html = render_run(run_dir)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = Path(args.output) if args.output else run_dir / "trace.html"
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}")
    if args.open:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from ebrowse_evals.viewer_server import serve_runs

    try:
        serve_runs(Path(args.runs_dir), args.host, args.port, args.open)
    except (FileNotFoundError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


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
    p_run.add_argument(
        "--tool-call-limit",
        type=int,
        help="maximum agent tool calls per task (default: 200; 0 disables)",
    )
    p_run.add_argument(
        "--jobs", type=int, default=None, help="number of tasks to run concurrently (default: 1)"
    )
    p_run.add_argument(
        "--no-capture",
        action="store_true",
        help="skip per-step browser-state capture + ebrowse debug-log ingestion",
    )
    p_run.add_argument("--runs-dir", default="runs", help="where run directories go")
    p_run.add_argument("--name", help="run-name prefix (default: <task-id>-<utc timestamp>)")
    p_run.set_defaults(func=_cmd_run)

    p_view = sub.add_parser("view", help="render a run directory to a self-contained HTML page")
    p_view.add_argument("run_dir")
    p_view.add_argument("-o", "--output", help="output path (default: <run-dir>/trace.html)")
    p_view.add_argument("--open", action="store_true", help="open the page in a browser")
    p_view.set_defaults(func=_cmd_view)

    p_serve = sub.add_parser("serve", help="browse all trace runs in a central local web app")
    p_serve.add_argument("runs_dir", nargs="?", default="runs", help="runs root (default: runs)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--open", action="store_true", help="open the app in the default browser")
    p_serve.set_defaults(func=_cmd_serve)

    # -- inspection queries (evals/docs/inspect.md) --------------------------
    from ebrowse_evals import inspect as _inspect

    def _rd(p: argparse.ArgumentParser) -> None:
        p.add_argument("run_dir")
        p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("overview", help="run meta, outcome, per-step table")
    _rd(p)
    p.set_defaults(func=lambda a: _inspect.cmd_overview(a.run_dir, a.as_json))

    p = sub.add_parser("anomalies", help="triage list of pipeline anomalies")
    _rd(p)
    p.set_defaults(func=lambda a: _inspect.cmd_anomalies(a.run_dir, a.as_json))

    p = sub.add_parser("errors", help="failed steps + whether recovery hints were followed")
    _rd(p)
    p.set_defaults(func=lambda a: _inspect.cmd_errors(a.run_dir, a.as_json))

    p = sub.add_parser("step", help="everything recorded for one step")
    _rd(p)
    p.add_argument("n", type=int)
    p.add_argument("--full", action="store_true", help="untruncated agent output")
    p.add_argument("--debug", action="store_true", help="include debug-level ebrowse logs")
    p.set_defaults(func=lambda a: _inspect.cmd_step(a.run_dir, a.n, a.full, a.debug, a.as_json))

    p = sub.add_parser("trace-ref", help="history of one element ref (@eN)")
    _rd(p)
    p.add_argument("ref")
    p.set_defaults(func=lambda a: _inspect.cmd_trace_entity(a.run_dir, a.ref, a.as_json))

    p = sub.add_parser("trace-section", help="history of one section id (sN)")
    _rd(p)
    p.add_argument("sid")
    p.set_defaults(func=lambda a: _inspect.cmd_trace_entity(a.run_dir, a.sid, a.as_json))

    p = sub.add_parser("timing", help="per-step phase breakdown + totals")
    _rd(p)
    p.set_defaults(func=lambda a: _inspect.cmd_timing(a.run_dir, a.as_json))

    p = sub.add_parser("grep", help="regex over trace records (escape hatch)")
    _rd(p)
    p.add_argument("pattern")
    p.add_argument("--type", action="append", dest="types", help="record type (repeatable)")
    p.add_argument("--step", type=int)
    p.add_argument("--module")
    p.add_argument("--level")
    p.set_defaults(
        func=lambda a: _inspect.cmd_grep(
            a.run_dir, a.pattern, a.types, a.step, a.module, a.level, a.as_json
        )
    )

    p = sub.add_parser("replay", help="re-render a step's DomSnapshot through pure core")
    p.add_argument("run_dir")
    p.add_argument("--step", type=int, required=True, dest="n")
    p.add_argument("--section", help="render one section's markdown instead of the outline")
    p.set_defaults(func=_cmd_replay)

    args = parser.parse_args(argv)
    return args.func(args)


def _cmd_replay(args: argparse.Namespace) -> int:
    from ebrowse_evals.replay import cmd_replay  # defer: imports ebrowse core

    return cmd_replay(args.run_dir, args.n, args.section)


if __name__ == "__main__":
    sys.exit(main())
