# Inspection queries

Canned, entity-centric queries over a trace run directory so an LLM (or human)
can root-cause a run without reading the whole trace. Every command takes a
`<run-dir>` and prints concise deterministic plain text (a few lines,
golden-tested); add `--json` for structured output. Misses are graceful and
name what *does* exist ("no events for @e9; refs seen in this trace: @e1, @e2").

Suggested workflow: `annotate` a finished run (or batch) once, then triage from
`issues`/`overview` → drill down with `step`/`trace-ref` → `replay` when you
need tier-2 detail the trace doesn't store. On an un-annotated run, start at
`overview` and lean on `anomalies`/`errors` instead.

## Query catalog

| command | question it answers |
|---|---|
| `overview <run-dir>` | Where do I start? Run meta one-liner, outcome/totals, per-step table (exit, latency, `A`/`E` badge for anomaly/error, command, url), post-hoc summaries. |
| `issues <run-dir>` | What did the annotator find? The run verdict, cited issue spans (`category`/`severity`), stuck spans, and vision discrepancies — each printed with the drill-down command to verify it. Empty until the run is `annotate`d. |
| `anomalies <run-dir>` | What surprised the pipeline? One line per anomaly: step, kind, message. |
| `errors <run-dir>` | Which steps failed, what recovery did the error name, and did the agent's NEXT command follow it? (`[followed -> …]` / `[ignored -> …]` / `[last step]`) |
| `step <run-dir> <n> [--full] [--debug]` | Everything for one step: command, agent text, exit/latency/tokens, timing phases, browser state, blob refs, output (truncated; `--full` for verbatim), browser events, ebrowse logs (debug level hidden unless `--debug`), anomalies. |
| `trace-ref <run-dir> <@eN>` | History of one element ref, in step order: every log/anomaly whose structured fields name it, plus every step command/output line mentioning it. |
| `trace-section <run-dir> <sN>` | Same for a section id (word-boundary match — `s2` never matches `s12`). |
| `timing <run-dir>` | Per-step phase breakdown, accounted-vs-latency, phase totals; steps at ≥2× median latency get an outlier flag. |
| `grep <run-dir> <pattern> [--type T]* [--step N] [--module M] [--level L]` | Escape hatch: regex over raw records with structured filters, when no canned query fits. |
| `replay <run-dir> --step <n> [--section sid]` | Regenerate tier-2 detail: load the step's `dom_snapshot` blob and run it through pure core (`DomSnapshot.from_dict` → `build_page` → outline, or one section's markdown with `--section`). |

## LLM annotation (`annotate` → `issues`)

`ebrowse-eval annotate <run-dir>...` runs a cheap local model over a finished
run and writes its findings back as `summary` records (schema principle 1:
labels only, never load-bearing — a trace is fully usable un-annotated). It is
the bridge from raw traces to a triage list a frontier model can act on
cheaply: the local model burns free tokens turning the trajectory into cited
claims; a reader then spends expensive context only on the claims plus the few
ground-truth slices they point at. Implementation: `ebrowse_evals.annotate`.

Two passes:

1. **Text pass** — the whole trajectory (task + each step's agent text,
   command, output) in one prompt → a one-line `verdict`, per-incident `issue`
   spans (`category` ∈ tool_bug / agent_confusion / site_behavior /
   inefficiency; `severity` high/low), and `stuck_span`s where the agent looped
   without progress. One incident = one contiguous step range.
2. **Vision pass** — for each stuck span and high-severity issue span (merged,
   capped), the span's screenshot blob + the outline text the agent actually
   saw + the goal → anything visible but missing/mislabeled in the text view.
   `ADEQUATE` replies write nothing. This audits the outline's token-economy
   trade-off: what the render dropped that mattered.

Design notes (see `evals/src/ebrowse_evals/annotate.py`):

- **Cited spans, executable drill-downs.** Every annotation carries a step
  range; the `issues` lens prints each with the `ebrowse-eval step` command to
  verify it. A claim whose cited steps don't show the problem is then visible
  immediately — keep the cheap model honest by checking, don't trust.
- **Windowing.** Trajectories over the annotator's context budget
  (`--context-limit`, tokens) are split into overlapping step windows; each is
  annotated, then a merge call consolidates duplicate/continuing incidents
  (with a mechanical fallback if the merge is unparseable). Most runs fit in one
  pass — the token efficiency of the tool keeps even long runs small.
- **Reproducibility / model.** Fixed temperature + seed; thinking disabled (it
  otherwise buries the answer in `reasoning_content`). The model defaults to the
  run's own agent model from `run_meta`; override with `--model` and point at
  any OpenAI-compatible endpoint with `--api-base`.
- **Idempotent.** Re-annotating skips a run that already has annotations unless
  `--force`, which strips the prior `summary` records first (the one sanctioned
  rewrite of `events.jsonl`). `--no-vision` skips the screenshot pass.
- **Batch.** `annotate` accepts many run dirs; chain it after `run` to wake up
  to an annotated batch, then read verdicts across runs with `issues`/`grep`.

The annotator is a triage lens, not ground truth: its free-text verdicts can
editorialize or err, while the structured issue/stuck-span spans hold up better
under drill-down. Always verify a claim against the cited steps before acting.

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
