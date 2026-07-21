# 0019 — Resolve iframe scopes through the live frame graph, not CSS re-query

Status: accepted (2026-07-20)

## Context

Refs carry an `iframe_path` of frame ids (the frame element's id, title, or
src — whichever capture recorded). Act-time resolution walked that path with
`frame_locator('iframe[id=…], iframe[title=…], iframe[src=…]')`. On real
Salesforce this fails hard: Lightning keeps a hidden, stale duplicate of its
Report Builder iframe with the identical title, so every frame_locator hit two
elements and died on Playwright strict mode — no action inside the visible
frame could ever run. The same ambiguity could silently stitch captured frame
content onto the wrong iframe node.

## Decision

`locate._frame_scope` walks the LIVE Playwright frame graph: each path segment
is matched against child frames' element id/title/src/name (one
`frame_element()` round trip per frame, never per element). Duplicate matches
are broken by geometry — the frame element with the largest visible box wins
(stale duplicates are display:none and have no box). The CSS frame_locator
chain remains only as fallback when the graph walk finds no match.
Capture-time stitching (`snapshot._match_iframe_node`) applies the same idea:
among fid-matching iframe nodes, the one whose captured rect best matches the
live frame element's bounding box gets the content.

## Consequences

- Duplicate-fid iframes (Salesforce Report Builder, portal shells) resolve to
  the visible frame instead of erroring; fixture `iframe_dup.html` locks this.
- fid stays the stable id/title/src string (ref identity and `model.py` are
  unchanged); uniqueness is no longer assumed anywhere.
- Frame resolution costs a few round trips per action only when
  `iframe_path` is non-empty; main-frame actions are untouched.
