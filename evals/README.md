# ebrowse-evals

Evaluation harness for ebrowse: run agents on browsing tasks, capture rich
traces (screenshots + DomSnapshots + internal events every step), and inspect
them — as a human (two-lane trace viewer) or as an LLM (canned queries).

A uv workspace member sharing the repo venv; `make setup` installs it.
Everything is a thin reader/writer of the **trace schema** —
[docs/trace-schema.md](docs/trace-schema.md) is the contract, and
[docs/tasks.md](docs/tasks.md) covers task/benchmark definition.

Status: Phase 0 — schema, task model, and `ebrowse-eval validate`/`tasks`
exist; runner, capture, viewer, and inspection queries land next. The legacy
scripts in `../experiments/` keep working until this reaches parity.

```bash
uv run ebrowse-eval validate evals/tests/fixtures/sample-trace
uv run ebrowse-eval tasks evals/tests/fixtures/benchmark --tag fixture
```
