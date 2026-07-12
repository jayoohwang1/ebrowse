# 0012 — Ax view is derived from DomSnapshot, not the native accessibility tree

Status: Accepted (2026-07-12)

## Context

Agents already know accessibility-tree formats, so Playwright's
`aria_snapshot()` initially appears to be the natural source for an ax view.
But it carries no durable ebrowse refs: an agent could read the tree but could not
act on its nodes. It also adds a browser round trip and is not purely testable.

## Decision

Derive an approximate accessibility tree in pure core from the existing
DomSnapshot, retaining ebrowse `@refs` inline.

## Consequences

- The tree is an honest approximation: accessible-name resolution is simplified,
  `aria-owns` is not followed, and hidden nodes are pruned.
- The renderer is golden-testable.
- Rendering adds no browser traffic.
