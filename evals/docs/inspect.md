# Inspection queries

Canned, entity-centric queries over a trace run directory so an LLM (or human)
can root-cause a run without reading the whole trace. Every command takes a
`<run-dir>` and prints concise deterministic plain text (a few lines,
golden-tested); add `--json` for structured output. Misses are graceful and
name what *does* exist ("no events for @e9; refs seen in this trace: @e1, @e2").

Suggested workflow: `overview` → `anomalies`/`errors` → `step`/`trace-ref` →
`replay` when you need tier-2 detail the trace doesn't store.

## Query catalog

| command | question it answers |
|---|---|
| `overview <run-dir>` | Where do I start? Run meta one-liner, outcome/totals, per-step table (exit, latency, `A`/`E` badge for anomaly/error, command, url), post-hoc summaries. |
| `anomalies <run-dir>` | What surprised the pipeline? One line per anomaly: step, kind, message. |
| `errors <run-dir>` | Which steps failed, what recovery did the error name, and did the agent's NEXT command follow it? (`[followed -> …]` / `[ignored -> …]` / `[last step]`) |
| `step <run-dir> <n> [--full] [--debug]` | Everything for one step: command, agent text, exit/latency/tokens, timing phases, browser state, blob refs, output (truncated; `--full` for verbatim), browser events, ebrowse logs (debug level hidden unless `--debug`), anomalies. |
| `trace-ref <run-dir> <@eN>` | History of one element ref, in step order: every log/anomaly whose structured fields name it, plus every step command/output line mentioning it. |
| `trace-section <run-dir> <sN>` | Same for a section id (word-boundary match — `s2` never matches `s12`). |
| `timing <run-dir>` | Per-step phase breakdown, accounted-vs-latency, phase totals; steps at ≥2× median latency get an outlier flag. |
| `grep <run-dir> <pattern> [--type T]* [--step N] [--module M] [--level L]` | Escape hatch: regex over raw records with structured filters, when no canned query fits. |
| `replay <run-dir> --step <n> [--section sid]` | Regenerate tier-2 detail: load the step's `dom_snapshot` blob and run it through pure core (`DomSnapshot.from_dict` → `build_page` → outline, or one section's markdown with `--section`). |

## Replay notes

- Replay uses **default** `ObserveConfig`, not the inspecting machine's user
  config, so output is stable across machines.
- It requires a real DomSnapshot capture payload (the runner stores one per
  step; `python -m ebrowse.dev <url> capture` produces one by hand). The
  committed sample trace uses stub blobs, so replay on it prints a
  "blob is not a DomSnapshot payload" error naming those recovery paths.
- Refs minted during replay come from a fresh `RefRegistry`; they match what
  the agent saw only for the first observe of a session (later steps re-mint
  from scratch — use `trace-ref` for the historical ref lifecycle).

Exit codes: 0 ok, 1 miss/empty result (missing step/ref/section, no grep
matches), 2 unusable input (bad run dir, bad regex, stub blob).
