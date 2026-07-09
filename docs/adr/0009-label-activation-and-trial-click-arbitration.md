# 0009 — Label activation and trial-click arbitration in the click pre-check

**Status:** Accepted (2026-07-09)

## Context

The click pre-check sampled one point — the target's center — with
`elementFromPoint()` and hard-refused the click when the hit element was outside
the target's containment chain. Real-world testing (Amazon's currency radios)
showed this is too weak an evidence standard: restyled native controls routinely
place a decorative sibling (`<i>`, `<svg>`) over a transparent input inside a
`<label>`, so the "cover" is actually the control's intended click surface. The
error then pointed the agent at a decorative node that discovery deliberately
does not expose — an unrecoverable hard block. See
docs/interaction-reliability-assessment.md (main branch) for the full analysis.

## Decision

A hard pre-dispatch block requires stronger evidence than a one-point geometric
mismatch:

- **Associated-label hits are not occlusion.** If the hit element lies inside a
  label associated with the target (`el.labels` or a wrapping `label`), the
  click is routed through the label — browser-defined activation semantics, no
  site knowledge — and the diff discloses `note: clicked via the associated
  label`. Only for plain left single clicks; modified clicks fall through.
- **Only a dialog cover blocks pre-emptively.** A cover inside
  `dialog/[role=dialog]/[role=alertdialog]` is strong evidence the click can't
  mean what the agent intended, and the dialog is exposed in the outline, so the
  error names a recoverable target.
- **Any other cover is arbitrated by Playwright.** A short trial click
  (`trial=True`, 2s) applies the same scroll/stability/receives-events rules as
  the real click, with retries that let transient layers clear. Only a sustained
  interception raises `blocked: … covered by …`.
- Containment uses the composed tree (shadow-root hosts), not `.contains()`.

## Consequences

- The Amazon class of false blocks (restyled radios/checkboxes, external
  `label[for]` surfaces) clicks normally; verified on amazon.com and
  bestbuy.com plus fixtures (`styled_controls.html`).
- A genuinely covered click now costs up to ~2s (trial timeout) before failing
  instead of failing instantly; the modal/dialog paths still fail fast.
- Label routing changes the physical click point (label center, not input
  center). Acceptable: it is exactly what a human does, and the route is
  disclosed in the diff notes.
- The trial click still samples Playwright's chosen point; a control whose
  center is covered but which is otherwise clickable remains blocked — a known
  residual (failure-only diagnostics are the planned follow-up).
