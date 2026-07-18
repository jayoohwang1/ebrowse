# ebrowse-evals

Evaluation harness for ebrowse: run agents on browsing tasks, capture rich
traces (screenshots + DomSnapshots + internal events every step), and inspect
them — as a human (two-lane trace viewer) or as an LLM (canned queries).

A uv workspace member sharing the repo venv; `make setup` installs it.
Everything is a thin reader/writer of the **trace schema** —
[docs/trace-schema.md](docs/trace-schema.md) is the contract, and
[docs/tasks.md](docs/tasks.md) covers task/benchmark definition.

Status: Phase 0 (schema, task model, `validate`/`tasks`) plus the runner —
`ebrowse-eval run` drives the pi harness over a benchmark or single task and
writes a trace per run; browser-state capture, viewer, and inspection queries
land next. The legacy scripts in `../experiments/` keep working until this
reaches parity.

```bash
uv run ebrowse-eval validate evals/tests/fixtures/sample-trace
uv run ebrowse-eval tasks evals/tests/fixtures/benchmark --tag fixture
# run tasks through pi (provider/model from flags, $PI_PROVIDER/$PI_MODEL, or experiments/.env)
uv run ebrowse-eval run evals/tests/fixtures/benchmark --task 'list-*' --worktree
```

`run` selects tasks (`--task` globs OR, `--tag` AND, `--sample N --seed S`),
layers config (harness defaults → benchmark `[config]` → task `[config]` →
flags), and persists the resolved config + git sha + ebrowse version/mode in
each run's `run_meta`. `--worktree` is the port of `run-agent.sh -w`: it shims
`ebrowse` to this checkout's `.venv` and stops any running daemon first. The
agent boundary is the `AgentHarness` protocol (`harness.py`); `runner.StepCapture`
is the hook where the capture layer enriches each step.
