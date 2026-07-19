# Trace viewer (`ebrowse-eval view`)

Renders a run directory into one self-contained HTML file — all CSS/JS inline,
screenshots embedded as data URIs, no CDN — so a trace can be opened anywhere,
attached to an issue, or diffed by eye against another run.

```bash
uv run ebrowse-eval view runs/my-run              # writes runs/my-run/trace.html
uv run ebrowse-eval view runs/my-run -o /tmp/t.html --open
```

## Layout

A vertical log of the trajectory, one row per step, **two lanes**:

- **Right lane — what the agent saw.** Assistant text for the turn, the exact
  command, tool output verbatim (monospace, whitespace preserved), tokens and
  latency, non-zero exit codes.
- **Left lane — ground truth + internals.** Always the step screenshot
  thumbnail — the filmstrip that lets a human skim which page the agent was on
  across the whole trajectory, rendered even when the agent never looked at
  it — plus URL/title, a per-phase timing bar, and anomaly badges. An
  **internals** expander (collapsed by default) reveals: browser events since
  the last step (console/network/navigation/dialog), the step's `ebrowse_log`
  records grouped by module (debug level hidden unless the header toggle is
  on), browser state, blob refs with the DomSnapshot JSON inlined, structured
  error/recovery, and any unknown record types as raw JSON.

The **header** carries run metadata (task, prompt, agent/model, git sha, mode;
resolved config behind an expander), the outcome and totals from `run_end`,
and the run's **anomaly list** as the triage layer — each entry links to its
step. `summary` records appear as range markers in the flow after their
`step_end` row.

Philosophy: track everything, hide verbosity behind expansion. The collapsed
view stays skimmable: filmstrip + commands + badges.

## Degraded traces

The viewer never crashes on an imperfect run: missing/absent screenshot blobs
and non-image blobs render as placeholder boxes showing the ref, a torn final
`events.jsonl` line is skipped, unknown record types/fields render as raw
JSON, and a missing `run_end` shows "in progress / no run_end". Inlined
DomSnapshot JSON is truncated at 200 kB to keep the page openable.
