# Architecture

`ebrowse` is a daemon-backed browser-control CLI (Python + async Playwright) that any
agent harness can drive — via Bash, or through the built-in MCP server. It adapts the
reusable ideas from **WebChallenger** (PageMem sectioning, diff-based observation,
deterministic page-structure analysis) into a general-purpose tool, fixing the dominant
token pathologies of browser tools: full-page snapshots per step, no change
observation, one round trip per micro-interaction, and session amnesia.

## Design principles (binding)

1. **Determinism first; LLM as optional enhancement.** Every feature works with no LLM
   configured. The summarizer sidecar only ever produces *navigational hints* (section
   labels, image captions); everything the host agent acts on — refs, element states,
   expanded content, diffs — is ground truth derived from the DOM. An LLM failure
   degrades label quality, never correctness. See [ADR 0001](adr/0001-llm-sidecar-never-load-bearing.md).
2. **Token economy is a feature.** Every line of default output earns its place. All
   output formats have golden tests so token regressions are visible diffs.
3. **Core is pure.** The `core/` package operates on plain data (`DomSnapshot` in,
   `PageMem`/`Diff`/rendered text out) with no Playwright, daemon, or network
   dependency. All page inspection happens in a single injected-JS pass that returns
   JSON — browser round trips are O(1) per observation, never O(elements). Playwright
   is allowed only in `core/snapshot.py` (the evaluate wrapper), `core/locate.py`, and
   screenshot clipping.
4. **The data model is the stable interface.** Modules communicate through `model.py`
   dataclasses and the renderers. Extend by adding optional fields; never repurpose
   existing ones. `model.py` and the [output contracts](output-contracts.md) are FROZEN.
5. **No site-specific logic.** Heuristics must be justified by DOM/ARIA semantics or
   generic visual structure, never by a particular website.
6. **Sensible defaults, escape hatches.** Default behavior needs zero flags; complexity
   is opt-in.
7. **Every change ends with a working tool.** `make lint typecheck test` green, prior
   verbs intact.
8. **Fail loud and actionable.** Every error states what failed and the next command
   the agent should try (stale ref → "re-run `ebrowse outline`").

## Component overview

```
┌────────────┐  argv    ┌──────────────────────── daemon (asyncio) ───────────────────────┐
│ ebrowse CLI├─────────▶│ unix-socket newline-JSON (daemon/protocol.py)                    │
│ (thin)     │◀─────────│   └─ Daemon ── Session("default"), Session("other"), …           │
└────────────┘  text    │        ├─ browser: Playwright launch (persistent ctx) │ CDP      │
┌────────────┐          │        ├─ observation state: PageMem, RefRegistry, raw sections  │
│ ebrowse mcp├─────────▶│        ├─ actions.py / compound.py: act → quiesce → diff         │
│ (stdio MCP)│          │        └─ summarize/: sync section labels + visual glance, captions│
└────────────┘          │  core/ (pure): discover.js → DomSnapshot → split/label/          │
                        │                fingerprint/diff/render                           │
                        └──────────────────────────────────────────────────────────────────┘
```

- **CLI** (`cli/`): parses argv, autostarts the daemon if absent, sends one request,
  prints the response. No page logic.
- **Daemon** (`daemon/`): one process per user, owns Playwright, serves N named
  sessions. Commands within a session execute serially (per-session asyncio lock);
  different sessions run concurrently. Idle shutdown (default 30 min).
- **Session** (`session.py` + `actions.py` + `compound.py`): one browser context + one
  active page + observation state; verb implementations.
