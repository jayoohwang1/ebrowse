# Tasks and benchmarks

A **task** is a directory; a **benchmark** is a directory of task directories.
External datasets (e.g. Online-Mind2Web) are converted to this model by
adapters at load time — the runner never special-cases a source format.

## task.toml

```toml
[task]
# id defaults to the directory name
prompt = "Open http://127.0.0.1:8196/list.html and count the products."
url = "http://127.0.0.1:8196/list.html"   # optional; informational + env setup
tags = ["fixture", "read-only"]           # selection: --tag is AND, --task glob is OR
timeout_s = 120

[task.expected]          # optional declarative check on the final answer,
contains = "24"          # used only when no eval.py exists ("equals" also works)

[config]                 # optional task-level overrides (merged into run config)
```

## eval.py (optional)

```python
from ebrowse_evals.tasks import EvalResult
from ebrowse_evals.trace.store import TraceReader

def evaluate(trace: TraceReader) -> EvalResult:
    ...
```

Evaluators receive the **trace**, not just the final answer, so they can score
process (step count, error recovery, verbs used) as well as outcome.
`success=None` means "couldn't judge" — distinct from failure.

## benchmark.toml (optional)

```toml
[benchmark]
name = "fixtures"
[config]                 # benchmark-level defaults
```

Config layering (later overrides earlier): harness defaults → benchmark
`[config]` → task `[config]` → CLI flags. The fully resolved result is
persisted into the run's `run_meta` record.

For the pi/ebrowse harness, `url` is also the initial browser location. The
harness opens it before the agent starts; this setup action is excluded from
the trace's agent-step numbering and tool-call limit. Relevant config keys are
`timeout_s`, `tool_call_limit` (default 200; zero disables), and `jobs`
(default 1; CLI `--jobs` controls benchmark concurrency). Pi/ebrowse runs also
default to the browser-only tool and task-host navigation policy documented in
[pi-browser-policy.md](pi-browser-policy.md).

Example benchmark: `evals/tests/fixtures/benchmark/`.
