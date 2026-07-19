# ebrowse-evals

Evaluation harness for ebrowse: run agents on browsing tasks, capture rich
traces (screenshots + DomSnapshots + internal events every step), and inspect
them — as a human (two-lane trace viewer) or as an LLM (canned queries).

A uv workspace member sharing the repo venv; `make setup` installs it.
Everything is a thin reader/writer of the **trace schema** —
[docs/trace-schema.md](docs/trace-schema.md) is the contract,
[docs/tasks.md](docs/tasks.md) covers task/benchmark definition, and
[docs/inspect.md](docs/inspect.md) documents the inspection queries.

Status: working end to end (validated live on the fixture site and
traderjoes.com; design record in
[ADR 0014](../docs/adr/0014-eval-harness-design.md)) — schema, task model,
`ebrowse-eval validate`/`tasks`, the runner (`ebrowse-eval run` drives the pi
harness over a benchmark or single task and writes a fully-joined trace per
run), the per-step capture layer (below), the `view` trace viewer, and the
inspection queries (`overview`, `anomalies`, `errors`, `step`, `trace-ref`,
`trace-section`, `timing`, `grep`, `replay`). The legacy scripts in
`../experiments/` still cover one gap — `summarize-run.py`'s side-by-side
token comparison — and retire once that's ported.

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

For pi runs the capture moment is the **instrumented shim**: `run` (unless
`--no-capture`; ebrowse-only) wraps the `ebrowse` command so each invocation
is numbered, stamped with `EBROWSE_REQUEST_ID=call-<n>`, and followed
synchronously by a debug-capture spool to `capture/<n>.json`; the shim env also
enables the daemon's tier-1 debug log (`ebrowse-debug.jsonl`). After the run,
`ingest.py` joins both back to trace steps *ordinally* (the n-th
ebrowse-invoking step is call n — no timestamp heuristics; mismatches surface
as a `join_mismatch` anomaly), fills each step's browser/screenshot/DomSnapshot
fields, re-emits daemon events as `ebrowse_log` records, promotes warn-level
anomaly events to `anomaly` records, and rolls phase timings into `step.timing`.

```bash
uv run ebrowse-eval validate evals/tests/fixtures/sample-trace
uv run ebrowse-eval tasks evals/tests/fixtures/benchmark --tag fixture
# run tasks through pi (provider/model from flags, $PI_PROVIDER/$PI_MODEL, or experiments/.env)
uv run ebrowse-eval run evals/tests/fixtures/benchmark --task 'list-*' --worktree
uv run ebrowse-eval view evals/tests/fixtures/sample-trace --open
uv run ebrowse-eval overview evals/tests/fixtures/sample-trace
uv run ebrowse-eval trace-ref evals/tests/fixtures/sample-trace @e1
```

`run` selects tasks (`--task` globs OR, `--tag` AND, `--sample N --seed S`),
layers config (harness defaults → benchmark `[config]` → task `[config]` →
flags), and persists the resolved config + git sha + ebrowse version/mode in
each run's `run_meta`. `--worktree` is the port of `run-agent.sh -w`: it shims
`ebrowse` to this checkout's `.venv` and stops any running daemon first. The
agent boundary is the `AgentHarness` protocol (`harness.py`); `runner.StepCapture`
is the hook where the capture layer enriches each step.

`view` renders a run into a single self-contained HTML page — a two-lane
step log (right: what the agent saw; left: screenshot filmstrip + internals
behind expanders) with the anomaly list up top. See
[docs/viewer.md](docs/viewer.md).
