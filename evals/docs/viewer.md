# Trace viewer (`ebrowse-eval serve`)

The normal interface is a central local application that recursively discovers
trace runs and organizes them by their directory under a runs root:

```bash
uv run ebrowse-eval serve runs --open
# custom location/port:
uv run ebrowse-eval serve runs/online-mind2web --port 9000
```

The index is generated dynamically, so newly completed or currently running
traces appear on refresh. Each row identifies its run by the **task cell**: the
instruction the agent was given on the first line, then the site it started on
and the run-name part of the run id (the task id, which the instruction already
implies, is stripped). The site comes from the run's navigation bootstrap when
present, else the task url, else the first step's URL. Next to the outcome, the
**summary** column shows the annotation pipeline's one-line VERDICT for the run
(`ebrowse-eval annotate`); runs without annotations show a dash. The remaining
columns are model, step/anomaly counts, and last update time. Opening a trace
renders the full conversation and provides a link back to the index.

Legacy step-only traces retain the first-25/last-10 optimization.
For those traces, the server renders the first 25 and last 10 steps in full.
Middle steps remain visible as compact one-line summaries of page, agent
thought, and action. They are grouped in sets of 10; **Expand steps …** fetches
that group's full two-lane rows without reloading the page. Screenshots are
lazy-loaded from the content-addressed blob store, and DomSnapshot JSON is not
fetched until its internals expander is opened. Standalone exports remain fully
self-contained and render every step.

Runs can be selected individually, or all runs in a directory can be selected
from its heading checkbox. **Move selected to trash** asks for confirmation and
uses the system `trash-put` command; it never permanently deletes a run. The
server revalidates that every submitted directory is a trace beneath the
configured runs root before moving anything.

## Standalone export

`ebrowse-eval view` remains available to export one self-contained HTML file — all CSS/JS inline,
screenshots embedded as data URIs, no CDN — so a trace can be opened anywhere,
attached to an issue, or diffed by eye against another run.

```bash
uv run ebrowse-eval view runs/my-run              # writes runs/my-run/trace.html
uv run ebrowse-eval view runs/my-run -o /tmp/t.html --open
```

## Trace header

The header leads with what identifies a run to a human: the **site** and
outcome on one line, then the **instruction** as the page title. The task id is
a hash, so it drops into a collapsed `run details` expander alongside the run
id, benchmark, agent, git sha and resolved config. Totals and the eval result
stay visible. The **anomaly list** is collapsed — a run can carry dozens, and
they buried everything else — but still links each entry to its step.

## Segmented overview

When a run carries model annotations (`ebrowse-eval annotate`), opening it
leads with an **overview**: the one-line verdict, then the trajectory cut into
collapsed segments. Every annotation edge is a cut, so overlapping spans (a
vision finding straddling two issues) split rather than merge and each segment
lists every finding that overlaps it — category/severity badges plus the
model's sentence. A span states itself once and thins to `continues` on the
later segments it runs through. The stretches nobody annotated stay as
`no findings` segments, so the run is still covered end to end.

Opening a segment reveals that step range's full detail rows — the same
conversation/step rendering as always, just gated. Screenshots inside a
collapsed segment are never fetched, which is what keeps a 136-step run
openable. **Expand all** / **Collapse all** sit in the overview, and following
an anchor (an anomaly's `#step-N` link) opens whatever segments enclose it.

Unannotated runs are not segmented — they render as a plain vertical log, as
before. Plain (kind-less) step-range summaries keep their inline markers;
annotation records (`verdict`/`issue`/`stuck_span`/`vision`) appear in the
overview instead of duplicating there.

## Conversation layout

New traces preserve every finalized Pi message and its original content-block
order. The exact starting prompt is the first visible user row, in full but
height-capped and scrolling — it embeds the whole operating guide and would
otherwise push the trajectory off screen. Pi's
effective system prompt is shown above it behind a collapsed expander and is
omitted when capture was unavailable. Thinking, ordinary assistant text, every
tool call, every tool result, and final assistant-only answers are retained;
non-ebrowse tools no longer disappear from the browser-oriented view.

Rows are grouped by **page**: consecutive rows whose steps captured a
byte-identical screenshot (the blob store is content-addressed, so an equal ref
is an unchanged page) share one two-column group — conversation on the left,
a single browser panel on the right that sticks while you scroll the chain of
calls made against that page. The next page-changing action opens a visibly
separate group. A chain used to reserve a full screenshot's worth of height per
row and repeat the same image, which is what left the big gaps between calls.

The panel shows the group's last screenshot, URL/title, anomaly badges pooled
across its steps, and one `browser details` expander holding every step's
timings, browser state, blob refs, events, logs and errors, labelled per step.
A run of rows with no browser step at all (non-browser tools, the final answer)
renders full width instead of reserving an empty lane. Large transcript blocks
and browser blobs remain content-addressed and lazy-loaded.

Grouping is deliberately strict: identical bytes only. A page with a carousel,
animation or rotating ad re-captures differently on every step and therefore
does not group — the viewer never claims two different-looking screenshots are
the same page. Switching to a looser key (URL + title) is a one-line change in
`_page_key`.

## Legacy layout

A vertical log of the trajectory, one row per step, **two lanes**, remains the
fallback for traces created before conversation records were added:

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
