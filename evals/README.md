# ebrowse-evals

Evaluation harness for ebrowse: run agents on browsing tasks, capture rich
traces (screenshots + DomSnapshots + internal events every step), and inspect
them — as a human (two-lane trace viewer) or as an LLM (canned queries).

A uv workspace member sharing the repo venv; `make setup` installs it.
Everything is a thin reader/writer of the **trace schema** —
[docs/trace-schema.md](docs/trace-schema.md) is the contract, and
[docs/tasks.md](docs/tasks.md) covers task/benchmark definition.

Status: Phase 1 — schema, task model, `ebrowse-eval validate`/`tasks`, the
runner (`ebrowse-eval run` drives the pi harness over a benchmark or single
task and writes a trace per run), and the per-step capture layer (below).
The legacy scripts in `../experiments/` keep working until this reaches parity.

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
# run tasks through pi (provider/model from flags, $PI_PROVIDER/$PI_MODEL, or experiments/.env)
uv run ebrowse-eval run evals/tests/fixtures/benchmark --task 'list-*' --worktree
uv run ebrowse-eval view evals/tests/fixtures/sample-trace --open
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
