# 0003 — Strict element-descriptor matching (misbinding is worse than churn)

Status: accepted (2026-07-03)

## Context

Refs (`@eN`) promise durability: the same element keeps its ref across re-observations
and navigations. Durability requires matching newly discovered elements against the
session registry. Loose matching (fuzzy text, bbox proximity) keeps refs alive through
more page changes but risks binding a ref to the *wrong* element — and an agent acting
on a misbound ref silently does the wrong thing, which is far worse than being told to
re-read the page.

## Decision

`RefRegistry` matches on exact `ElementDesc.match_key()` equality (tag, role, id,
testid, accessible name, placeholder, normalized href, input type, text head, iframe
path). The k-th identical descriptor on a page binds to the k-th registered ref for
that key (`nth_hint` is a tiebreaker, deliberately excluded from identity). Anything
that doesn't match exactly gets a fresh ref.

## Consequences

- Misbinding is structurally impossible for descriptor-identical elements.
- A control whose visible text changes (a dropdown button relabeled "Sort by:
  Relevance" → "Sort by: Price") reads in diffs as remove+add rather than a state
  change — honest but slightly noisy. Revisit only if it confuses agents in practice.
- Registry grows monotonically per session (accepted; sessions are not immortal).
