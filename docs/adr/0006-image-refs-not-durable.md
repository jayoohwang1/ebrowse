# 0006 — @i image refs are page-scoped; captions are expand-time only

Status: accepted (2026-07-03)

## Context

Big images matter for some tasks (product photos, charts), so they need refs for
`screenshot --ref` and optional VLM captions. But images are not action targets, and
captioning every image on every page would burn VLM budget mostly on decoration.

## Decision

- Images ≥ 80×80 rendered px get page-scoped refs `@i1, @i2, …` — deliberately NOT
  durable across observations, unlike `@e` refs. The distinct prefix signals this.
- Captions are lazy and expand-time only: when a section is expanded, alt-less `@i`
  images in it (≤ 4 per expand) are clipped via screenshot and captioned by the
  multimodal sidecar, cached by src-hash. Images *with* alt text never spend caption
  budget — alt is usually adequate and free. Cached captions render as `![≈caption](@iN)`.
- Outline generation never triggers captioning.

## Consequences

- Caption cost is proportional to what the agent actually reads.
- Real-site check (adoptapet): lazy-loaded card images are often sizeless at capture
  and their link text already carries the information — the alt-less-only budget rule
  holds up.
- List-section screenshot summarization (captioning a whole section as one image)
  remains unimplemented; the text-digest path has been sufficient on every real site
  tested.
