# 0015 — CDP snapshot capture and backend-node-ID ref binding

Status: Accepted (2026-07-19)

## Context

Two forces converged on the capture layer:

1. **Unlocatable anonymous elements.** Eval run `qwen-hard-smoke-f707d765…`
   (petfinder.com) surfaced a hard failure class: an icon-only `<button>` with no
   id/testid/name/text produces an `ElementDesc` for which `locate.resolve()`
   generates zero candidates. The ref is permanently unclickable and the
   "run 'ebrowse outline'" recovery hint is a lie — a fresh outline reissues the
   same unresolvable descriptor. Descriptor fallbacks (class tokens, custom
   attributes) close most of this gap, but a binding to the *exact node* seen at
   capture time closes all of it for the fresh-outline case, including the
   known descriptor-identical-siblings misbind window noted in `core/locate.py`.
2. **Stealth.** discover.js currently executes in the page's main world. A page
   that traps `Element.prototype` methods can detect — and poison — the walk.
   Out-of-the-box stealth is a project goal.

Bindings considered: DOM attribute stamping (`data-eb="…"` during the walk),
a JS element registry in a CDP isolated world, and CDP `backendNodeId`s.
Stamping mutates the page (observable via MutationObserver; strippable by
frameworks). The isolated-world registry avoids mutation but adds live renderer
state (lifetime, memory pinning of detached subtrees) and still requires the
same action bridge, while advancing capture fidelity not at all.

## Decision

Replace the in-page discover.js walk with **`DOMSnapshot.captureSnapshot`** over
a persistent CDP session, and bind every discovered element to its
**`backendNodeId`**.

- `core/cdp_capture.py` is a **pure translator**: CDP flat arrays →
  the existing `DomNode`/`DomSnapshot` shape. The output contract is
  byte-compatible; the pure pipeline, JSON fixtures, and golden tests are
  untouched. In-page-only logic (accessible-name resolution, `label[for]`
  maps, `:disabled` fieldset inheritance, clickable signals) is recomputed in
  Python over the flat arrays — the snapshot includes hidden nodes, so this is
  possible and moves logic into the testable core.
- `DomNode` gains a runtime `backend_node_id`; the session refreshes a
  `ref → backendNodeId` table on every observe. The binding is consumed as a
  **rescue, not the primary path**: locators from the descriptor chain remain
  the actor whenever they resolve (that machinery — occlusion arbitration,
  label routing, keyboard fallbacks — is battle-tested and unchanged), and the
  `CdpTarget` bridge acts on the bound node only when the chain refuses (zero
  candidates on an anonymous element, or every candidate mismatched). A dead
  binding keeps the descriptor error **loudly**. Refuse-over-misbind (ADR
  0003) is preserved: the binding can only ever point at the exact node the
  outline described, and its staleness is detectable (unlike positional or
  geometric fallbacks, which were rejected for silently misbinding exactly the
  elements with the least verifiable identity). Since every verb re-observes,
  bindings self-heal: a re-outline (or any action) re-binds, so the stale-ref
  recovery hint is now genuinely actionable even for anonymous elements.
- `CdpTarget` duck-types the Locator/ElementHandle slice the verbs and the
  interaction pipeline use; its page-side evaluates run in a private isolated
  world (`Page.createIsolatedWorld`), never the main world, and its input goes
  through the same trusted CDP Input events Playwright dispatches. Binding may
  become the primary act path later, after eval-suite soak.
- The **listener signal** (`el`, weak candidate tier) loses
  `getEventListeners()` (a command-line-API/JS-execution feature). It is
  approximated from capture data: `onclick`-family attributes, `cursor:pointer`,
  non-negative `tabindex`, `contenteditable`. (Same approximation set as the
  agent-browser reference.)
- **All verbs** consume the binding via a CDP-backed target abstraction in
  `interaction.py` (pointer via existing occlusion probes + input dispatch at
  the verified point; fill/select/check via focus + keyboard/callFunctionOn).
- **OOPIF frames keep the existing per-frame plain-evaluate stitching** (they
  already lack listener signals today); no bindings inside them — the
  descriptor chain applies. Same-process iframes come free with
  `captureSnapshot` (`contentDocumentIndex`).
- discover.js remains available behind `capture.engine = "js"` **temporarily**,
  until eval runs establish parity; then it is deleted and CDP-unavailable
  becomes a hard, explained error. A dev-mode parity harness captures with both
  engines and diffs the resulting `PageMem`s.

## Consequences

- Capture executes **no JavaScript in the page**: nothing for prototype traps to
  observe or poison. This is the strongest stealth posture available and the
  primary reason backendNodeIds beat attribute stamping despite ~3–4× the work.
- Fresh-outline interactions bind to the exact observed node — anonymous
  icon buttons and descriptor-identical siblings resolve correctly. Stale
  bindings fail loudly with a recovery hint that now genuinely works
  (re-outline → fresh binding).
- Chromium-only capture (already the commitment of ADR 0002); the plain-JS
  engine flag is the interim escape hatch.
- Bindings die on navigation and node replacement by design; cross-page ref
  persistence continues to ride on descriptors.
- Architecture principle 3 is rephrased: "Playwright calls only in
  core/snapshot.py …" becomes "browser I/O (Playwright or CDP) only in …".
- Future capture upgrades (paint order → pure-code occlusion, computed-style
  whitelists, layout text) become incremental additions to the translator
  rather than new in-page code.
