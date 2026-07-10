"""Action verbs: resolve -> pre-check -> act -> quiesce -> re-observe -> diff.

Mixed into Session. Every action returns the diff rendering
(docs/output-contracts.md), never a full snapshot. Playwright errors are
mapped to actionable CommandErrors.
"""

from __future__ import annotations

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
from ebrowse.errors import CommandError, ExitCode
from ebrowse.interaction import _TRIAL_TIMEOUT_MS, InteractionMixin
from ebrowse.model import Element, PageMem

_ACTION_TIMEOUT_MS = 8_000

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
    full_url: str = ""  # with fragment; basis for anchor-jump detection
    scroll_y: int = 0
    navs: int = 0  # session main-frame navigation count at begin


class ActionsMixin(InteractionMixin):
    """Verb mixin for Session (pointer planning/fallbacks live in
    InteractionMixin). The block below is its typed contract: everything
    the host class must provide, declared so the checker verifies both sides."""

    if TYPE_CHECKING:
        from ebrowse.session import PendingDialog

        cfg: Config
        nav_id: int
        nav_events: int
        page_mem: PageMem | None
        raw_by_sid: dict[str, RawSection]
        _notes: list[str]
        _blocking_modal: str | None
        _hover_delivery_suspect: bool

        def _active_dialog(self) -> PendingDialog | None: ...
        @property
        def page(self) -> Page: ...
        async def observe(
            self, no_summaries: bool = ..., no_glance: bool = ..., preview: bool = ...
        ) -> str: ...
        async def _observe_page(self) -> None: ...
        async def _nav_landing(self, action_line: str) -> str: ...
        def _no_baseline_landing(self, action_line: str) -> str: ...
        def _expanded_now(self) -> set[str]: ...
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
        return await resolve(self.page, element.desc, ref=element.ref), desc

    def _section_texts(self) -> dict[str, str]:
        # per-node cap must be able to carry diff.EXPANDED_TEXT_BUDGET worth of
        # text, or a single-node section could never fill the expanded quote budget
        return {
            sid: " ".join(n.subtree_text(cap=8000) for n in raw.all_nodes())
            for sid, raw in self.raw_by_sid.items()
        }

    async def _begin_action(self) -> ActionSnapshot:
        """Snapshot pre-action state; pairs with _finish_action."""
        prev = self.page_mem
        self._notes = []
        self._blocking_modal = None
        self._hover_delivery_suspect = False
        scroll_y = 0
        with contextlib.suppress(Exception):
            scroll_y = int(await self.page.evaluate("() => Math.round(window.scrollY)"))
        return ActionSnapshot(
            page=prev,
            texts=self._section_texts() if prev else {},
            url=urldefrag(self.page.url)[0],
            full_url=self.page.url,
            scroll_y=scroll_y,
            navs=self.nav_events,
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
        diff = diff_pages(prev, new, begin_state.texts, self._section_texts(), self._expanded_now())
        diff.notes = list(self._notes)
        if diff.kind == "no_change" and self._hover_delivery_suspect:
            diff.notes.append(
                "hover dispatched but the target is not :hover and no page change was detected; "
                "browser input delivery may be degraded — run 'ebrowse daemon stop' and retry"
            )
        # Outcome evidence the DOM diff can't see: a same-URL reload (form
        # resubmit, meta refresh) and — only when nothing else changed —
        # anchor jumps and scroll movement, so a dispatched click isn't
        # misreported as a dead no-op.
        # (framenavigated also fires for same-document anchor jumps, so a
        # fragment difference means "anchor", not "reload")
        if self.nav_events > begin_state.navs and self.page.url == begin_state.full_url:
            diff.notes.append("the document reloaded (same URL) — page state may have reset")
        if diff.kind == "no_change":
            if self.page.url != begin_state.full_url:
                diff.notes.append(f"URL fragment changed: now at {self.page.url}")
            else:
                with contextlib.suppress(Exception):
                    y = int(await self.page.evaluate("() => Math.round(window.scrollY)"))
                    if abs(y - begin_state.scroll_y) > 40:
                        diff.notes.append(
                            f"scroll position moved y={begin_state.scroll_y} → {y} "
                            "(likely an in-page jump)"
                        )
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
        begin_state = await self._begin_action()
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
            plain = not (double or right or new_tab)
            plan = await self._plan_pointer(loc, target, plain=plain)
            if plan.route == "label":
                # the control's click point belongs to decoration inside its
                # associated label — click the label (standards-defined proxy)
                if await self._click_via_label(loc):
                    self._notes.append(
                        "clicked via the associated label (the control's visible surface)"
                    )
                    return
            elif plan.route == "obstructed":
                if await self._keyboard_activate(loc):
                    self._notes.append(
                        f"pointer route blocked by {plan.cover}; activated via keyboard "
                        "(trusted focus + key press)"
                    )
                    return
                assert plan.blocked is not None
                raise plan.blocked
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
            # the click is only there to focus without clearing — so a blocked
            # pointer route can simply be skipped (press_sequentially focuses
            # the element itself, no pointer involved)
            plan = await self._plan_pointer(loc, target)
            if plan.route == "label":
                if not await self._click_via_label(loc):
                    await loc.click(timeout=_ACTION_TIMEOUT_MS)
            elif plan.route == "obstructed":
                self._notes.append(f"pointer route blocked by {plan.cover}; typed via direct focus")
            else:
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

    _ARIA_CHECKABLE_ROLES = ("checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio")

    async def verb_set_checked(self, target: str, checked: bool) -> str:
        element, _ = self._element_for(target)
        loc, desc = await self._locator_for(target)
        want = "checked" if checked else "unchecked"
        action_line = f"{'CHECK' if checked else 'UNCHECK'} {desc}"

        # ARIA widget (role checkbox/radio/switch on a non-native element):
        # Playwright's set_checked() refuses these, but the ARIA contract is
        # activate + observe aria-checked — click (with the full plan) and
        # verify the postcondition.
        if element is not None:
            role, tag = element.desc.role, element.desc.tag
        else:  # CSS target: no cached descriptor; read the live element
            role, tag = None, "input"
            with contextlib.suppress(Exception):
                role = await loc.get_attribute("role", timeout=1000)
                tag = await loc.evaluate("(el) => el.tagName.toLowerCase()")
        if tag != "input" and role in self._ARIA_CHECKABLE_ROLES:

            async def do_aria() -> None:
                state = await loc.get_attribute("aria-checked", timeout=2000)
                if (state == "true") == checked:
                    return  # already in the desired state; diff says no change
                if not checked and role in ("radio", "menuitemradio"):
                    raise CommandError(
                        f"a radio cannot be unchecked directly — check another radio "
                        f"in the group instead of unchecking {target}",
                        ExitCode.USAGE,
                    )
                plan = await self._plan_pointer(loc, target)
                activated = False
                if plan.route == "label":
                    activated = await self._click_via_label(loc)
                elif plan.route == "obstructed":
                    if not await self._keyboard_activate(loc):
                        assert plan.blocked is not None
                        raise plan.blocked
                    activated = True
                    self._notes.append(
                        f"pointer route blocked by {plan.cover}; activated via keyboard"
                    )
                if not activated:
                    await loc.click(timeout=_ACTION_TIMEOUT_MS)
                new = await loc.get_attribute("aria-checked", timeout=2000)
                if (new == "true") != checked:
                    raise CommandError(
                        f"could not set {target} to {want}: aria-checked did not change — "
                        "the widget may need a different interaction; run 'ebrowse outline'",
                        ExitCode.ACTION_FAILED,
                    )

            return await self._act(action_line, do_aria, loc=loc, target=target)

        async def do() -> None:
            plan = await self._plan_pointer(loc, target)
            if plan.route == "label":
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
            elif plan.route == "obstructed":
                if await self._keyboard_set_checked(loc, checked):
                    self._notes.append(
                        f"pointer route blocked by {plan.cover}; {want} via keyboard "
                        "(trusted focus + Space, state verified)"
                    )
                    return
                assert plan.blocked is not None
                raise plan.blocked
            await loc.set_checked(checked, timeout=_ACTION_TIMEOUT_MS)

        return await self._act(action_line, do, loc=loc, target=target)

    async def verb_hover(self, target: str) -> str:
        """Hover the pointer over a target. The mouse STAYS there afterwards,
        so hover-revealed menus remain open in the re-observe — revealed items
        appear in the diff with fresh refs; click one next."""
        loc, desc = await self._locator_for(target)

        async def do() -> None:
            with contextlib.suppress(Exception):
                await loc.scroll_into_view_if_needed(timeout=2000)
            await loc.hover(timeout=_ACTION_TIMEOUT_MS)
            # A successful Playwright hover should leave the live target under
            # the pointer. Treat a contrary DOM fact as a canary only: target
            # replacement/overlays can be legitimate, so _finish_action warns
            # solely when the page also reports no observable change.
            with contextlib.suppress(Exception):
                self._hover_delivery_suspect = not bool(
                    await loc.evaluate("(el) => el.matches(':hover')")
                )

        return await self._act(f"HOVER {desc}", do, loc=loc, target=target)

    async def verb_drag(self, source: str, target: str) -> str:
        """Drag source onto target (Playwright drag_to: real pointer sequence,
        works for HTML5 draggable and mouse-based sortables)."""
        src, sdesc = await self._locator_for(source)
        dst, ddesc = await self._locator_for(target)

        async def do() -> None:
            with contextlib.suppress(Exception):
                await src.scroll_into_view_if_needed(timeout=2000)
            await src.drag_to(dst, timeout=_ACTION_TIMEOUT_MS)

        return await self._act(f"DRAG {sdesc} → {ddesc}", do, loc=src, target=source)

    async def verb_select(self, target: str, values: list[str]) -> str:
        element, desc = self._element_for(target)
        if element is not None and element.desc.tag != "select":
            if len(values) > 1:
                raise CommandError(
                    "multiple values need a native <select multiple>; "
                    f"{target} is a custom dropdown — select one value",
                    ExitCode.USAGE,
                )
            # custom dropdown: run the compound state machine (compound.py)
            return await self._select_custom(element, target, values[0])
        loc, _ = await self._locator_for(target)

        async def do() -> None:
            if len(values) > 1:
                multi = False
                with contextlib.suppress(Exception):
                    multi = await loc.evaluate("(el) => el.multiple === true")
                if not multi:
                    raise CommandError(
                        f"{target} is a single-choice <select> — pass exactly one value",
                        ExitCode.USAGE,
                    )
            try:
                await loc.select_option(label=values, timeout=_ACTION_TIMEOUT_MS)
            except CommandError:
                raise
            except Exception:
                await loc.select_option(value=values, timeout=_ACTION_TIMEOUT_MS)

        shown = ", ".join(values)
        return await self._act(f'SELECT {desc} = "{_clip(shown)}"', do, loc=loc, target=target)

    async def verb_scroll(self, direction: str, pages: int = 1, inner: str | None = None) -> str:
        await self._ensure_browser()
        vh = (self.cfg.browser.viewport + [1280, 1280])[1]

        if direction not in ("down", "up") and inner in ("down", "up"):
            # nested scrolling: scroll INSIDE the scrollable container at/above
            # the target (inner panels, virtualized lists, modal bodies)
            return await self._scroll_container(direction, inner, pages)
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

    # Shared by the @ref and section entry points below: find the nearest real
    # scroll container (self or composed ancestor; body/html scroll via the
    # window) and scroll it by clientHeight * signed pages, deterministically.
    _SCROLL_CONTAINER_HELPERS = """
        const scrollable = (n) => {
            if (!n || n === document.body || n === document.documentElement) return false;
            const s = getComputedStyle(n);
            return (s.overflowY === 'auto' || s.overflowY === 'scroll')
                && n.scrollHeight > n.clientHeight + 4;
        };
        const up = (n) => n.parentElement
            || (n.getRootNode() instanceof ShadowRoot ? n.getRootNode().host : null);
        const doScroll = (start, pagesSigned) => {
            let c = start;
            while (c && !scrollable(c)) c = up(c);
            if (!c) return null;
            const before = Math.round(c.scrollTop);
            c.scrollTop = before + c.clientHeight * pagesSigned;
            const after = Math.round(c.scrollTop);
            const max = Math.round(c.scrollHeight - c.clientHeight);
            return {name: c.tagName.toLowerCase() + (c.id ? '#' + c.id : ''),
                    before, after, max};
        };
    """

    async def _scroll_container(self, target: str, inner: str, pages: int) -> str:
        signed = pages * (1 if inner == "down" else -1)
        result_box: list = []

        if target.startswith("@"):
            loc, desc = await self._locator_for(target)

            async def do() -> None:
                handle = await loc.element_handle(timeout=2000)
                js = "(el, [p]) => {" + self._SCROLL_CONTAINER_HELPERS + "return doScroll(el, p);}"
                result_box.append(await handle.evaluate(js, [signed]))
        else:
            section = self._get_section(target)
            bbox, desc = section.bbox, target

            async def do() -> None:
                # resolve the section to a live container geometrically
                # (sections have no locators): hit-test several points in its
                # box until one sits inside a scroll container
                js = (
                    "([x, y, h, p]) => {"
                    + self._SCROLL_CONTAINER_HELPERS
                    + """
                    if (y < scrollY || y > scrollY + innerHeight) {
                        window.scrollTo(0, Math.max(0, y - innerHeight / 3));
                    }
                    const cx = Math.min(Math.max(0, x - scrollX), innerWidth - 1);
                    for (const f of [0.5, 0.33, 0.66, 0.15, 0.85]) {
                        const cy = Math.min(Math.max(0, y + h * f - scrollY), innerHeight - 1);
                        const r = doScroll(document.elementFromPoint(cx, cy), p);
                        if (r) return r;
                    }
                    return null;}"""
                )
                args = [bbox.x + bbox.width / 2, bbox.y, bbox.height, signed]
                result_box.append(await self.page.evaluate(js, args))

        line = f"SCROLL {desc} {inner} {pages} page(s) (inner container)"
        result = await self._act(line, do)
        info = result_box[0] if result_box else None
        if info is None:
            raise CommandError(
                f"no scrollable container at or above {desc} — for the page itself use "
                f"'ebrowse scroll {inner}'",
                ExitCode.USAGE,
            )
        edge = ""
        if inner == "down" and info["after"] >= info["max"]:
            edge = " — at the bottom"
        elif inner == "up" and info["after"] <= 0:
            edge = " — at the top"
        pos = f"container {info['name']} scroll y={info['after']}/{info['max']}{edge}"
        return f"{result}\n{pos}"

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
