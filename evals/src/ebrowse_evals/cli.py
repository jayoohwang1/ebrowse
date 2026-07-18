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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
