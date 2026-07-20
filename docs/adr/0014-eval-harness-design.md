# 0014 — Eval harness: in-repo package, replay over logging, ordinal command join

Status: accepted (2026-07-18)

## Context

The evaluation harness (`evals/`, package `ebrowse-evals`) runs a real coding
agent (pi) over browsing tasks and records rich traces: what the agent saw,
unconditional per-step browser state (screenshot + DomSnapshot), and ebrowse's
internal tier-1 debug events (ADR 0013). Three choices would surprise a future
reader.

## Decision

1. **In-repo workspace package, not a separate repo.** The harness co-evolves
   with ebrowse internals (trace schema ↔ debug events, `replay` ↔ pure core,
   worktree runs exercise uncommitted code). One PR changes both sides; CI
   catches drift. It is a uv workspace member with its own dependencies so the
   ebrowse install footprint is untouched.
2. **Replay instead of exhaustive logging (two tiers).** Because `core/` is
   pure and every step's DomSnapshot is stored as a content-addressed blob,
   full pipeline decision traces are *regenerated on demand*
   (`ebrowse-eval replay`) rather than logged. Only what live replay cannot
   reproduce is recorded at run time: timings, action execution, ref
   lifecycle, diff verdicts, anomalies, page events.
3. **Ordinal command join, no timestamp heuristics.** pi runs to completion and
   steps are parsed post-hoc, so the runner cannot hook tool-calls live.
   Instead the trusted wrapper around `ebrowse` numbers every
   invocation: it exports `EBROWSE_REQUEST_ID=call-<n>`, then synchronously
   spools a `debug-capture` payload to `capture/<n>.json` — the only moment
   post-action state is observable. After the run, `ingest.py` joins both to
   trace steps ordinally (the n-th executed ebrowse step is call n); a count
   mismatch is surfaced as a `join_mismatch` anomaly, never silently
   mis-attributed.

## Consequences

- Traces are self-sufficient for offline analysis: new render/split/diff code
  can be evaluated against every stored trajectory without re-running agents.
- Capture and debug logging apply only to ebrowse-driven runs (the wrapper is
  the instrumentation point); other tools get agent-side records only.
- Paths embedded in the shim/subprocess args must be absolute (the agent's
  cwd differs); pi's session lands only at exit, so the streamed event log is
  the fallback step source for timed-out runs.
