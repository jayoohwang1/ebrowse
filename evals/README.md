# ebrowse-evals

Evaluation harness for ebrowse: run agents on browsing tasks, capture rich
traces (screenshots + DomSnapshots + internal events every step), and inspect
them — as a human (two-lane trace viewer) or as an LLM (canned queries).

A uv workspace member sharing the repo venv; `make setup` installs it.
Everything is a thin reader/writer of the **trace schema** —
[docs/trace-schema.md](docs/trace-schema.md) is the contract, and
[docs/tasks.md](docs/tasks.md) covers task/benchmark definition.

Status: Phase 0+ — schema, task model, `ebrowse-eval validate`/`tasks`, and the
per-step capture layer (below) exist; runner, viewer, and inspection queries
land next. The legacy scripts in `../experiments/` keep working until this
reaches parity.

## Per-step capture (`ebrowse_evals.capture`)

`StepCapture` is the hook the runner calls after every agent tool-call: it
captures post-action browser state **unconditionally** (URL/title/tabs/
viewport/scroll, a viewport screenshot, the DomSnapshot) into the Step record —
`on_step(writer, step)` fills the record in place — plus `browser_event`
records for console output, failed requests, navigations, and dialogs
accumulated since the previous step. It talks to the running ebrowse daemon
over its unix socket via the additive `debug-capture` verb (no second
Playwright connection; the daemon owns the browser and serializes capture under
the session lock), and the daemon reuses the DomSnapshot it already took for
the previous verb's observation when no possibly-mutating verb ran since. Any
capture failure degrades to a partial step plus an `anomaly` record; it never
raises into the runner. Details in `src/ebrowse_evals/capture.py`.

```bash
uv run ebrowse-eval validate evals/tests/fixtures/sample-trace
uv run ebrowse-eval tasks evals/tests/fixtures/benchmark --tag fixture
```
