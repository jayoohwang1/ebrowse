# Changelog

All notable changes to ebrowse. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [SemVer](https://semver.org/). Unimplemented plans live in
[GitHub issues](https://github.com/jayoohwang1/ebrowse/issues), not here.

## [Unreleased]

### Fixed

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
