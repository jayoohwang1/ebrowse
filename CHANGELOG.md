# Changelog

All notable changes to ebrowse. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [SemVer](https://semver.org/). Unimplemented plans live in
[GitHub issues](https://github.com/jayoohwang1/ebrowse/issues), not here.

## [Unreleased]

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
