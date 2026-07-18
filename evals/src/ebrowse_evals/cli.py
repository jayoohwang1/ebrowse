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
