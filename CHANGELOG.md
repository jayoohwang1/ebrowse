# Changelog

All notable changes to ebrowse. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [SemVer](https://semver.org/). Unimplemented plans live in
[GitHub issues](https://github.com/jayoohwang1/ebrowse/issues), not here.

## [Unreleased]

### Added

- Redirect-aware eval startup discovers bounded regional task redirects before the
  agent runs, freezes the resulting domain scope, and records the chain in trace
  metadata and `navigation-bootstrap.json`.

- Pi/ebrowse evals now expose one shell-free custom browser tool instead of
  Bash. A configurable verb/argument/output/timeout policy blocks `eval`, file
  upload, process control, CDP attachment, session overrides, and caller-chosen
  paths by default; task-host navigation is enforced for explicit opens,
  clicks, redirects, and popups, with structured policy errors retained in traces.
- Eval traces now preserve the exact starting prompt, Pi's effective system
  prompt, and every finalized conversation message/content block. The viewer is
  conversation-first with a fixed browser-state side lane on every row and
  retains the legacy step renderer for older runs.
- Eval tasks now start on their declared target URL, stop at a configurable
  200 tool calls by default, and support isolated parallel execution with
  `ebrowse-eval run --jobs N`.
- Added `ebrowse-eval serve`, a central local trace application that discovers
  runs recursively, groups them by directory, opens traces dynamically, and
  moves selected runs or whole directory groups to the system trash.
- Central trace pages now render the first 25 and last 10 steps in full, show
  compact middle steps expandable in groups of 10, and lazy-load screenshot
  and DomSnapshot blobs. Standalone exports remain self-contained.
- The trace server now treats browser-cancelled responses as normal disconnects
  instead of printing `BrokenPipeError` worker tracebacks.
- Pi raw-event capture now filters cumulative `message_update` snapshots and
  has a 64 MiB ceiling without compromising timeout/tool-limit recovery.

- **`evals/` workspace package (`ebrowse-evals`)** — foundation of the evaluation
  harness. Trace schema v1 (typed JSONL records + content-addressed blob store,
  forward-compatible by construction; see `evals/docs/trace-schema.md`), the
  task/benchmark model with optional per-task `eval.py` evaluators receiving the
  full trace (`evals/docs/tasks.md`), an `ebrowse-eval` CLI (`validate`, `tasks`),
  and a committed generated sample trace for building viewers/inspection tools
  against. Runner/capture/viewer land next; `experiments/` is unchanged until parity.
- **`ebrowse-eval run` — the eval runner.** Ports `experiments/run-agent.sh` into
  the package: task selection (`--task` globs / `--tag` / `--sample --seed`),
  config layering (harness defaults → benchmark → task → CLI flags) persisted
  fully resolved into `run_meta` alongside git sha/dirty + ebrowse version/mode,
  a generic `AgentHarness` protocol with a `PiHarness` implementation (tool-guide
  prepending, `experiments/.env` defaults, JSON event + pi session capture,
  isolated per-run workdir, fixed worktree executable with daemon stop), and one
  Step trace record per agent tool-call with a `StepCapture` hook for the
  browser-state capture layer. Per-task `eval.py` (or declarative `expected`)
  results land in `run_end`.
- **Per-step capture layer (`ebrowse_evals.capture`)** — `StepCapture` fills each
  Step record's browser state, screenshot, and DomSnapshot blobs and appends
  `browser_event` records (console, failed requests, navigations, dialogs)
  unconditionally after every agent tool-call, degrading to a partial step plus an
  `anomaly` record on any failure (capture never breaks a run). Backed by a new
  additive daemon verb `debug-capture` that reuses the session's own snapshot
  machinery — including the snapshot already taken for the previous verb's
  observation when no possibly-mutating verb ran since.
- **`ebrowse-eval view <run-dir>`** — human trace viewer rendering a run into one
  self-contained HTML file (assets inlined, no CDN). Two-lane step log: right lane
  is what the agent saw (command, verbatim output, tokens/latency); left lane is
  ground truth + internals (screenshot filmstrip, URL/title, timing bar, anomaly
  badges, with browser events / ebrowse_log / browser state / DomSnapshot JSON
  behind a per-step expander). Header carries run metadata, outcome/totals, and
  the anomaly list linking to steps; degrades gracefully on missing blobs, torn
  tails, and unknown record types. See `evals/docs/viewer.md`.
- **`ebrowse-eval` inspection queries** — canned entity-centric queries over a
  trace run directory (`overview`, `anomalies`, `errors` with recovery-hint
  followed/ignored joins, `step`, `trace-ref`, `trace-section`, `timing`,
  `grep`, and `replay --step N` regenerating tier-2 detail by running the
  stored DomSnapshot blob through pure core). Concise deterministic plain text,
  `--json` everywhere; documented in `evals/docs/inspect.md`.
- **Phase 2 wiring: capture + debug log flow into `ebrowse-eval run` traces.**
  The trusted browser-tool launcher instruments every allowed call (on by default for
  ebrowse runs; `--no-capture` to disable): per-call `EBROWSE_REQUEST_ID=call-<n>`,
  a synchronous post-call debug-capture spool, and the daemon debug log enabled
  via env. `ebrowse_evals.ingest` joins both back to steps ordinally after the
  run — capture fields onto each ebrowse step, daemon events as `ebrowse_log`
  records, anomaly promotion, and phase timings rolled into `step.timing`.
- **Structured debug-event channel (tier 1).** `EBROWSE_DEBUG_LOG=<path>` /
  config `[debug] log` streams per-request JSONL events (`{request_id, module,
  event, level, fields, ts, mono}`): phase timings, snapshot/ref/diff/locate
  facts, and anomaly events (`ref_rebound`, `ref_gone`, `snapshot_truncated`,
  `element_moved`, `wait_timeout`, `section_reshaped`). Off by default — zero
  overhead, no file, byte-identical output. `EBROWSE_REQUEST_ID` lets a harness
  set the request id joined across CLI response and events (ADR 0013).
- **Opt-in accessibility-tree expansion.** `ebrowse expand <target> --ax` renders
  an actionable, deterministic accessibility-tree outline with inline durable refs.
- **Browse every option of a large `<select>`.** `expand @ref` on a native
  select lists its options, 50 per page with the usual cursor hints
  (`… 300 more options — expand @e5 --cursor 50`; `--all` dumps the captured
  list). The capture cap rose 50 → 350 options, covering country pickers and
  country+state combos; only timezone-class monsters truncate, and there the
  tail is honestly absent with the escape hatch named (`'ebrowse select @e5
  "<label>"' still matches any option by its text` — selection always matches
  the live DOM). Closes #10.

### Changed

- **Section splitting is now lossless and expansion-budgeted.** Oversized semantic
  containers are partitioned without dropping direct text or wrapper actions;
  nested lists/tables are promoted into pageable, queryable sections while form
  residuals remain in order. `observe.max_section_tokens` defaults to 16,384.
  `max_sections` is now a soft target using only safe adjacent merges, so it can no
  longer recreate a giant tail section. Native multi-`tbody` tables and ARIA
  list/table/grid collections share the same classification/query adapter.
  Small same-owner content/form fragments now coalesce across moderately taller
  layouts, standalone headings attach to compatible following content, and a final
  substance gate removes empty layout projections.
- Outline token estimates for collections now report the default paginated expansion
  cost rather than `--all`; default expand/query windows also stop at the configured
  token budget. Explicit `--all` / query `--limit` retain the large-output escape
  hatches. Outlines now warn when DOM capture hit its 15,000-node cap.

- **Diff new-text extraction: status messages no longer lose to bulk content**
  (#11). `added_text()` now ranks short fragments (≤ 100 chars — status
  messages, validation errors) ahead of bulk insertions, caps each fragment so
  one long insertion can't consume the budget, elides over-cap fragments as
  `start … end` instead of a bare prefix, pads replaced words with one word of
  context per side (`20` → `30` quotes as `Showing 30 results.`), and raises
  the overall budget from 160 to 500 chars. Sections the agent has `expand`ed
  on the current page get an 8000-char budget — verbose text diffs where the
  agent is actively reading.

### Fixed

- **Long-lived tab/input lifecycle guardrails** (#9). New tabs are foregrounded
  when adopted; when the active popup/tab closes, the session immediately
  falls back to its most recently active live tab instead of retaining a dead
  Playwright page. Page event wiring is idempotent. `hover` also verifies the
  live target acquired `:hover`; a successful dispatch with neither `:hover`
  nor a DOM change now warns that browser input delivery may be degraded and
  names `ebrowse daemon stop` as the recovery action.

- **Refs verified against their descriptor before acting** (#12). When a ref
  resolves through disambiguation (`nth_hint` among identical descriptors, or
  any candidate after a mismatch was seen), the live element's identity facts
  (tag; id/testid when recorded; text head, leniently) are checked with one
  evaluate before the action dispatches. A page that reordered
  descriptor-identical siblings between observation and action (item removed,
  list re-sorted) now refuses with `stale ref @eN: … now resolves to a
  different element … — run 'ebrowse outline'` (exit 2) instead of silently
  acting on the wrong sibling (refuse > misbind, ADR 0003). The mismatch
  check also recovers via later locator candidates — buttons sharing a
  captured name but differing in text used to all resolve to the first
  sibling; they now resolve correctly. Unique untainted matches skip the
  check (zero happy-path cost). Fixture: `reorder_cart.html`.

- **Wrong-element resolution for links with repeated hrefs.** The locator
  chain tried `a[href$="…"]` before any text-based candidate and disambiguated
  multiple matches with `nth_hint` — but nth_hint counts identical
  *descriptors*, not href matches, so on a page where several links share an
  href (`"#"`, `/cart`), acting on `@ref (link "Products")` could silently hit
  the FIRST such link (e.g. "Home") while reporting the right name. The
  role+text candidate now precedes href, and href candidates are filtered by
  the link text when one exists. Surfaced by the hover verb's e2e test.
- **Full-page clickable overlays are no longer invisible to the outline.** The
  splitter never treated an oversized node as terminal, so a full-viewport
  overlay with no element children (cookie veil / interstitial wired via
  `onclick`, holding only a text node) vanished during descent — no section,
  no ref, and a click it blocked could only report "no exposed ref". Oversized
  childless nodes are now terminal; the substance gate still drops bare
  decorative backdrops. Blocked clicks can now name the veil's own ref as the
  recovery action.
- **Restyled native controls no longer falsely block clicks.** The click
  pre-check treated any unrelated element at the target's center as a cover, so
  Amazon-style radios/checkboxes (transparent native input + decorative sibling
  inside a `<label>`) hard-failed with `covered by <i …>` pointing at an
  unexposed decorative node. A hit inside an associated label is now recognized
  as the control's legitimate click surface (HTML label activation): the click
  is routed through the label and the diff notes
  `clicked via the associated label`. The pre-check is also shadow-DOM-aware now
  (composed-tree containment instead of `.contains()`).
- **Refs inside id-less iframes are now actionable.** Frame identity captured
  for an iframe without `id`/`title` fell back to the frame URL, which locator
  resolution could never match — refs inside such frames (common third-party
  embeds) errored on every action. Capture now records the iframe's `src`
  attribute and resolution matches `iframe[src=…]` as well.

### Added

- **`hover <target>` verb** — reveals hover-only menus (CSS `:hover` and JS
  `mouseenter`); the mouse stays put, so revealed items survive the re-observe
  and appear in the diff with fresh, immediately clickable refs.
- **`drag <source> <to>` verb** — Playwright `drag_to` (real pointer sequence;
  HTML5 draggable and mouse sortables). `draggable="true"` elements now count
  as candidate evidence (`draggable`), so sortable rows get refs to drag.
- **`<select multiple>` support + truncated-select honesty.** `select <t>
  <label>…` accepts several labels for a multi-select (usage error against a
  single-choice select or a custom dropdown); expand marks them `, multiple`
  and joins current selections (`▾ "Cheese, Bacon"`). Selects with more than
  50 options now report the REAL total (`of 80 options`), not the truncated
  list length.
- **Nested scrolling.** `scroll <sid|@ref> down|up [--pages N]` scrolls INSIDE
  the scroll container at/above the target (nearest composed ancestor with
  real overflow), reporting `container div#results scroll y=600/660` with
  at-the-bottom/top edges — the route to virtualized lists, lazy-loading
  panels, and modal bodies that window scrolling can't reach (newly mounted
  rows show in the action diff). Discovery flags real scroll containers
  (`scr` = [scrollTop, max]) and expand headers announce them:
  `(inner scrollable panel: y=0 of 1104px — 'ebrowse scroll s3 down' scrolls
  it)`. A target with no scrollable ancestor is refused with the window-scroll
  alternative named. Fixture: `nested_scroll.html`.
- **Disabled controls are visible, marked, and fast-refused.** Previously a
  disabled control got no ref at all — a grayed-out submit was invisible in
  expand, so an agent couldn't reason about enabling it. Disabled controls
  (own attribute, `aria-disabled`, or fieldset-inherited via `:disabled`) now
  keep their refs with a `disabled` marker; acting on one fails fast naming
  the state instead of burning the 8s Playwright timeout, and the
  `disabled: "true" → "false"` transition shows in the diff when another
  action unlocks them. Elements under `[inert]` are marked ` inert` in
  expand. Weak-evidence candidates remain gated on enabled. Fixture:
  `disabled_states.html`.
- **Outcome evidence beyond the DOM diff.** Tracked element state now includes
  `pressed` (`aria-pressed`), `selected` (`aria-selected`), and `checked` from
  `aria-checked` on role checkbox/radio/switch widgets (rendered with the same
  `[x]`/`[ ]` marks as native inputs), so state-only toggles/tabs no longer
  diff as "no change". Action results also report non-DOM outcomes as notes:
  `download started: "…"` (page download event), `the document reloaded (same
  URL)` (main-frame navigation counter), and on an otherwise-empty diff, URL
  fragment jumps and scroll movement. Fixture: `outcomes.html`.
- **ARIA checkable widgets work with `check`/`uncheck`.** A non-native element
  with role `checkbox`/`radio`/`switch`/`menuitemcheckbox`/`menuitemradio`
  (Playwright's `set_checked` refuses these) is activated through the full
  interaction plan and its `aria-checked` postcondition is verified; already-
  in-state is a clean no-op, and unchecking a radio is a usage error naming
  the constraint. Works for CSS targets too (live role read).
- **Parent-page covers over iframes are now detected.** The in-frame probes
  cannot see a banner/modal sitting above the target's iframe; a second probe
  (`core/js/cover_above.js`) hit-tests the target's viewport point in the
  parent document. Blocked errors and `diagnose` name the cover — including
  an actionable control INSIDE it (a consent bar's own OK button) as the
  recovery ref, in any frame. Cover-descendant matching also applies to
  main-document covers.
- **`diagnose <target>` verb** — read-only actionability report: Playwright
  trial-click verdict (`actionability: PASS/BLOCKED`) plus the blocker
  classification and recovery step from the failure-diagnosis machinery,
  without dispatching anything (the trial may scroll the target into view).
  Label-decoration hits report PASS since actions route via the label.
  Exposed on CLI, daemon, and MCP.
- **Keyboard-activation fallback for blocked clicks.** When a plain click on
  a natively focusable control (link, button, summary, checkbox/radio) is
  pointer-blocked by a NON-modal cover, the click completes as trusted
  focus + Enter/Space — what a keyboard user does when an overlay doesn't
  trap focus. Fails closed: never used when a dialog/aria-modal/inert
  context is detected, and the focus must verifiably land on the target
  (traps refuse it). Disclosed in the diff:
  `note: pointer route blocked by …; activated via keyboard`. Custom widgets
  (cursor-pointer divs) are never keyboard-guessed.
- **Candidate discovery: weak-evidence custom widgets get expand-only refs.**
  Elements with no strong clickable signal but real interactivity evidence — a
  live pointer listener (found via one CDP `getEventListeners` sweep, chromium
  main frame, single round trip), an explicit `tabindex`, or role-less ARIA
  state — now become `?`-marked refs in `expand`: `[Save changes (@e4 ?)]`.
  ElementState gains an optional `candidate` provenance field
  (`listener`/`focusable`/`aria-state`). Outline counts, default outline
  output, and action policy are unchanged: candidates are excluded from
  counts, and candidate evidence never authorizes proxy activation. A
  candidate containing (or inside) a strong element is suppressed — the
  native control is the real target. Zero-signal decorative nodes still get
  no ref. Fixture: `custom_widgets.html`.
- **Failure-only blocker diagnostics.** When a click is refused (trial-click
  failure, or Playwright "intercepts pointer events" on any pointer verb), one
  diagnostic probe (`core/js/diagnose.js`, exposed via
  `snapshot.probe_blocker`) classifies the blocker and the error names an
  executable next step: the cover's own exposed ref (`dismiss or interact with
  @eN (…) first`), an open dialog (`a dialog is open (…); resolve it first` —
  found even when the hit target is only its anonymous backdrop), or an honest
  limitation (`has no exposed ref (likely a new overlay)` with
  `outline`/`press Escape`/`screenshot` suggestions). Also detects
  disabled-`<fieldset>` inheritance, `pointer-events: none` targets, and inert
  regions. Zero cost on the happy path — the probe only runs after a failure.

### Changed

- **All pointer verbs share one InteractionPlan** (`src/ebrowse/interaction.py`):
  scroll → center-point probe → route (`direct` / `label` / `obstructed`), with
  dialog covers and modal contexts raising immediately. `click`, `check`/
  `uncheck`, and `type`'s focus click all plan the same way; `type` under a
  non-modal cover now skips the focus click entirely (typing focuses without a
  pointer) instead of timing out. Native `fill`/`select_option`/`upload` keep
  their specialized non-pointer APIs. Compound verbs are untouched pending
  their rework.
- **`check`/`uncheck` get the same cover handling as `click`.** The occlusion
  preflight, label routing, and trial-click arbitration now also protect
  `set_checked`: a restyled checkbox/radio whose input center is covered by
  label decoration is toggled via its label — only when the state must change
  (label clicks toggle), with the resulting state verified — instead of timing
  out after 8s with a misleading error. The failure diagnosis (diagnose.js)
  also learned label semantics, so a control's own label decoration is never
  misreported as "no exposed ref (likely a new overlay)" on any verb.
  Compound verbs (`search`, custom `select`, `fill-form`) are intentionally
  untouched pending their rework.
- **Generic covers are arbitrated by Playwright, not the one-point hit test.**
  Only a cover inside a dialog still fails the click pre-emptively. Any other
  center-point mismatch (partial overlays, sticky headers, transient layers,
  odd geometry) runs a short trial click — the same scroll/stability/
  receives-events rules as the real click, with retries — and only a sustained
  interception raises `blocked: … covered by …`. See docs/adr/0009.

- **Navigation no longer prints the outline.** `open`/`back`/`forward`/`reload`/
  `tab` and any navigating action now return a terse landing line
  (`opened <url> · "title"` / `… → navigation … now at <url>`) plus a
  `run 'ebrowse outline'` hint — reading the page is an explicit `outline`. This
  keeps the (now LLM-heavy) outline opt-in, lets the page settle before it's read,
  and matches sibling tools. Durable `@refs` stay live across the jump, so acting
  on known chrome without re-outlining still works. See docs/adr/0008.
- **Section summaries are now synchronous**, filled during `outline` under a hard
  `summarizer.sync_timeout_s` (default 30s) alongside the visual glance (they run
  concurrently), instead of an async background backfill. No more
  `backfill running` status; a slow/dead sidecar — or any error in the
  summarizer/cache stack — degrades to deterministic labels with a status note,
  so enrichment can never fail an `outline`. `outline --wait-summaries` is
  removed (summaries always wait now); `--no-summaries` stays and `--no-glance`
  is added. See docs/adr/0008.

### Added

- **Named modal in blocked-click errors.** A modal that blocks the page without
  geometrically covering the target (native `showModal()` top-layer/`inert`, or an
  `aria-modal` focus trap) can't be pre-empted safely, but the click's occlusion
  pre-check now *records* the open modal (visible `:modal` / `[aria-modal="true"]`
  not containing the target). When the click then fails or no-ops, the message
  names it — `blocked: a modal is open ("…") and is intercepting the click` — so
  an agent stops retrying a dead click. False-positive-free: it only enriches an
  already-failed/no-changed click, never blocks a valid one.
- **`describe-screen [prompt]`** verb: a free-form visual query answered by the
  local vision model over a viewport screenshot, returning TEXT only (`◉`,
  untrusted). No prompt → a concise gist (shared with the outline's `◉` line and
  its cache); a prompt → any visual question, from "is there an overlay?" to
  "transcribe every price" to "describe every detail", bounded by
  `summarizer.describe_max_tokens` (default 4096). The cheap routing tier between
  page text and a full `screenshot`. Exposed as the MCP `browse_describe` tool.
  Config: `summarizer.describe_max_tokens`, `summarizer.describe_timeout_s`.
- **`◉` visual-gist line on the outline** (default on when a vision sidecar is
  configured + reachable, `summarizer.glance = true`): one VLM line under the
  `PAGE` header describing what's visible and flagging overlays/modals/
  interstitials the DOM outline can't convey. Untrusted routing signal, cached
  per page state (`screens` table) so revisits are instant. `outline --no-glance`
  or `summarizer.glance = false` suppresses it. New provenance marker `◉`. See
  docs/adr/0008.
- Appeared in-page **`dialog` sections are expanded inline in the action diff**
  (full `expand` markdown with `@refs`) — a modal is the forced next interaction,
  and it's deterministic DOM, not a guess. Over ~4000t the expansion is compacted
  (all controls kept, prose truncated, `expand sN` for the rest) rather than
  dropped. A modal the splitter **coalesced into a content section** is detected
  from its dialog-scoped added controls: the outcome is reported as `→ dialog`
  and the line tagged `+ sN [dialog]: …`, so it carries the same signal as a
  standalone dialog.
- `dialog` verb (`dialog accept [text]` / `dialog dismiss` / `dialog status`) to
  resolve or inspect a native `confirm`/`prompt` blocking the page. Exposed via the
  MCP `browse_act` tool (`verb=dialog`, `response=accept|dismiss|status`). See
  docs/adr/0007.
- `docs/model-prompting.md`: dated per-model lab notebook for summarizer/vision
  prompting experiments (Qwen reasoning-off findings, screenshot visual-gist
  prompt comparison + token costs). Referenced from AGENTS.md.
- `outline --preview` (opt-in): appends a short verbatim text preview after each
  `≈` summary (`≈ summary  | "preview…"`), keeping both provenance markers, for
  when a section's literal text may answer without an `expand`. Preview width is
  `observe.combined_preview_chars` (default 60); costs ~+50% outline tokens on a
  typical page. Default outline output is unchanged. Also exposed via the MCP
  `browse_outline` tool.
- `summarizer.extra_body`: dict merged verbatim into every `/chat/completions`
  request, so model/provider-specific knobs (e.g. reasoning-off for a
  llama.cpp/Qwen sidecar via `chat_template_kwargs.enable_thinking = false`)
  live as config data rather than provider-branching code. Default `{}`;
  per-provider recipes in docs/configuration.md. On a reasoning sidecar,
  disabling thinking made a real-page outline ~7x faster (29s → 4s) with full
  section coverage instead of partial.
- `ebrowse connect` now probes the CDP endpoint's reachability before use and,
  when nothing is listening, fails with a targeted hint (`start Chrome with
  --remote-debugging-port=<port>`) instead of a generic browser-launch error.
  The probe runs before the existing session is torn down, so a failed
  re-point no longer costs the caller their current browser.
- GitHub Actions CI: lint + typecheck + pure tests on every push/PR, plus a
  browser/e2e job with Playwright chromium.
- pyright type checking (`make typecheck`, basic mode); `ActionsMixin` and
  `CompoundMixin` now declare the typed contract Session must satisfy, so
  the mixin wiring is checker-verified.

### Changed

- Native dialog policy: `confirm`/`prompt` are no longer auto-answered. They are
  left open (blocking the page) for the agent to resolve with the new `dialog` verb;
  the opening action returns `→ dialog opened (blocking)` and page-touching verbs
  fail fast with a recovery hint until it's resolved (or you `tab` away).
  `alert`/`beforeunload` are still auto-accepted and noted. Reverses the v1
  auto-accept-everything policy (docs/adr/0007).

### Fixed

- Summarizer no longer returns `0/N` on reasoning models and large pages: the
  output-token budget now has a floor for reasoning overhead, and the JSON
  parser salvages complete rows from a truncated array instead of dropping the
  whole page on one dangling row.
- `security.allowed_domains` is now enforced on every observed URL, so link
  clicks and redirects that leave the allowed set fail with a recovery hint —
  previously only `open <url>` was checked.

### Removed

- Dead `data` field from the wire protocol's Response (was documented for
  `--json` but never populated).
- Dead `observe.resummarize_element_delta` config key (invalidation is
  structural via content hashes; the key was read by nothing).

## [0.1.0] — 2026-07-05

First working release: the complete v1 design plus the first four roadmap extensions
(compound verbs, query, MCP server, image refs/captions), validated on real sites.

### Added

- **Core page model** (pure, JSON-fixture-testable): single-pass in-page DOM discovery
  (`discover.js`), section splitting with type classification, deterministic labels,
  section fingerprints, durable session-scoped element refs (`@eN`), markdown
  renderers for outline/expand with pagination.
- **Daemon + CLI**: autostarted unix-socket daemon owning Playwright; named sessions
  with persistent browser profiles; CDP attach mode (`connect`); navigation,
  observation (`outline`, `expand`, `screenshot`, `get`), and tab verbs; `doctor`.
- **Actions with diffs**: `click fill type press check uncheck select scroll upload
  eval` — every action quiesces (MutationObserver debounce), re-observes, and prints a
  diff of what changed (never a full snapshot). Occlusion pre-check fails fast naming
  the covering element; native dialogs auto-handled and reported as notes; honest
  `no change detected` outcome.
- **Compound verbs** (deterministic state machines, one diff): `select` on custom
  dropdowns (open → match revealed option → click), `fill-form <sid> --data '{…}'`
  with per-field ✓/✗ outcomes, `search [--pick]` with suggestion handling.
- **`query`**: regex filtering + column projection over list/table sections, with
  stable item indices and clickable refs.
- **MCP server**: `ebrowse mcp` — stdio JSON-RPC, six tools, shares the daemon (and
  browser state) with the CLI.
- **Summarizer sidecar** (optional, never load-bearing): one batched call per page to
  any OpenAI-compatible server; sqlite cache keyed by content hash; circuit breaker;
  `≈`/`|` provenance markers.
- **Image refs & VLM captions**: `@iN` refs on large images, `screenshot --ref @iN`,
  lazy expand-time captions for alt-less images.
- Golden-tested output formats; pure/browser/e2e test tiers; real-site smoke script
  (`scripts/smoke_real_sites.py`). Measured outlines at 1–9% of the token cost of a
  full aria snapshot on large pages.

[Unreleased]: https://github.com/jayoohwang1/ebrowse/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jayoohwang1/ebrowse/releases/tag/v0.1.0
