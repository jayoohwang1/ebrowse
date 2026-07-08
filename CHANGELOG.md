# Changelog

All notable changes to ebrowse. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [SemVer](https://semver.org/). Unimplemented plans live in
[GitHub issues](https://github.com/jayoohwang1/ebrowse/issues), not here.

## [Unreleased]

### Added

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
