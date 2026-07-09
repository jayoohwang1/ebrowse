# 0008 — explicit outline + synchronous visual glance (`◉`, `describe-screen`)

Status: accepted (2026-07-08). Refines two v1 defaults after live testing with a
local vision model (Qwen3.6-35B, llama.cpp on one RTX 3090): navigation
auto-printed the outline, and section summaries were async-backfilled.

## Context

We wanted a cheap *visual* tier on the observation ladder: a one-line VLM gist of
the screenshot (`◉`) so the agent can tell whether a page is worth a full
`screenshot` (~2.4k image tokens to the main agent) — and, crucially, catch
states the DOM outline can't convey (a country-picker interstitial, a modal
covering the content, a mostly-visual page). The gist text costs the main agent
~40–120 tokens; the ~1.6k image tokens are absorbed by the local sidecar. Live
tests confirmed the gist is grounded when the prompt hard-constrains it to "only
what is visible" (VLMs otherwise drift into *typical* page furniture).

Two existing decisions collided with making this a good default:

1. **The sidecar was "never on the critical path"** — summaries backfilled
   asynchronously and appeared on the *next* outline. A visual gist generated the
   same way would lag a render behind, and the two-phase "backfill running… rerun"
   dance is worse UX than a complete first render. But making enrichment
   synchronous puts an LLM call (seconds) on every outline.

2. **Navigation auto-printed the outline.** Once the outline runs the summarizer
   synchronously, auto-firing it on *every* `open`/nav/nav-action would block each
   navigation on the sidecar — even when the agent only wanted to screenshot or
   act on a durable ref.

The principle at stake is **1 (never load-bearing)**, which is about
*correctness*, not latency. "Never on the critical path" was a latency stance. A
synchronous call with a hard timeout that degrades to deterministic output keeps
correctness intact while accepting bounded latency — and on a local sidecar the
latency is a few seconds, dwarfed by a reasoning agent's own think time.

## Decision

- **Synchronous enrichment on `outline` only.** `outline` fills `≈` section
  summaries and the `◉` glance before returning, running them concurrently under
  `summarizer.sync_timeout_s` (default 30s). On timeout/failure it degrades to
  deterministic labels + no `◉` line with a status note; genuine timeouts count
  toward the circuit breaker. No async backfill; `--wait-summaries` removed.
- **Navigation returns a landing line, not the page.** `open`/`back`/`forward`/
  `reload`/`tab` and navigating actions return `opened <url> · "title"` (or
  `… → navigation … now at <url>`) + a `run 'ebrowse outline'` hint. This makes
  the now-heavy outline explicitly opt-in, gives the page wall-clock time to
  settle before it's read (fewer gists of half-loaded pages), and matches sibling
  tools (claude-in-chrome, agent-browser split navigate from observe). The page
  is still rebuilt internally on navigation, so **durable `@refs` stay live** —
  an agent can act on known persistent-chrome refs without re-outlining.
- **`◉` default-on when a vision sidecar is reachable** (`summarizer.glance`,
  default true), cached per page-structure key (`screens` table) so revisits are
  instant. A distinct provenance marker `◉` — weaker than `≈` — keeps the trust
  boundary explicit (principle 1): it's a routing signal, never data to act on.
- **`describe-screen [prompt]`** is the patient, agent-initiated path: a free-form
  visual query with its own generous `describe_max_tokens` (4096) and
  `describe_timeout_s` (180s), given a longer daemon/transport ceiling than
  page-touching verbs. No prompt → the cached default gist; a prompt → anything.
- **Appeared `dialog` sections expand inline in the diff** (deterministic DOM,
  not a VLM guess) — the one action shape where the auto-glance can't fire (a JS
  modal via partial change) is exactly where the real markup beats a gist.

## Consequences

- First read of each new page costs one synchronous enrichment (seconds on a
  local model, timeout-bounded); revisits and cache hits are instant. Rapidly
  clicking through pages pays nothing until you `outline`.
- One extra round trip in the common navigate-then-read flow, traded for opt-in
  latency, a natural loading buffer, and ecosystem-consistent behavior.
- Cost note (principle 2): the `◉` *output* is the one visual cost the main agent
  pays, per outline — so the shipped gist prompt stays terse; `describe-screen` is
  where verbosity is a deliberate, agent-chosen expense. The ~1.6k image tokens
  are free to the main agent but real money on a *paid* multimodal API — hence the
  `glance = false` escape hatch, documented in docs/configuration.md.
- Contract churn: navigation/action result and outline formats changed (frozen
  interfaces, updated with goldens per principle 4). The now-unused
  `navigation_diff` / `Diff(kind="navigation")` machinery is retained as a valid,
  tested pure utility the renderer still handles.
