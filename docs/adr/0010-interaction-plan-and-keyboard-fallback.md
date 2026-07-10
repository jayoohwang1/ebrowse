# 0010 — InteractionPlan routing and the keyboard-activation fallback

**Status:** Accepted (2026-07-09)

## Context

ADR 0009 fixed the false-block class for `click`, but every pointer verb had
its own partial copy of the pipeline (or none: `type`'s focus click had no
cover handling at all), and a click blocked by a *non-modal* cover was still a
dead end even when the target was a native control any keyboard user could
operate.

## Decision

1. **One pre-dispatch pipeline** (`src/ebrowse/interaction.py`): scroll →
   center-point probe → route `direct` / `label` / `obstructed`. Dialog covers
   and modal contexts raise immediately; `obstructed` carries a prebuilt
   blocked error the verb raises unless a safe fallback applies. `click`,
   `check`/`uncheck`, and `type`'s focus click all use it. Native
   `fill`/`select_option`/`upload` stay on their non-pointer APIs.
2. **Keyboard-activation fallback (surprising behavior):** a *plain* click on
   a natively focusable control (or a focusable ARIA button/link/checkable)
   blocked by a NON-modal cover completes as trusted `focus()` + Enter/Space —
   what a keyboard user does when an overlay doesn't trap focus. Constraints:
   never when a dialog/`aria-modal`/`inert` context is detected; focus must
   verifiably land on the target (`activeElement` check — traps refuse it,
   so the route fails closed); native/ARIA activation keys only, no guessing;
   always disclosed via `note: … activated via keyboard`. Consequence: a
   button under a focus-transparent overlay (e.g. a cookie veil without
   dialog semantics) is *activated*, not blocked — that matches what the
   platform allows a human to do, and `diagnose` still names the cover.
3. **`type` under a non-modal cover skips its focus click** — typing focuses
   without a pointer, so a decorative cover must not block text entry.
4. **Cross-frame covers** (`core/js/cover_above.js`): for iframe targets the
   parent document is probed at the target's viewport point, and diagnosis
   may name an actionable control *inside* a cover (a consent bar's own OK
   button) as the recovery ref.

## Consequences

- One behavior and error vocabulary across the pointer verbs; fixture
  coverage exercises each route once instead of per-verb.
- Fewer hard blocks: the remaining "blocked" outcomes are modal contexts
  (correct) and non-focusable custom widgets under real covers (honest).
- A disclosed lower-fidelity route exists (keyboard ≠ pointer sequence):
  hover-dependent widgets won't fire hover effects. Acceptable — the note
  makes the route visible to the agent.
- Compound verbs intentionally bypass the plan pending their rework.
