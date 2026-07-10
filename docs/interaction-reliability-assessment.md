# Interaction Reliability Assessment

**Status:** Largely implemented (2026-07-09/10, PR #14). The chosen designs are
recorded in [ADR 0009](adr/0009-label-activation-and-trial-click-arbitration.md)
(label activation + trial-click arbitration) and
[ADR 0010](adr/0010-interaction-plan-and-keyboard-fallback.md) (InteractionPlan +
keyboard fallback); see CHANGELOG Unreleased for the full inventory (failure
diagnostics, candidate discovery, disabled/inert state, outcome evidence,
nested scrolling, hover/drag/multi-select). Remaining items are tracked in
GitHub issues #9–#13; option H (DOM-click fallback) was rejected in favor of
keyboard activation, and option J (paint order) stays parked pending the
measurement harness. This document remains the record of the analysis, not a
plan.

**Date:** 2026-07-09

## Purpose

This report records interaction reliability risks found while investigating a real
Amazon currency-selection failure. It is intended to inform future issues, design
experiments, and ADRs. It does not select a solution or create a roadmap; once the
team chooses work, that work belongs in GitHub issues and any non-obvious decision
should receive an ADR.

The assessment uses the project's existing constraints as its baseline:

- Deterministic DOM/ARIA semantics must remain load-bearing; visual or LLM evidence
  can be advisory only.
- Default outline output must stay token-efficient and its contracts remain frozen
  unless deliberately updated with golden tests and documentation. Expanded sections
  may deliberately favor comprehensive candidate discovery over aggressive filtering.
- Core structure analysis remains pure. Page-side inspection should remain
  centralized in the snapshot/evaluation boundary rather than spread across verbs.
- Refs must remain strict: acting on the wrong repeated control is worse than ref
  churn or an explicit failure.
- Generic browser and web-platform semantics are acceptable. Bounded generic
  heuristics are also acceptable when their false-negative reduction is measurable,
  their failure modes are documented, and they do not depend on a particular site.

Related references: [architecture](architecture.md),
[output contracts](output-contracts.md), and
[ADR 0003: strict element descriptor matching](adr/0003-strict-ref-matching.md).

## Executive Summary

The current interaction pipeline has three distinct models of a target:

1. Discovery exposes a semantic DOM element as an `@eN` ref.
2. Locator resolution finds a live element from its descriptor.
3. Click preflight uses a single center-point hit test to decide whether the live
   element is geometrically clickable.

These models normally agree, but modern component styling frequently makes them
different. A semantic native input can be activated through an associated `<label>`
while a decorative sibling is the pointer receiver. Conversely, an actual overlay
can receive pointer events without appearing as an exposed, actionable ref.

The resulting principle is:

> A hard interaction block requires stronger evidence than a one-point geometric
> mismatch. When equivalence is defined by browser semantics, use it; otherwise let
> the browser actionability engine or a failure diagnostic establish the block.

The best near-term direction is broad candidate discovery in expanded sections,
paired with a standards-limited semantic fallback for native controls and a
centralized actionability layer. The best long-term direction is an explicit
distinction between a semantic control, its evidence, and its legal activation
surfaces. Strict ref identity should remain separate from a permissive discovery
policy: the tool may expose more candidates without silently resolving a ref to the
wrong repeated element.

## Incident: Amazon Currency Radio

On Amazon's language and currency preferences page, `ebrowse expand` exposed a
native USD radio input as the only actionable ref. The DOM structure was effectively:

```html
<label class="a-radio a-radio-fancy">
  <input type="radio" ...>
  <i class="a-icon a-icon-radio"></i>
  <span class="a-label">$ - USD - US Dollar (Default)</span>
</label>
```

The click preflight sampled the input's center. `document.elementFromPoint()`
returned the sibling `<i>`, which is neither an ancestor nor descendant of the
input, so the action failed before Playwright attempted it. The reported recovery
was to interact with the `<i>`, but discovery intentionally did not expose that
decorative node as an `@eN` ref.

This is not Amazon-specific. It is a common implementation of native radios,
checkboxes, file inputs, and other visually restyled controls. In this particular
case, browser-defined label activation supplies a safe semantic relationship:
clicking the associated label activates the native input.

## Current Interaction Model

The intended click path is:

```text
@ref -> ElementDesc -> Playwright locator -> scroll -> occlusion preflight
     -> Playwright click -> quiesce -> re-observe -> DOM diff
```

Relevant implementation points:

- `ActionsMixin._check_occlusion()` samples only the target's center and hard-fails
  when the hit-tested element is unrelated in the DOM containment tree.
- Only `verb_click()` invokes that preflight. `type`, search, and compound dropdown
  workflows perform their own pointer actions.
- Discovery recognizes wrapped labels for accessible naming, but labels themselves
  are not normally extracted as interactive elements.
- `resolve()` reconstructs a locator from descriptor fields such as ID, test ID,
  role/name, placeholder, href, or visible text.
- Action outcome is inferred primarily from URL comparison plus a post-action DOM
  snapshot and diff.

The architecture documentation reserves page-side inspection for the snapshot
boundary. The existing preflight evaluation in `actions.py` is therefore a warning
sign for future design: any richer interaction probe should be a typed, centralized
helper, not a growing collection of action-specific injected JavaScript.

## Discovery Recall and Activation Certainty

Discovery answers "what might the agent need to act on?" while actionability answers
"which route can safely cause the intended interaction now?" They should not share
one all-or-nothing boolean.

For targeted expansions, the discovery layer can include broad, generic candidate
signals such as a direct pointer listener, `tabindex`, an ARIA state, AX
focusability/editability, cursor pointer, or a wrapper around a form control. This
raises recall without inflating the whole-page outline because agents only pay for
the sections they expand. Candidate evidence should remain available to the action
layer, but weak evidence must not authorize a proxy click or a through-overlay
fallback on its own.

This separation permits the following policy:

- expose a likely custom control in `expand` rather than omit it;
- use the target's normal Playwright action first;
- auto-route through another element only for a proven browser semantic such as an
  associated label; and
- surface other fallbacks as explicit or clearly annotated lower-guarantee routes.

## Confirmed and Likely Edge Cases

### 1. False geometric blocks

The current center-point test can falsely block valid activation in these generic
patterns:

- Native control plus sibling visual glyph inside a wrapping or external label.
- A control whose center is covered but whose remaining visible area is clickable.
- Sticky headers, tooltips, badges, transient loading layers, and animated layout
  shifts that briefly cover the center.
- Rounded, clipped, transformed, or non-rectangular elements whose bounding-box
  center is not a valid click point.
- Open shadow-DOM components where containment relationships do not faithfully
  express the composed pointer target relationship.

This behavior is currently implemented in
[`actions.py`](../src/ebrowse/actions.py#L132-L191). It is deliberately fast, but
its confidence threshold is too low for a hard refusal.

### 2. A blocker that cannot be recovered from

Discovery recognizes native interactive tags, selected ARIA roles, inline listener
attributes, and top-level `cursor: pointer` hints. A genuine covering element may
instead be a generic `div` wired via `addEventListener`, a CSS overlay, a pseudo
element, or an inaccessible third-party surface. It can intercept a click while
having no ref and no actionable control in the outline.

The tool must not say "interact with that first" unless it can identify an exposed
target or another concrete recovery action. A better error distinguishes:

- an exposed dialog/popover and its actionable control;
- an unexposed geometric cover;
- a pseudo-element or cross-frame cover; and
- a failed action for which no blocker could be classified.

### 3. Inconsistent actionability behavior across verbs

`verb_click()` performs a custom preflight, while the following paths rely directly
on Playwright behavior:

- focus click before `type`;
- search-box focus and suggestion click;
- custom dropdown trigger and option click;
- form-filling interactions; and
- check/uncheck through `set_checked()`.

As a result, the same modal or proxy pattern can produce an immediate hard block,
a timeout, a generic interception error, or a silent no-change result depending on
which verb an agent uses. This inconsistency makes recovery behavior difficult to
teach and test.

### 4. Exposed ref, no usable locator

An element can receive a ref but lack a locator candidate. Common examples include
an icon-only button with no accessible name and an `<input type="submit" value="Save">`
without an ID or label. The displayed value is state, not descriptor text, so the
current resolver may have no ID, test ID, role/name, placeholder, href, or text to
use.

Repeated controls introduce the opposite risk. `nth_hint` is based on occurrence
among identical descriptors, while locator candidates can produce a different set.
A reactive reorder of repeated `Edit`, `Remove`, or radio controls can make an old
ref point at the wrong live element. Broad CSS targets currently choose `.first`,
which has the same risk.

Any fallback must prefer an ambiguity error over a fuzzy action. Improving this
case should not relax the strict-ref policy documented in ADR 0003.

### 5. Discovery and semantic activation surfaces differ

The snapshot walker correctly derives names from associated and wrapped labels, but
the extractor exposes the form control rather than the label activation surface.
That creates a structural mismatch for visually hidden native controls.

Related patterns include:

- transparent native input with a visible custom indicator;
- external `<label for="...">` that owns the visual click area;
- native file input activated through a label or button-like proxy;
- a custom ARIA checkbox/radio/switch where `set_checked()` is not applicable; and
- nested interactive content that extraction intentionally suppresses beneath a
  parent link or button.

The right abstraction is one semantic control ref with zero or more proven legal
activation surfaces, rather than duplicate user-visible refs for decorative nodes.
Expanded sections may additionally expose broad candidate controls when generic
evidence supports them; candidate status is not evidence that a nearby sibling is a
legal proxy for the control.

### 6. Visibility and effective-disabled state are shallow

Discovery prunes `display: none` and `visibility: hidden`, then records every
remaining extracted element as visible. This does not account for all forms of
effective inactionability:

- inherited disabled state from a disabled `fieldset`;
- `inert`, focus traps, and top-layer modal behavior;
- `pointer-events: none`;
- clipping, content visibility, and some collapsed/disconnected layout states;
- `aria-hidden` or an opacity-zero overlay; and
- a modal marked with `aria-modal` that is visible in geometry but does not actually
  intercept pointer events.

The answer is not to hide every transparent input: transparent native inputs often
need to remain represented so they can be paired with their label. Instead, capture
enough optional state to explain why a ref requires a semantic proxy or may fail.

### 7. Frames, shadow DOM, and cross-context covers

The current preflight evaluates in the target's own frame, which avoids treating the
iframe element itself as a cover. It cannot, however, see a parent-page banner or
modal sitting above the iframe. Playwright can then fail later with an unhelpful
interception diagnostic.

There is also a concrete frame identity mismatch: capture falls back to a frame URL
when an iframe has no ID or title, while locator resolution searches path components
only by iframe ID or title. Refs inside common id-less iframes can therefore be
captured but not acted upon. Closed shadow DOM and inaccessible cross-origin widgets
remain inherent limitations and require explicit recovery paths rather than false
assurances.

### 8. Widget and gesture coverage gaps

The current verbs cover common native controls well, but common browser interaction
patterns remain outside their semantic model:

- ARIA checkbox, radio, switch, slider, and combobox widgets;
- `<select multiple>` and large native selects whose options are truncated from the
  snapshot;
- virtualized or pre-rendered dropdown options that do not appear as newly added DOM
  nodes;
- contenteditable and rich-text editor behavior that depends on actual key events;
- hover-only menus, drag-and-drop, sortable lists, canvas/image-map controls, and
  nested scroll containers; and
- browser-owned surfaces such as downloads, permissions, payment requests, auth
  prompts, and some file chooser flows.

These are not all candidates for default support. They do establish the need for
clear capability boundaries and explicit escape hatches.

### 9. Outcome detection can under-report success

The post-action result is DOM-centric. A click can be successfully dispatched while
the tool reports `no change detected` when:

- a hash/anchor navigation changes scroll position but not the fragment-stripped URL
  or DOM;
- a document reloads to the same URL;
- asynchronous work begins after the capped quiescence window;
- a mutation occurs in an iframe or shadow root outside the main-document observer;
- only `aria-selected`, `aria-pressed`, focus, CSS, canvas, or browser state changes;
- a popup or download occurs without a meaningful page DOM change.

The reverse risk is more serious: an interaction can dispatch and then time out.
The tool must not blindly retry a real click after that, because it may duplicate a
purchase, navigation, or irreversible operation.

## Solution Directions

The following approaches are complementary. None alone solves arbitrary website
behavior.

### Comprehensive candidate discovery in expanded sections

The current output favors a small set of high-confidence interactive nodes. A more
recall-oriented expansion mode can add candidates backed by generic evidence:

- direct click, mouse, or pointer listeners when available;
- `tabindex`, ARIA focusability, editable/settable state, or interactive ARIA state;
- cursor pointer and native/ARIA roles;
- wrapper labels or form-control wrappers; and
- actual scrollability for nested panels and iframes.

**Benefits**

- Reduces the chance that an agent cannot even name a crucial custom control.
- Costs tokens only when an agent expands the relevant section, not in the outline.
- Makes reasonable generic heuristics useful without turning them into global page
  noise.

**Costs and limits**

- A candidate may be focusable, decorative, or structurally related to a control
  without accepting a useful click.
- More refs increase duplicate and repeated-control pressure, so descriptor
  validation and explicit ambiguity remain important.

**Assessment:** High value for targeted expansion. Preserve evidence provenance so
the action layer can distinguish a native control from a weak candidate.

### A. Make Playwright actionability authoritative

Remove the generic center-point hard block and either execute the locator action
directly or perform `locator.click(trial=True)` before the real click.

**Benefits**

- Uses the same scrolling, stability, visible-point, and receives-events rules as
  the eventual click.
- Avoids a duplicate, narrower geometry implementation.
- Broadly applicable to ordinary DOM controls and lower maintenance than custom hit
  testing.

**Costs and limits**

- Direct execution can wait until timeout before producing recovery information.
- A trial doubles some actionability work and still has a small race before the
  real click.
- Neither route alone explains which overlay or control should be used next.

**Assessment:** High universality and robustness for ordinary pointer interactions;
low-to-medium implementation complexity. This is a strong candidate for the default
execution authority.

### B. Add standards-defined semantic proxy activation

For native checkboxes and radios, detect whether the intercepted node lies within
an associated label (`input.labels` or a wrapping label). If so, activate the label
rather than force-clicking the input, then verify the expected checked state.

**Benefits**

- Directly solves a widespread, standards-defined pattern without site knowledge.
- Preserves normal browser label activation, event propagation, and accessibility
  semantics.
- Keeps one semantic ref in the outline rather than exposing decorative markup.

**Costs and limits**

- It intentionally does not generalize to arbitrary siblings, classes, or custom
  components.
- Labels can contain unusual nested content or custom listeners, so the fallback
  must be limited to native checkable controls and verified afterward.

**Assessment:** Very high payoff, high maintainability, and high confidence when
strictly constrained to HTML label semantics. This is the preferred immediate fix
for the reported failure class.

### C. Use multi-point hit testing only as an advisory or opt-in fallback

Sample a small deterministic set of points in the visible target area with
`elementsFromPoint()`. A point that resolves to the target, its descendant, or a
proven semantic proxy demonstrates that the center is not authoritative.

**Benefits**

- Helps with partial overlays, sticky headers, and irregular geometry.
- Can produce much better diagnostics than one center point.

**Costs and limits**

- Clicking a chosen coordinate can change behavior when a control has distinct
  subregions.
- Clipping, transforms, frames, and shadow boundaries make a universal safe-point
  heuristic difficult.

**Assessment:** Medium universality and maintainability. Prefer it for diagnostics
or an explicit `--position` style escape hatch, not an invisible automatic click
strategy.

### D. Introduce a centralized InteractionPlan

Create one action-layer planner that accepts a semantic target and operation, probes
the live page once, and chooses a bounded execution route:

```text
semantic ref
  -> resolve live locator
  -> InteractionProbe (trial/actionability, labels, modal state, hit-test evidence)
  -> InteractionPlan (direct | associated-label | blocked | explicit escape hatch)
  -> execute once
  -> observe outcome
```

All pointer-requiring operations should use it: click, type focus, search focus and
pick, custom select open/pick, and check/uncheck. Native `fill`, `select_option`,
and upload should continue to use their specialized APIs rather than be converted
to clicks.

**Benefits**

- One consistent behavior and error vocabulary across verbs.
- Easier fixture coverage and fewer divergent action-specific workarounds.
- Makes automatic fallback rules explicit, reviewable, and testable.

**Costs and limits**

- A substantial refactor with a real risk of growing an opaque heuristic ladder.
- The probe belongs at the sanctioned snapshot/evaluation boundary, which requires
  a careful API design that keeps `core/` data-oriented and pure downstream.

**Assessment:** High maintainability and robustness over time. It is the best
structural follow-up after proving the narrow label fallback.

### E. Capture failure-only blocker diagnostics

When a trial or real action fails before dispatch, take one fresh deterministic
diagnostic snapshot. It can report the hit-test stack, nearest dialog/popover,
effective inert/modal state, and any exposed interactive ancestor of the cover. If
the browser backend supports it, inspect direct listener evidence only for the cover
and a bounded ancestor chain rather than scanning every page element.

**Benefits**

- Spends extra work and output only on failure.
- Converts many dead-end errors into an actionable ref or a truthful limitation.
- Detects overlays that appeared after the last outline.

**Costs and limits**

- No reliable DOM rule identifies every popup, toast, backdrop, or pseudo-element.
- The output must avoid falsely claiming that an unexposed cover is actionable.
- Direct-listener inspection is incomplete for framework event delegation and can be
  expensive if performed page-wide.

**Assessment:** High recovery value with moderate complexity. This should accompany
Playwright-authoritative actionability rather than replace it.

### F. Extend snapshot metadata and state evidence

Add optional, non-rendered-by-default fields for activation surfaces, associated
labels, effective disabled/inert state, pointer-event clues, selected/pressed state,
and a resilient frame identity. Expand action evidence to include document identity,
hash/scroll movement, popup/download events, and selected ARIA state.

**Benefits**

- Keeps required evidence deterministic and available without per-element browser
  round trips.
- Improves both execution planning and truthful action diffs.
- Preserves token economy when fields are retained internally rather than printed
  by default.

**Costs and limits**

- `model.py` and output contracts are frozen interfaces; additions require careful
  optional-field design, fixtures, documentation, and golden-test review.
- DOM paths and geometry are volatile, so metadata must not become a silent
  replacement for strict descriptor identity.

**Assessment:** High long-term value and medium complexity. Use it to support proven
rules, not to create unbounded heuristics.

### G. Build widget-specific semantic capabilities selectively

Support generic web-platform semantics for ARIA widgets where there is a reliable
operation: activate a custom checkbox/radio with click or Space, select a native
multi-select explicitly, or use keyboard arrows for a focused native range.

**Benefits**

- Improves common accessible component patterns.
- Gives agents intentional verbs instead of forcing `eval` or raw CSS selectors.

**Costs and limits**

- Custom widget behavior varies widely even when ARIA roles look similar.
- Each capability needs postcondition verification and broad real-site testing.

**Assessment:** Medium universality and maintenance cost. Add only where platform or
ARIA semantics supply a strong contract; do not build a generic custom-widget guesser.

### H. Experiment with a controlled DOM-click fallback

When normal pointer actionability fails, resolve the target and invoke its DOM
`click()` activation as a distinct route. This is the fallback used by the reviewed
browser-use watchdog after its own occlusion check.

**Benefits**

- Can recover native controls behind decorative or non-intercepting visual layers.
- Avoids a false negative when the semantic target remains connected but coordinate
  delivery is unreliable.
- Provides a useful empirical comparison against strict pointer-only behavior.

**Costs and limits**

- DOM click events are untrusted and omit the normal pointer sequence: hover,
  pointer down/up, mouse down/up, pointer capture, coordinate data, and sometimes
  focus or user-activation behavior. Widgets can no-op or behave differently.
- It can activate a control beneath a real cookie banner, age gate, confirmation
  sheet, autocomplete menu, checkout step, or modal that a human could not click
  through. The overlay can remain open while the underlying state changes.
- Popup, file picker, permission, payment, and anti-bot flows can reject untrusted
  activation. A post-action DOM diff alone may not explain the discrepancy.

**Recommended experiment shape**

1. Use Playwright trial or ordinary pointer action first.
2. Route native checkbox/radio controls through an associated label before trying
   DOM click; label activation has normal browser semantics and verifies the Amazon
   class of failure without bypassing the displayed UI.
3. Initially gate a generic DOM-click fallback behind an explicit option or config.
   Annotate the result with the cover and the reduced interaction guarantee.
4. Do not enable it automatically for a detected dialog, popover, `aria-modal`,
   `inert` region, iframe cover, or exposed interactive cover.
5. Execute at most once and never use it as a retry after a real action timeout.

**Assessment:** Worth trying because it may materially reduce hard blocks. It is a
lower-guarantee activation route, not evidence that the normal pointer action was
valid.

### I. Provide explicit escape hatches

Potential opt-in commands include coordinate click, targeted focus or key press,
nested-container scrolling, a generic through-overlay click, and a `diagnose @ref`
command.

**Benefits**

- Allows agents to recover from canvas, image-map, inaccessible iframe, or broken
  site implementations without requiring a universal heuristic.
- Makes a lower-guarantee route visible in the command and action result.

**Costs and limits**

- Coordinate clicks are resolution-, viewport-, and layout-sensitive.
- An escape hatch can still produce a state no normal pointer interaction could
  reach, so outcome reporting must identify the route used.

**Assessment:** Valuable beyond the DOM-click experiment and appropriate for agents
that explicitly prefer completion over strict interaction fidelity.

### J. Use paint order as evidence, not default ref removal

Browser-use captures paint order, bounds, and a limited set of computed styles, then
suppresses lower rectangles fully contained by higher opaque-looking rectangles. This
can reduce noisy serialized output, especially under a full-page modal or cookie
overlay.

**Benefits**

- Helps rank the controls in the visually topmost dialog or popover.
- Can collapse duplicated background content in an expanded section.
- Provides a cheap candidate index for failure diagnostics when combined with live
  hit testing.

**Costs and limits**

- A paint rectangle is not a hit-test result. `pointer-events: none`, transparent
  holes, rounded corners, clip paths, masks, transforms, pseudo-elements, iframes,
  top-layer dialogs, and animation can make bounds-based coverage wrong.
- Opacity and background-color thresholds are inherently approximate. A translucent
  visual layer can hide a usable ref, while an opaque-looking layer can be missed.
- Fixed headers and sidebars can cover a target only at one scroll position; removing
  its ref prevents the agent from scrolling it into a valid position.
- An unrecognized top overlay can hide all background refs without exposing a way to
  dismiss itself, recreating a hard block.

**Assessment:** Worth experimenting with for ranking, compacting non-interactive
content, and failure diagnostics. In recall-oriented expanded sections it should
annotate or deprioritize interactive refs rather than remove them by default. Use
`elementsFromPoint()` or Playwright trial actionability as the final pointer truth.

### K. Accessibility-tree or visual fallback

An accessibility-tree backend can help identify semantic widgets; screenshot or
coordinate-based interaction can help with canvas and inaccessible DOM.

**Benefits**

- Covers some controls that DOM heuristics miss.
- Can be useful for an explicit diagnostic or manual recovery mode.

**Costs and limits**

- Accessibility data does not establish geometric clickability or solve overlays.
- Visual interaction is resolution-sensitive, difficult to verify, and conflicts
  with deterministic load-bearing behavior if used by default.

**Assessment:** Useful optional tooling, not a replacement for DOM/Playwright
semantics and not a default action path.

## Guardrails

The following constraints should remain explicit in any implementation:

1. Treat `force=True`, coordinate dispatch, and DOM `click()` as distinct execution
   routes, not equivalent retries. A controlled DOM-click experiment may be useful,
   but it must be feature-gated initially and its result must disclose the route.
2. Do not retry a real action solely because it timed out. It may have dispatched
   before the timeout and a retry can duplicate an irreversible action.
3. Retry only pre-dispatch probes, locator resolution after DOM churn, or an action
   whose prior failure is known to have happened before dispatch.
4. Do not make ref matching fuzzier to reduce failures. Refuse ambiguity rather than
   silently target a different repeated control.
5. A recovery message must name an exposed ref, a specific command such as Escape or
   screenshot, or an honest limitation. It must not direct the agent toward an
   invisible decorative node.
6. Automatic proxy activation must be justified by standardized semantics, not DOM
   proximity, CSS class names, or site-specific markup.
7. Broad candidate discovery is allowed in expanded sections. Weak candidate
   evidence can justify exposing a ref, but not bypassing a pointer obstruction.
8. Paint order and style-derived coverage are evidence for ranking or diagnostics;
   they must not remove an interactive ref by default without confirming actual
   pointer routing.

## Evaluation Criteria

Any experiment should be evaluated against the following questions:

- **Universality:** Does the behavior follow HTML, ARIA, or Playwright semantics
  across sites, or does it infer intent from incidental DOM structure?
- **Robustness:** Does it avoid both false blocks and wrong-target clicks during
  re-rendering, scrolling, overlays, and asynchronous state changes?
- **Maintainability:** Is the behavior centralized, typed, and fixture-tested, or
  does it add another verb-specific exception?
- **Token cost:** Does it add default output or page round trips? Can detailed
  diagnostics occur only after a failure?
- **Action safety:** Can it accidentally bypass a modal, repeat an action, or hide
  uncertainty from the host agent? Does it disclose a lower-guarantee DOM or
  coordinate route?
- **Recovery quality:** Does the result provide an executable next step rather than
  merely naming a DOM node?

## Suggested Validation Matrix

Before treating any interaction change as complete, add fixture coverage for:

- wrapped and external labels with `i`, `span`, and `svg` visual proxies;
- transparent native inputs with label activation;
- a true modal, inert modal, popover, and CSS overlay that must remain blocked;
- a `pointer-events: none` cover, a translucent cover, and rounded or clipped cover
  whose bounding box overstates actual hit coverage;
- partial center occlusion with another valid target point;
- a generic DOM-click fallback that is gated, annotated, attempted once, and does
  not bypass a recognized modal or repeat after timeout;
- a target whose handler relies on pointer sequence, coordinates, focus, trusted
  activation, or an event-delegated ancestor;
- icon-only and input-value-only controls;
- broad-candidate signals from direct listeners, `tabindex`, ARIA state, cursor, and
  form-control wrappers, including evidence that does not produce a useful click;
- repeated controls reordered between observation and action;
- id-less iframe, parent-page overlay over an iframe, and open shadow-root control;
- ARIA checkbox/radio/switch and a native multi-select;
- pre-rendered, portal, and virtualized dropdown options;
- hash navigation, same-URL reload, delayed asynchronous mutation, popup, and
  download outcomes.

Run the affected browser tests and validate on at least two permissive real sites
from the Online-Mind2Web dataset. Record any retained generic heuristic as a concise
code comment, and write an ADR if the selected design changes a surprising behavior
or tradeoff.

## Decision Questions for Future Work

1. Is direct Playwright execution plus failure diagnostics sufficient, or is a short
   `trial=True` preflight worth its latency cost?
2. Should label activation be an internal execution detail, or should the model gain
   an optional activation-surface field now?
3. Which action evidence belongs in the frozen model versus session-local state?
4. What explicit escape hatch is acceptable without making unsafe behavior easy to
   invoke accidentally?
5. Should a generic DOM-click fallback graduate from an experiment to a default for
   any cover classifications, and how should its reduced guarantee be rendered?
6. Should paint-order information rank, annotate, or compact expanded content, and
   which live hit-test confirmation is required before it suppresses a ref?
7. Should generic action diagnostics be a separate `diagnose` verb, failure-only
   output, or both?
8. Which widget capabilities have enough web-platform semantic consistency to justify
   first-class verbs?

## Source Map

- `src/ebrowse/actions.py`: action lifecycle, occlusion preflight, quiescence, and
  Playwright failure mapping.
- `src/ebrowse/compound.py`: custom select, fill-form, and search interaction paths.
- `src/ebrowse/core/js/discover.js`: single-pass DOM visibility, accessible-name, and
  clickable-signal discovery.
- `src/ebrowse/core/pipeline.py`: element extraction and conversion into refs/state.
- `src/ebrowse/core/locate.py`: descriptor-to-locator and iframe-path resolution.
- `src/ebrowse/core/snapshot.py`: frame capture and snapshot boundary.
- `src/ebrowse/core/fingerprint.py`: descriptor occurrence and durable ref assignment.
- Browser-use reference implementation (reviewed locally): layered candidate
  discovery, wrapper-label handling, CDP listener detection, DOM-click fallback,
  and paint-order filtering. Its behavior is evidence for experiments, not a direct
  compatibility target for ebrowse.
