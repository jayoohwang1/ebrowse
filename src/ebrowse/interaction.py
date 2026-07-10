"""InteractionPlan: one pre-dispatch pipeline for every pointer-requiring verb.

    resolve -> scroll -> center-point probe -> route
      "direct"     normal Playwright action
      "label"      activation must go via the associated <label>
      "obstructed" pointer blocked by a NON-modal cover; the op may complete
                   through a verified non-pointer route (keyboard activation,
                   plain focus) or raise the prebuilt blocked error

Dialog covers and modal contexts raise immediately — no fallback route is
ever legal against a modal. All page-side probes live in core/js/*.js via
core/snapshot.py (probe_blocker) or the single inline preflight evaluate here;
never per-element round trips.

Mixed into Session below ActionsMixin (the verbs in actions.py drive this).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ebrowse.model import Element, PageMem
    from ebrowse.session import PendingDialog

from ebrowse.core.snapshot import probe_blocker, probe_cover_above
from ebrowse.errors import CommandError, ExitCode

# Trial-click budget when the one-point hit test found a cover: long enough for
# transient overlays/animations to clear, short enough to fail fast on a real one.
_TRIAL_TIMEOUT_MS = 2_000
_ACTIVATE_TIMEOUT_MS = 8_000


@dataclass(slots=True)
class InteractionPlan:
    route: str  # "direct" | "label" | "obstructed"
    cover: str | None = None  # generic cover name (obstructed route)
    blocked: CommandError | None = None  # raise this if no safe fallback applies


class InteractionMixin:
    """Pointer-interaction planning + fallback routes. Host contract below."""

    if TYPE_CHECKING:
        page_mem: PageMem | None
        _notes: list[str]
        _blocking_modal: str | None

        def _active_dialog(self) -> PendingDialog | None: ...
        @property
        def page(self) -> Page: ...

    # ------------------------------------------------------------- planning ----

    async def _plan_pointer(self, loc, target: str, plain: bool = True) -> InteractionPlan:
        """Centralized pre-dispatch pipeline. `plain` = plain-left-click
        semantics (label routing and keyboard fallback are activation
        equivalents of a plain click only — never of modified clicks)."""
        with contextlib.suppress(Exception):
            await loc.scroll_into_view_if_needed(timeout=2000)
        # disabled controls get refs (the agent must SEE the grayed-out submit),
        # but acting on one would burn the full Playwright timeout — refuse fast
        # with the state named. is_disabled covers native, aria-disabled, and
        # fieldset inheritance.
        disabled = False
        with contextlib.suppress(Exception):
            disabled = await loc.is_disabled(timeout=1000)
        if disabled:
            raise CommandError(
                f"blocked: {target} is disabled — the page must enable it first "
                "(complete required fields or prior steps), then retry; the diff of "
                "your next action will show it as enabled",
                ExitCode.ACTION_FAILED,
            )
        info = await self._check_occlusion(loc, target)  # raises on dialog cover
        if info.get("coverInLabel") and plain:
            return InteractionPlan(route="label")
        if info.get("covering"):
            # one-point mismatch is too weak for a hard refusal; let
            # Playwright's actionability engine (scroll + stability +
            # receives-events, with retries) arbitrate via a trial click
            try:
                await loc.click(trial=True, timeout=_TRIAL_TIMEOUT_MS)
            except Exception as e:
                diag = await self._probe_diagnosis(loc)
                blocked = self._blocked_error(diag, target) or CommandError(
                    f"blocked: {target} is covered by {info['covering']} — interact "
                    "with that first (run 'ebrowse outline' to see it)",
                    ExitCode.ACTION_FAILED,
                )
                modal_context = (
                    diag.get("coverDialog") or diag.get("openDialog") or diag.get("inert")
                )
                if modal_context or not plain:
                    raise blocked from e
                return InteractionPlan(route="obstructed", cover=info["covering"], blocked=blocked)
        return InteractionPlan(route="direct")

    async def _check_occlusion(self, loc, target: str) -> dict:
        """Center-point hit test on the live target. Returns the probe result:
        {covering, coverDialog, coverInLabel} (any subset). Hard-fails ONLY when
        the cover sits inside a dialog — that is strong evidence the click can't
        mean what the agent intended. A generic cover is weak evidence (restyled
        controls, partial/transient overlays) and is arbitrated by _plan_pointer.

        Separately RECORDS a modal that blocks the page without covering the
        target — native `showModal()` (top layer + inert) or an aria-modal
        focus trap, where `::backdrop` is a pseudo-element invisible to the
        geometric hit-test. That can't be pre-empted safely (a false positive
        would block a valid click), so it's surfaced post-hoc only if the click
        then no-ops (see actions._finish_action). Best-effort; Playwright still
        enforces."""
        try:
            handle = await loc.element_handle(timeout=2000)
            # handle.evaluate runs in the element's own frame — essential for
            # elements inside iframes, where main-frame elementFromPoint would
            # see only the <iframe> region and falsely report occlusion
            info = await handle.evaluate(
                """(el) => {
                    const name = (n) => n.tagName.toLowerCase()
                        + (n.id ? '#' + n.id : '')
                        + ((n.getAttribute('aria-label') || n.textContent || '')
                            .trim().slice(0, 40) ? ' "' + (n.getAttribute('aria-label')
                            || n.textContent).trim().slice(0, 40) + '"' : '');
                    // composed-tree containment: .contains() is blind across
                    // shadow roots, which turns open-shadow components into
                    // false "covered" verdicts
                    const within = (anc, n) => {
                        while (n) {
                            if (n === anc) return true;
                            n = n.parentNode || (n instanceof ShadowRoot ? n.host : null);
                        }
                        return false;
                    };
                    // browser-defined label activation: a hit anywhere inside an
                    // associated <label> (wrapping or for=) activates the control,
                    // so decoration there is a click surface, not occlusion
                    // (restyled radios/checkboxes: input + sibling icon in a label)
                    const inLabel = (t) => {
                        const labs = el.labels ? Array.from(el.labels) : [];
                        const wrap = el.closest ? el.closest('label') : null;
                        if (wrap && !labs.includes(wrap)) labs.push(wrap);
                        return labs.some((l) => within(l, t));
                    };
                    // (1) geometric occlusion at the element's center point
                    let covering = null, coverDialog = null, coverInLabel = 0;
                    const r = el.getBoundingClientRect();
                    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                    if (cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight) {
                        const t = document.elementFromPoint(cx, cy);
                        if (t && !within(el, t) && !within(t, el)) {
                            if (inLabel(t)) {
                                coverInLabel = 1;
                            } else {
                                covering = name(t);
                                const dlg = t.closest('[role=dialog],[role=alertdialog],dialog');
                                coverDialog = dlg ? name(dlg) : null;
                            }
                        }
                    }
                    // (2) a modal blocking the page but NOT over this point.
                    // Visible candidates only: aria-modal is often left set on
                    // hidden/closed dialogs (a false-positive trap).
                    let modal = null;
                    try {
                        for (const c of document.querySelectorAll(':modal,[aria-modal="true"]')) {
                            if (c.contains(el) || c === el) continue;
                            const cr = c.getBoundingClientRect();
                            if (cr.width > 0 && cr.height > 0
                                && getComputedStyle(c).visibility !== 'hidden') {
                                modal = name(c); break;
                            }
                        }
                    } catch (e) { /* :modal unsupported on this engine */ }
                    return {covering, coverDialog, coverInLabel, modal};
                }"""
            )
        except Exception:
            return {}  # pre-check is best-effort; Playwright will still enforce
        if not info:
            return {}
        if not info.get("covering"):
            # the in-frame probe cannot see a parent-page banner/modal above
            # the target's iframe — check the parent document too
            above = await self._cover_above(handle)
            for k in ("covering", "coverDialog"):
                if above.get(k):
                    info[k] = above[k]
            if above.get("modal") and not info.get("modal"):
                info["modal"] = above["modal"]
        if info.get("modal"):
            self._blocking_modal = info["modal"]
        if info.get("coverDialog"):
            raise CommandError(
                f"blocked: {target} is covered by {info['coverDialog']} — interact with "
                "that first (run 'ebrowse outline' to see it)",
                ExitCode.ACTION_FAILED,
            )
        return info

    async def _cover_above(self, handle) -> dict:
        """Parent-document cover probe for iframe targets; {} for main-frame
        targets or when the probe is unavailable. Best-effort."""
        try:
            frame = await handle.owner_frame()
            if frame is None or frame.parent_frame is None:
                return {}
            box = await handle.bounding_box()
            if not box:
                return {}
            # the frame element of the topmost non-main ancestor frame — the
            # element a parent-page overlay would sit above
            f = frame
            while f.parent_frame is not None and f.parent_frame.parent_frame is not None:
                f = f.parent_frame
            frame_el = await f.frame_element()
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            return await asyncio.wait_for(probe_cover_above(frame_el, cx, cy), timeout=3) or {}
        except Exception:
            return {}

    # ------------------------------------------------------------ diagnosis ----

    def _ref_for_chain(self, chain: list[dict]) -> Element | None:
        """Map a live cover's ancestor chain (identifying attrs from diagnose.js)
        to an exposed element, closest ancestor first. Exact id/testid/role+name
        matches only — naming the wrong ref is worse than naming none."""
        if not self.page_mem:
            return None
        by_id: dict[str, Element] = {}
        by_tid: dict[str, Element] = {}
        by_role_name: dict[tuple[str, str], Element] = {}
        for sec in self.page_mem.sections:
            for el in sec.elements:
                d = el.desc
                if d.id:
                    by_id.setdefault(d.id, el)
                if d.testid:
                    by_tid.setdefault(d.testid, el)
                if d.role and d.name:
                    by_role_name.setdefault((d.role, d.name), el)
        for node in chain:
            nid, tid = node.get("id"), node.get("tid")
            role, nm = node.get("role"), node.get("nm")
            if nid and nid in by_id:
                return by_id[nid]
            if tid and tid in by_tid:
                return by_tid[tid]
            if role and nm and (role, nm) in by_role_name:
                return by_role_name[(role, nm)]
        return None

    async def _probe_diagnosis(self, loc) -> dict:
        """One probe_blocker evaluate on the live target, plus exposed-ref
        resolution for the cover's ancestor chain. Returns {} when the probe
        is unavailable (native dialog pending, detached element, timeout)."""
        if self._active_dialog() is not None:
            return {}  # renderer is frozen by a native dialog; evaluate would hang
        try:
            handle = await loc.element_handle(timeout=2000)
            info = await asyncio.wait_for(probe_blocker(handle), timeout=3)
        except Exception:
            return {}
        if not info:
            info = {}
        if not info.get("cover"):
            # a parent-page cover above the target's iframe is invisible to
            # the in-frame diagnosis
            above = await self._cover_above(handle)
            if above.get("covering"):
                info["cover"] = above["covering"]
                info["chain"] = above.get("chain") or []
                info["inside"] = above.get("inside") or []
                if above.get("coverDialog"):
                    info["coverDialog"] = above["coverDialog"]
            if above.get("modal") and not info.get("openDialog"):
                info["openDialog"] = above["modal"]
        if info.get("cover"):
            # recovery ref: an exposed ancestor of the cover, or an exposed
            # control INSIDE it (a consent bar's own OK button)
            exposed = self._ref_for_chain(info.get("chain") or []) or self._ref_for_chain(
                info.get("inside") or []
            )
            if exposed is not None:
                info["exposedRef"] = exposed.ref
                info["exposedDesc"] = exposed.desc.short_desc()
        return info

    def _blocked_error(self, info: dict, target: str) -> CommandError | None:
        """Map a _probe_diagnosis result to an error naming an executable next
        step — an exposed ref, an open dialog, or an honest limitation. Returns
        None when nothing classifies (caller falls back to its generic message)."""
        cover = info.get("cover")
        dialog = info.get("coverDialog") or info.get("openDialog")
        if cover and info.get("exposedRef"):
            return CommandError(
                f"blocked: {target} is covered by {cover} — dismiss or interact with "
                f"{info['exposedRef']} ({info['exposedDesc']}) first",
                ExitCode.ACTION_FAILED,
            )
        if cover and dialog:
            return CommandError(
                f"blocked: {target} is covered by {cover} — a dialog is open ({dialog}); "
                "resolve it first (run 'ebrowse outline' to see its controls)",
                ExitCode.ACTION_FAILED,
            )
        if cover:
            return CommandError(
                f"blocked: {target} is covered by {cover}, which has no exposed ref "
                "(likely a new overlay) — run 'ebrowse outline' to re-read the page, or "
                "try 'ebrowse press Escape' / 'ebrowse screenshot'",
                ExitCode.ACTION_FAILED,
            )
        if info.get("disabledFieldset"):
            return CommandError(
                f"blocked: {target} is inside a disabled <fieldset> — the form section "
                "must be enabled first (look for a control that unlocks it)",
                ExitCode.ACTION_FAILED,
            )
        if info.get("pointerEvents") == "none":
            return CommandError(
                f"blocked: {target} has pointer-events: none — it never receives clicks; "
                "run 'ebrowse expand' on its section to find the visible control instead",
                ExitCode.ACTION_FAILED,
            )
        if info.get("inert") or dialog:
            what = dialog or "an inert region"
            return CommandError(
                f"blocked: {target} is blocked by {what} — resolve or dismiss it first "
                "(run 'ebrowse outline' to see it)",
                ExitCode.ACTION_FAILED,
            )
        return None

    # ------------------------------------------------------ fallback routes ----

    async def _click_via_label(self, loc) -> bool:
        """Click a form control's associated <label> instead of the control
        itself (browser semantics forward label activation to the control).
        Used when the control's own click point is covered by decoration inside
        its label — the label IS the visible click surface. Returns False to
        fall back to the normal click path."""
        try:
            handle = await loc.element_handle(timeout=2000)
            lab = await handle.evaluate_handle(
                "(el) => (el.labels && el.labels[0]) || (el.closest && el.closest('label'))"
            )
            el = lab.as_element()
            if el is None:
                return False
            await el.click(timeout=_ACTIVATE_TIMEOUT_MS)
            return True
        except Exception:
            return False

    async def _keyboard_activate(self, loc) -> bool:
        """Trusted keyboard activation for focusable controls: focus, VERIFY
        the focus landed on the target (a focus trap or inert region refusing
        it means stay blocked — fail closed), then press the element's native
        activation key. Web-platform semantics only: links/buttons/summary →
        Enter, checkbox/radio → Space; focusable ARIA button/link/checkbox/
        switch/radio widgets per the ARIA authoring practices. Anything else
        returns False — no guessing."""
        try:
            handle = await loc.element_handle(timeout=2000)
            key = await handle.evaluate(
                """(el) => {
                    const tag = el.tagName.toLowerCase();
                    if (tag === 'a') return el.getAttribute('href') ? 'Enter' : null;
                    if (tag === 'button' || tag === 'summary') return 'Enter';
                    if (tag === 'input') {
                        const t = (el.getAttribute('type') || 'text').toLowerCase();
                        if (['button', 'submit', 'reset', 'image'].includes(t)) return 'Enter';
                        if (['checkbox', 'radio'].includes(t)) return 'Space';
                    }
                    // ARIA widgets: keyboard operation is part of the contract,
                    // but only when the author made them focusable
                    if (el.tabIndex >= 0) {
                        const role = el.getAttribute('role');
                        if (role === 'button' || role === 'link') return 'Enter';
                        if (['checkbox', 'switch', 'radio', 'menuitemcheckbox',
                             'menuitemradio'].includes(role)) return 'Space';
                    }
                    return null;
                }"""
            )
            if not key:
                return False
            await handle.focus()
            focused = await handle.evaluate("(el) => el.getRootNode().activeElement === el")
            if not focused:
                return False
            await self.page.keyboard.press(key)
            return True
        except Exception:
            return False

    async def _keyboard_set_checked(self, loc, checked: bool) -> bool:
        """Keyboard route for check/uncheck under a non-modal cover. Space
        TOGGLES, so only press when the state must change, and verify the
        postcondition. Returns False when the route doesn't apply or failed."""
        try:
            if await loc.is_checked(timeout=2000) == checked:
                return True  # already in the desired state
            if not await self._keyboard_activate(loc):
                return False
            return await loc.is_checked(timeout=2000) == checked
        except Exception:
            return False