- **core/**: pure functions — the only code that understands page *structure*.
- **summarize/**: optional client for an OpenAI-compatible endpoint. Runs only on the
  `outline` verb (section labels + `◉` visual glance), synchronously but under a hard
  timeout with a circuit breaker — never load-bearing: on failure the outline is
  deterministic. Navigation and actions never call it. See [ADR 0008](adr/0008-explicit-outline-and-synchronous-visual-glance.md).
- **MCP server** (`mcp.py`): stdio JSON-RPC, seven tools, thin client to the same daemon
  (CLI and MCP share browser state). See [ADR 0005](adr/0005-mcp-server-without-sdk.md).

## Observation flow (the heart of the tool)

```
ebrowse open <url>                      # navigation: land, don't render the page
  → goto + settle → _observe_page()      # rebuild PageMem (durable refs) — NO summarizer
  → landing line ("opened <url> · title — run 'ebrowse outline'")

ebrowse outline                         # the only verb that reads + summarizes
  → page.evaluate(discover.js)          # ONE round trip: full DOM walk in-page
  → DomSnapshot (JSON tree)             # nodes: tag/attrs/text/bbox/clickable signals
  → split(DomSnapshot) → [RawSection]   # WebChallenger DividePage adaptation
  → extract elements, assign refs (RefRegistry), fingerprint, label
  → (sync, if summarizer enabled) summarize.batch + caption_screen → sqlite cache
                                        # text labels + ◉ glance, concurrent, timeout-bounded
  → render_outline(PageMem) → stdout
```

```
ebrowse click @e12
  → resolve @e12 → ElementDesc → locator chain → occlusion pre-check → click
  → quiesce (MutationObserver debounce, capped)
  → re-observe → diff(prev, new) → render_diff → stdout   # NOT a full snapshot
```

Compound verbs (`select` on custom dropdowns, `fill-form`, `search`) are deterministic
state machines over the same machinery: N internal steps, ONE final diff. Ambiguity or
no-match degrades to an actionable error listing the choices — never a silent guess.

## Package layout

```
src/ebrowse/
  model.py            # frozen dataclasses (see output-contracts.md)
  config.py           # TOML + env config (see configuration.md)
  errors.py           # CommandError with agent-facing message + exit code
  core/
    js/discover.js    # single-pass DOM walker (the only page-side code)
    snapshot.py       # DomSnapshot types + the evaluate() wrapper
    split.py          # DomSnapshot → [RawSection]
    clickable.py      # interactable predicate (canonical sets, templated into JS)
    label.py          # deterministic heading/preview labels
    fingerprint.py    # section fingerprints, class normalization, RefRegistry
    pipeline.py       # build_page(): split → extract → refs → label → fingerprint
    diff.py           # PageMem × PageMem → Diff
    render.py         # outline / expand / diff / query renderers (FROZEN formats)
    locate.py         # ElementDesc → Playwright locator chain
  summarize/          # client.py (breaker), batch.py (one call/page), cache.py (sqlite)
  session.py          # Session: browser lifecycle, observation, misc verbs
  actions.py          # ActionsMixin: atomic action verbs → diff
  compound.py         # CompoundMixin: select machine, fill-form, search
  daemon/             # protocol.py (newline JSON), server.py (asyncio unix socket)
  cli/                # main.py (argparse), client.py (dispatch), doctor.py
  mcp.py              # stdio MCP server
  dev.py              # daemonless harness: outline/expand/capture/stats on a URL
tests/
  fixtures/pages/     # fixture site (list/table/huge.html generated by generate.py)
  fixtures/domsnapshots/  # captured DomSnapshots for pure-core tests
  golden/             # pinned outline/expand renderings
scripts/smoke_real_sites.py  # manual real-site outline quality check
```

## Accepted risks & tradeoffs

| Risk | Stance |
|---|---|
| SPA quiescence heuristics fire early/late | MutationObserver debounce is best-effort; capped, with an honest `no change detected` caveat. Effects animated past the window attribute to the next diff (observed, accepted). |
| Section splitter quality on wild pages | The most quality-sensitive code. Golden fixtures + real-site smoke script; `max_sections` overflow valve. The failure mode of generic heuristics is "everything collapses into one section" — test on css-in-js sites. |
| Descriptor matching too strict/loose | Strict by design; misbinding is worse than ref churn. See [ADR 0003](adr/0003-strict-ref-matching.md). |
| Cross-origin iframes invisible | Surfaced in the outline so the agent knows to screenshot. CDP route is future work. |
| Closed shadow DOM | Out of scope; open shadow roots are walked. |
| CDP-attach with a human using the browser | Commands serialized per session; diffs make external changes visible rather than corrupting state. |
| Summary injection (page text/pixels → model → label) | Provenance markers (`≈` text summary, `◉` visual gist, `|` verbatim), length clamp, refs stripped from model output; structure is never model-controlled. |

## Provenance

Core algorithms (section splitting, clickable predicate, element diffing, locator
chains, class normalization) are adaptations of WebChallenger's DividePage /
UpdatePageMem / is_clickable. Ideas were ported, not code; the original is a
sync-Playwright monolith. Verb naming aligns with the `agent-browser` CLI (vendored
at `./agent-browser/` as read-only reference material, excluded from packaging).
