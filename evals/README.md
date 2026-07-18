# ebrowse-evals

Evaluation harness for ebrowse: run agents on browsing tasks, capture rich
traces (screenshots + DomSnapshots + internal events every step), and inspect
them — as a human (two-lane trace viewer) or as an LLM (canned queries).

A uv workspace member sharing the repo venv; `make setup` installs it.
Everything is a thin reader/writer of the **trace schema** —
[docs/trace-schema.md](docs/trace-schema.md) is the contract,
[docs/tasks.md](docs/tasks.md) covers task/benchmark definition, and
[docs/inspect.md](docs/inspect.md) documents the inspection queries.

Status: schema, task model, `ebrowse-eval validate`/`tasks`, and the
inspection CLI (`overview`, `anomalies`, `errors`, `step`, `trace-ref`,
`trace-section`, `timing`, `grep`, `replay`) exist; runner, capture, and the
viewer land next. The legacy scripts in `../experiments/` keep working until
this reaches parity.

```bash
uv run ebrowse-eval validate evals/tests/fixtures/sample-trace
uv run ebrowse-eval tasks evals/tests/fixtures/benchmark --tag fixture
uv run ebrowse-eval overview evals/tests/fixtures/sample-trace
uv run ebrowse-eval trace-ref evals/tests/fixtures/sample-trace @e1
```
