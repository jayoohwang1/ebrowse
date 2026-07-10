# 0011 — Lossless budgeted section partitioning

Status: accepted (2026-07-10)

## Context

The original splitter treated semantic tags such as `<form>` as unconditional
terminals. A form containing a large table therefore became one enormous ordinary
section: expansion could cost tens of thousands of tokens, while table pagination
and query were unavailable. Descending through a wrapper was not sufficient because
the old recursion owned only its children and could discard direct text or the
wrapper's own click signal. The hard `max_sections` tail merge could also reconstruct
an oversized section after a successful split.

## Decision

Sections are lossless owned fragments. Oversized ordinary containers are partitioned
at stable child boundaries with headroom below `observe.max_section_tokens`; wrapper-
owned text and interaction signals are retained in a shallow projection. Queryable
list/table descendants are promoted into their own sections, with residual runs on
either side preserving document order. Collection capability is defined by one shared
adapter used by classification, counting, expansion, and query.

`max_sections` is a soft outline-size target. Compatible adjacent content fragments
are merged only while the expansion budget remains satisfied; collection and semantic
boundaries outrank the target. Default collection expansion/query pages are also
token-budgeted, while `--all` and an explicit query `--limit` remain opt-in escape
hatches.

## Consequences

- Ordinary default expansions remain bounded without hiding controls or duplicating
  collection rows.
- Large forms may become more than one form section around promoted collections;
  `fill-form` operates on each residual section independently.
- An outline may exceed `max_sections` when no safe merge exists.
- Section fingerprints use the first owned structural root when a fragment needs a
  synthetic forest container, improving stability over fingerprinting the synthetic
  `<div>`.
- Collections support native and ARIA list/table/grid semantics, including multiple
  `<tbody>` elements.
