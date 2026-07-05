# 0001 — LLM sidecar is never load-bearing

Status: accepted (2026-07-03)

## Context

Section outlines are much more skimmable with model-written one-line labels than with
verbatim page-text previews. But an LLM dependency on the critical path would make the
tool slow, flaky, and unusable without a configured endpoint — and model output
injected into structured output is an injection surface.

## Decision

The summarizer sidecar only ever produces *navigational hints*: section labels and
image captions. Everything the host agent acts on — refs, element states, expanded
content, diffs — is derived deterministically from the DOM. Every feature works with
`summarizer.enabled = false`. Concretely:

- Summaries are computed in ONE batched call per page, in a background task, cached by
  section `content_hash` in sqlite; they appear on the *next* outline, never mutating
  already-emitted output.
- A circuit breaker (3 consecutive failures → off for 10 min) keeps a dead server from
  taxing every observation.
- Injection hygiene: model output is length-clamped, control-stripped, and `(@eN)`
  tokens are removed (the original design allowed up to 2 validated inline refs;
  v1 strips all refs for simplicity). Renderers add the `≈` provenance marker so the
  host can distinguish model paraphrase (`≈`) from verbatim page text (`|`).

## Consequences

- The tool is fully functional (and fully testable) with no LLM anywhere.
- Labels are strictly cosmetic; agents must treat `≈` lines as hints, not ground truth.
- Summary freshness is one observation behind when the cache is cold.
