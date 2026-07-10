"""Action verbs: resolve -> pre-check -> act -> quiesce -> re-observe -> diff.

Mixed into Session. Every action returns the diff rendering
(docs/output-contracts.md), never a full snapshot. Playwright errors are
mapped to actionable CommandErrors.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urldefrag

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ebrowse.config import Config
    from ebrowse.core.split import RawSection
    from ebrowse.model import Section

from ebrowse.core import render
from ebrowse.core.diff import diff_pages
from ebrowse.core.locate import resolve
from ebrowse.core.snapshot import probe_blocker
from ebrowse.errors import CommandError, ExitCode
from ebrowse.model import Element, PageMem

_ACTION_TIMEOUT_MS = 8_000
# Trial-click budget when the one-point hit test found a cover: long enough for
# transient overlays/animations to clear, short enough to fail fast on a real one.
_TRIAL_TIMEOUT_MS = 2_000

# Resolves when the DOM has been mutation-quiet for `quiet` ms (or `cap` ms
# elapsed). Installed per-wait; cheap enough and avoids init-script lifecycle.
_QUIESCE_JS = """
([quiet, cap]) => new Promise((resolve) => {
  let last = performance.now();
  const obs = new MutationObserver(() => { last = performance.now(); });
  obs.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  const start = performance.now();
  const iv = setInterval(() => {
    const now = performance.now();
    if (now - last >= quiet || now - start >= cap) {
      clearInterval(iv); obs.disconnect(); resolve(Math.round(now - start));
    }
  }, 40);
})
"""


@dataclass(slots=True)
class ActionSnapshot:
    """Pre-action state captured by _begin_action, consumed by _finish_action."""

    page: PageMem | None
    texts: dict[str, str] = field(default_factory=dict)
    url: str = ""  # fragment-stripped; basis for navigation detection


class ActionsMixin:
    """Verb mixin for Session. The block below is its typed contract: everything
    the host class must provide, declared so the checker verifies both sides."""

    if TYPE_CHECKING:
        from ebrowse.session import PendingDialog

        cfg: Config
        nav_id: int
        page_mem: PageMem | None
        raw_by_sid: dict[str, RawSection]
        _notes: list[str]
        _blocking_modal: str | None

        def _active_dialog(self) -> PendingDialog | None: ...
        @property
        def page(self) -> Page: ...
        async def observe(
            self, no_summaries: bool = ..., no_glance: bool = ..., preview: bool = ...
        ) -> str: ...
        async def _observe_page(self) -> None: ...
        async def _nav_landing(self, action_line: str) -> str: ...
        def _no_baseline_landing(self, action_line: str) -> str: ...
        async def _ensure_browser(self) -> None: ...
        def _require_page_mem(self) -> PageMem: ...
        def _get_section(self, sid: str) -> Section: ...
        # satisfied by CompoundMixin on the same host (custom-dropdown select)
        async def _select_custom(self, element: Element, target: str, value: str) -> str: ...

    # ------------------------------------------------------------ plumbing ----

    async def _quiesce(self) -> None:
        ob = self.cfg.observe
        with contextlib.suppress(Exception):
            # throws if a navigation destroys the execution context mid-wait;
            # in that case wait for the new document instead
            await self.page.evaluate(_QUIESCE_JS, [ob.quiescence_ms, ob.quiescence_max_ms])
            return
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("domcontentloaded", timeout=ob.quiescence_max_ms)
            await self.page.evaluate(_QUIESCE_JS, [ob.quiescence_ms, ob.quiescence_max_ms])

    def _element_for(self, target: str) -> tuple[Element | None, str]:
        """target -> (Element from current PageMem or None for CSS, description)."""
        if not target or not target.strip():
            raise CommandError(
                "empty target — pass a @ref from expand output or a CSS selector",
                ExitCode.USAGE,
            )
        if target.startswith("@"):
            if self.page_mem is None:
                raise CommandError(
                    "nothing observed yet — run 'ebrowse outline' first", ExitCode.USAGE
                )
            found = self.page_mem.find_element(target)
            if not found:
                raise CommandError(
                    f"stale ref {target}: not on the current page — run 'ebrowse outline'",
                    ExitCode.USAGE,
                )
            element = found[1]
            return element, f"{target} ({element.desc.short_desc()})"
        return None, f"css '{target}'"

    async def _locator_for(self, target: str):
        element, desc = self._element_for(target)
        if element is None:
            loc = self.page.locator(target)
            n = await loc.count()
            if n == 0:
                raise CommandError(f"no element matches CSS '{target}'", ExitCode.USAGE)
            return loc.first, desc
        return await resolve(self.page, element.desc), desc

    async def _check_occlusion(self, loc, target: str) -> dict:
        """Center-point hit test on the live target. Returns the probe result:
        {covering, coverDialog, coverInLabel} (any subset). Hard-fails ONLY when
        the cover sits inside a dialog — that is strong evidence the click can't
        mean what the agent intended. A generic cover is weak evidence (restyled
        controls, partial/transient overlays), so it is returned for the caller
        to arbitrate with a Playwright trial click rather than refused outright.

        Separately RECORDS a modal that blocks the page without covering the
        target — native `showModal()` (top layer + inert) or an aria-modal
        focus trap, where `::backdrop` is a pseudo-element invisible to the
        geometric hit-test. That can't be pre-empted safely (a false positive
        would block a valid click), so it's surfaced post-hoc only if the click
        then no-ops (see _finish_action). Best-effort; Playwright still enforces."""
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
        if info.get("modal"):
            self._blocking_modal = info["modal"]
        if info.get("coverDialog"):
            raise CommandError(
                f"blocked: {target} is covered by {info['coverDialog']} — interact with "
                "that first (run 'ebrowse outline' to see it)",
                ExitCode.ACTION_FAILED,
            )
        return info

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
            return {}
        if info.get("cover"):
            exposed = self._ref_for_chain(info.get("chain") or [])
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

    async def _arbitrate_cover(
        self, loc, target: str, cover: str, keyboard_fallback: bool = False
    ) -> bool:
        """A one-point hit-test mismatch is too weak for a hard refusal; let
        Playwright's actionability engine (scroll + stability + receives-events,
        with retries) arbitrate via a trial click. Sustained interception →
        blocked, with the failure diagnosis naming the recovery step.

        With keyboard_fallback, a natively focusable control blocked by a
        NON-modal cover is activated via trusted focus + key press instead —
        exactly what a keyboard user does when an overlay doesn't trap focus.
        Never used when a dialog/inert context is detected: keyboard input
        must not reach controls a modal is guarding. Returns True when the
        action completed via keyboard (caller must not click again)."""
        try:
            await loc.click(trial=True, timeout=_TRIAL_TIMEOUT_MS)
            return False
        except Exception as e:
            diag = await self._probe_diagnosis(loc)
            modal_context = diag.get("coverDialog") or diag.get("openDialog") or diag.get("inert")
            if keyboard_fallback and not modal_context and await self._keyboard_activate(loc):
                self._notes.append(
                    f"pointer route blocked by {cover}; activated via keyboard "
                    "(trusted focus + key press)"
                )
                return True
            raise self._blocked_error(diag, target) or CommandError(
                f"blocked: {target} is covered by {cover} — interact "
                "with that first (run 'ebrowse outline' to see it)",
                ExitCode.ACTION_FAILED,
            ) from e

    async def _keyboard_activate(self, loc) -> bool:
        """Trusted keyboard activation for natively focusable controls: focus,
        VERIFY the focus landed on the target (a focus trap or inert region
        refusing it means stay blocked — fail closed), then press the element's
        native activation key. Only web-platform activation semantics: links/
        buttons/summary → Enter, checkbox/radio → Space. Custom widgets return
        False — no guessing."""
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
            await el.click(timeout=_ACTION_TIMEOUT_MS)
            return True
        except Exception:
            return False

    def _section_texts(self) -> dict[str, str]:
        return {
            sid: " ".join(n.subtree_text(cap=4000) for n in raw.all_nodes())
            for sid, raw in self.raw_by_sid.items()
        }

    def _begin_action(self) -> ActionSnapshot:
        """Snapshot pre-action state; pairs with _finish_action."""
        prev = self.page_mem
        self._notes = []
        self._blocking_modal = None
        return ActionSnapshot(
            page=prev,
            texts=self._section_texts() if prev else {},
            url=urldefrag(self.page.url)[0],
        )

    async def _finish_action(self, action_line: str, begin_state: ActionSnapshot) -> str:
        """Quiesce, re-observe, diff against the _begin_action snapshot."""
        # A native confirm/prompt opened by this action blocks the renderer main
        # thread, so we cannot observe until it is resolved. Stash the action
        # context on the pending dialog (so 'dialog accept|dismiss' can emit this
        # action's diff) and return the blocking notice now.
        pending = self._active_dialog()
        if pending is not None:
            pending.action_line = action_line
            pending.begin_state = begin_state
            return render.render_dialog_pending(
                action_line, pending.type, pending.message, pending.default_value
            )
        prev = begin_state.page
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        await self._quiesce()

        navigated = urldefrag(self.page.url)[0] != begin_state.url
        if navigated:
            # New page: rebuild page_mem (keeps durable @refs live) but don't dump
            # a full outline — return a landing line, like the navigation verbs.
            self.nav_id += 1
            await self._observe_page()
            return await self._nav_landing(action_line)
        await self._observe_page()  # rebuilds self.page_mem / raw_by_sid
        new = self._require_page_mem()
        if prev is None:
            # acted (via CSS) before any outline: no baseline to diff against
            return self._no_baseline_landing(action_line)
        diff = diff_pages(prev, new, begin_state.texts, self._section_texts())
        diff.notes = list(self._notes)
        # A modal that blocks the page via the top layer / inert (rather than by
        # covering the target) lets the click dispatch to an inert element — a
        # harmless no-op. Name it so the agent doesn't retry the same dead click.
        if diff.kind == "no_change" and self._blocking_modal:
            diff.notes.append(
                f"a modal is open ({self._blocking_modal}) and is likely intercepting the "
                "click — interact with it or dismiss it before retrying"
            )
        return render.render_diff(action_line, diff, self.raw_by_sid, self.cfg.observe)

    async def _act(self, action_line: str, fn, loc=None, target: str = "") -> str:
        """The action pipeline shared by every atomic verb. Compound verbs
        (compound.py) use _begin_action/_finish_action directly so several
        steps produce ONE diff. When `loc`/`target` are given, a Playwright
        interception failure is enriched with the blocker diagnosis."""
        await self._ensure_browser()
        begin_state = self._begin_action()
        try:
            await fn()
        except CommandError:
            raise
        except Exception as e:
            # If the action opened a blocking confirm/prompt, the triggering call
            # itself can time out (the renderer is frozen). That timeout is
            # expected — report the dialog via _finish_action, not a failure.
            if self._active_dialog() is None:
                # A modal that blocks the page without covering the target (native
                # showModal / aria-modal+inert) makes Playwright time out rather
                # than click. The occlusion pre-check recorded which modal it is —
                # name it so the agent resolves it instead of retrying the dead click.
                if self._blocking_modal is not None:
                    raise CommandError(
                        f"blocked: a modal is open ({self._blocking_modal}) and is intercepting "
                        "the click — interact with it or dismiss it first",
                        ExitCode.ACTION_FAILED,
                    ) from e
                if loc is not None and "intercepts pointer events" in str(e):
                    diag = await self._probe_diagnosis(loc)
                    err = self._blocked_error(diag, target or action_line)
                    if err is not None:
                        raise err from e
                raise _map_playwright_error(e) from e
        return await self._finish_action(action_line, begin_state)

    # --------------------------------------------------------------- verbs ----

    async def verb_click(
        self, target: str, double: bool = False, right: bool = False, new_tab: bool = False
    ) -> str:
        loc, desc = await self._locator_for(target)

        async def do() -> None:
            with contextlib.suppress(Exception):
                await loc.scroll_into_view_if_needed(timeout=2000)
            info = await self._check_occlusion(loc, target)
            if info.get("coverInLabel") and not (double or right or new_tab):
                # the control's click point belongs to decoration inside its
                # associated label — click the label (standards-defined proxy)
                if await self._click_via_label(loc):
                    self._notes.append(
                        "clicked via the associated label (the control's visible surface)"
                    )
                    return
            elif info.get("covering"):
                done = await self._arbitrate_cover(
                    loc,
                    target,
                    info["covering"],
                    keyboard_fallback=not (double or right or new_tab),
                )
                if done:
                    return
            kwargs: dict = {"timeout": _ACTION_TIMEOUT_MS}
            if right:
                kwargs["button"] = "right"
            if double:
                kwargs["click_count"] = 2
            if new_tab:
                kwargs["modifiers"] = ["ControlOrMeta"]
            await loc.click(**kwargs)

        verb = "DBLCLICK" if double else ("RIGHTCLICK" if right else "CLICK")
        return await self._act(f"{verb} {desc}", do, loc=loc, target=target)

    async def verb_fill(self, target: str, text: str) -> str:
        loc, desc = await self._locator_for(target)

        async def do() -> None:
            await loc.fill(text, timeout=_ACTION_TIMEOUT_MS)

        return await self._act(f'FILL {desc} = "{_clip(text)}"', do, loc=loc, target=target)

    async def verb_type(self, target: str, text: str, enter: bool = False) -> str:
        loc, desc = await self._locator_for(target)

        async def do() -> None:
            await loc.click(timeout=_ACTION_TIMEOUT_MS)  # focus without clearing
            await loc.press_sequentially(text, timeout=_ACTION_TIMEOUT_MS, delay=15)
            if enter:
                await loc.press("Enter", timeout=_ACTION_TIMEOUT_MS)

        suffix = " +Enter" if enter else ""
        return await self._act(f'TYPE {desc} "{_clip(text)}"{suffix}', do, loc=loc, target=target)

    async def verb_press(self, keys: str) -> str:
        async def do() -> None:
            await self.page.keyboard.press(keys)

        return await self._act(f"PRESS {keys}", do)

    async def verb_set_checked(self, target: str, checked: bool) -> str:
        loc, desc = await self._locator_for(target)
        want = "checked" if checked else "unchecked"

        async def do() -> None:
            with contextlib.suppress(Exception):
                await loc.scroll_into_view_if_needed(timeout=2000)
            info = await self._check_occlusion(loc, target)
            if info.get("coverInLabel"):
                # restyled checkbox/radio: the label is the click surface. Label
                # activation TOGGLES, so only click when the state must change,
                # and verify the postcondition (set_checked can't — it would
                # aim at the covered input).
                try:
                    if await loc.is_checked(timeout=2000) == checked:
                        return  # already in the desired state; diff says no change
                except Exception:
                    pass  # state unreadable; fall through to the normal path
                else:
                    if await self._click_via_label(loc):
                        if await loc.is_checked(timeout=2000) == checked:
                            self._notes.append(
                                f"{want} via the associated label (the control's visible surface)"
                            )
                            return
                        raise CommandError(
                            f"could not set {target} to {want}: the label click did not "
                            "change its state — the control may be custom-wired; run "
                            "'ebrowse outline' and check the diff",
                            ExitCode.ACTION_FAILED,
                        )
            elif info.get("covering"):
                await self._arbitrate_cover(loc, target, info["covering"])
            await loc.set_checked(checked, timeout=_ACTION_TIMEOUT_MS)

        return await self._act(
            f"{'CHECK' if checked else 'UNCHECK'} {desc}", do, loc=loc, target=target
        )

    async def verb_select(self, target: str, value: str) -> str:
        element, desc = self._element_for(target)
        if element is not None and element.desc.tag != "select":
            # custom dropdown: run the compound state machine (compound.py)
            return await self._select_custom(element, target, value)
        loc, _ = await self._locator_for(target)

        async def do() -> None:
            try:
                await loc.select_option(label=value, timeout=_ACTION_TIMEOUT_MS)
            except Exception:
                await loc.select_option(value=value, timeout=_ACTION_TIMEOUT_MS)

        return await self._act(f'SELECT {desc} = "{_clip(value)}"', do, loc=loc, target=target)

    async def verb_scroll(self, direction: str, pages: int = 1) -> str:
        await self._ensure_browser()
        vh = (self.cfg.browser.viewport + [1280, 1280])[1]

        if direction in ("down", "up"):
            dy = vh * pages * (1 if direction == "down" else -1)

            async def do() -> None:
                await self.page.evaluate("(dy) => window.scrollBy(0, dy)", dy)

            line = f"SCROLL {direction} {pages} page(s)"
        else:
            # scroll to a section or element
            if direction.startswith("@"):
                element, desc = self._element_for(direction)
                assert element is not None  # _element_for never returns None for @refs
                bbox = element.state.bbox
            else:
                bbox, desc = self._get_section(direction).bbox, direction
            y = max(0, int(bbox.y) - 80)

            async def do() -> None:
                await self.page.evaluate("(y) => window.scrollTo(0, y)", y)

            line = f"SCROLL to {desc}"

        result = await self._act(line, do)
        pos = await self.page.evaluate("() => Math.round(window.scrollY)")
        visible = self._sections_in_view(pos, vh)
        loc_line = f"scroll position y={pos}" + (f" — viewport over {visible}" if visible else "")
        return f"{result}\n{loc_line}"

    def _sections_in_view(self, scroll_y: int, vh: int) -> str:
        if not self.page_mem:
            return ""
        vis = [
            s.sid
            for s in self.page_mem.sections
            if s.bbox.y < scroll_y + vh and s.bbox.y + s.bbox.height > scroll_y
        ]
        return ", ".join(vis[:8])

    async def verb_upload(self, target: str, files: list[str]) -> str:
        loc, desc = await self._locator_for(target)

        async def do() -> None:
            await loc.set_input_files(files, timeout=_ACTION_TIMEOUT_MS)

        return await self._act(f"UPLOAD {desc} ← {len(files)} file(s)", do, loc=loc, target=target)

    async def verb_diagnose(self, target: str) -> str:
        """Read-only actionability report for a target: Playwright trial-click
        verdict + blocker classification, without dispatching anything. The
        trial may scroll the target into view; the page is otherwise untouched."""
        await self._ensure_browser()
        loc, desc = await self._locator_for(target)
        lines = [f"DIAGNOSE {desc}"]
        try:
            await loc.click(trial=True, timeout=_TRIAL_TIMEOUT_MS)
            trial_error = None
        except Exception as e:
            trial_error = str(e).splitlines()[0][:200]
        diag = await self._probe_diagnosis(loc)
        if trial_error is None:
            lines.append("actionability: PASS — a normal click should succeed")
            if diag.get("coverInLabel"):
                lines.append("note: click point is label decoration (label activation applies)")
        elif diag.get("coverInLabel"):
            # the pointer trial fails on the decoration, but click/check route
            # through the associated label, so the action will succeed
            lines.append(
                "actionability: PASS — click point is label decoration; actions are "
                "routed via the associated label"
            )
        else:
            err = self._blocked_error(diag, target)
            reason = str(err) if err else f"trial click failed: {trial_error}"
            lines.append(f"actionability: BLOCKED — {reason}")
        facts = []
        if diag.get("disabledFieldset"):
            facts.append("inside a disabled <fieldset>")
        if diag.get("pointerEvents") == "none":
            facts.append("pointer-events: none")
        if diag.get("inert"):
            facts.append("inside an inert region")
        if diag.get("openDialog") and trial_error is None:
            facts.append(f"a dialog is open elsewhere: {diag['openDialog']}")
        if facts:
            lines.append("state: " + "; ".join(facts))
        return "\n".join(lines)

    async def verb_eval(self, js: str) -> str:
        await self._ensure_browser()
        result_box: list = []

        async def do() -> None:
            result_box.append(await self.page.evaluate(js))

        diff_text = await self._act(f"EVAL {_clip(js, 60)}", do)
        try:
            rendered = json.dumps(result_box[0], default=str)[:2000]
        except Exception:
            rendered = str(result_box[0])[:2000]
        return f"result: {rendered}\n{diff_text}"


def _clip(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _map_playwright_error(e: Exception) -> CommandError:
    msg = str(e).splitlines()[0][:300]
    if "intercepts pointer events" in str(e):
        return CommandError(
            f"click blocked: another element intercepts the pointer ({msg}) — "
            "an overlay or dialog is probably open; run 'ebrowse outline'",
            ExitCode.ACTION_FAILED,
        )
    if "Timeout" in type(e).__name__ or "timeout" in msg.lower():
        return CommandError(
            f"action timed out: {msg} — the element may be hidden or detached; "
            "run 'ebrowse outline' and retry with a fresh ref",
            ExitCode.ACTION_FAILED,
        )
    if "not an <input>" in msg or "Element is not an" in msg:
        return CommandError(f"wrong element kind: {msg}", ExitCode.USAGE)
    return CommandError(f"action failed: {msg}", ExitCode.ACTION_FAILED)
