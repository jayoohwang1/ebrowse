"""Action verbs: resolve -> pre-check -> act -> quiesce -> re-observe -> diff.

Mixed into Session. Every action returns the §4.3 diff rendering, never a full
snapshot. Playwright errors are mapped to actionable CommandErrors.
"""

from __future__ import annotations

import contextlib
import json
from urllib.parse import urldefrag

from ebrowse.core import render
from ebrowse.core.diff import diff_pages, navigation_diff
from ebrowse.core.locate import resolve
from ebrowse.errors import CommandError
from ebrowse.model import Element

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


class ActionsMixin:
    """Requires (from Session): page, page_mem, raw_by_sid, registry, cfg,
    nav_id, observe(), _ensure_browser(), _notes."""

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

    async def _element_for(self, target: str) -> tuple[Element | None, str]:
        """target -> (Element from current PageMem or None for CSS, description)."""
        if not target or not target.strip():
            raise CommandError("empty target — pass a @ref from expand output or a CSS selector", 2)
        if target.startswith("@"):
            if self.page_mem is None:
                raise CommandError("nothing observed yet — run 'ebrowse outline' first", 2)
            found = self.page_mem.find_element(target)
            if not found:
                raise CommandError(
                    f"stale ref {target}: not on the current page — run 'ebrowse outline'", 2
                )
            element = found[1]
            return element, f"{target} ({element.desc.short_desc()})"
        return None, f"css '{target}'"

    async def _locator_for(self, target: str):
        element, desc = await self._element_for(target)
        if element is None:
            loc = self.page.locator(target)
            n = await loc.count()
            if n == 0:
                raise CommandError(f"no element matches CSS '{target}'", 2)
            return loc.first, desc
        return await resolve(self.page, element.desc), desc

    async def _check_occlusion(self, loc, target: str) -> None:
        """Fail fast when another element would swallow the click (§4.3)."""
        try:
            handle = await loc.element_handle(timeout=2000)
            # handle.evaluate runs in the element's own frame — essential for
            # elements inside iframes, where main-frame elementFromPoint would
            # see only the <iframe> region and falsely report occlusion
            covering = await handle.evaluate(
                """(el) => {
                    const r = el.getBoundingClientRect();
                    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                    if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return null;
                    const t = document.elementFromPoint(cx, cy);
                    if (!t || el.contains(t) || t.contains(el)) return null;
                    const dlg = t.closest('[role=dialog],[role=alertdialog],dialog');
                    const name = (n) => n.tagName.toLowerCase()
                        + (n.id ? '#' + n.id : '')
                        + ((n.getAttribute('aria-label') || n.textContent || '')
                            .trim().slice(0, 40) ? ' "' + (n.getAttribute('aria-label')
                            || n.textContent).trim().slice(0, 40) + '"' : '');
                    return {covering: name(t), dialog: dlg ? name(dlg) : null};
                }"""
            )
        except Exception:
            return  # pre-check is best-effort; Playwright will still enforce
        if covering:
            what = covering.get("dialog") or covering.get("covering")
            raise CommandError(
                f"blocked: {target} is covered by {what} — interact with that first "
                "(run 'ebrowse outline' to see it)",
                1,
            )

    def _section_texts(self) -> dict[str, str]:
        return {
            sid: " ".join(n.subtree_text(cap=4000) for n in raw.all_nodes())
            for sid, raw in self.raw_by_sid.items()
        }

    def _begin_action(self) -> tuple:
        """Snapshot pre-action state; pairs with _finish_action."""
        prev = self.page_mem
        prev_texts = self._section_texts() if prev else {}
        prev_url = urldefrag(self.page.url)[0]
        self._notes = []
        return prev, prev_texts, prev_url

    async def _finish_action(self, action_line: str, begin_state: tuple) -> str:
        """Quiesce, re-observe, diff against the _begin_action snapshot."""
        prev, prev_texts, prev_url = begin_state
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        await self._quiesce()

        navigated = urldefrag(self.page.url)[0] != prev_url
        if navigated:
            self.nav_id += 1
        await self.observe()  # rebuilds self.page_mem / raw_by_sid

        if prev is None or navigated:
            diff = navigation_diff(prev, self.page_mem)
        else:
            diff = diff_pages(prev, self.page_mem, prev_texts, self._section_texts())
        diff.notes = list(self._notes)
        return render.render_diff(action_line, diff)

    async def _act(self, action_line: str, fn) -> str:
        """The action pipeline shared by every atomic verb. Compound verbs
        (compound.py) use _begin_action/_finish_action directly so several
        steps produce ONE diff."""
        await self._ensure_browser()
        begin_state = self._begin_action()
        try:
            await fn()
        except CommandError:
            raise
        except Exception as e:
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
            await self._check_occlusion(loc, target)
            kwargs: dict = {"timeout": _ACTION_TIMEOUT_MS}
            if right:
                kwargs["button"] = "right"
            if double:
                kwargs["click_count"] = 2
            if new_tab:
                kwargs["modifiers"] = ["ControlOrMeta"]
            await loc.click(**kwargs)

        verb = "DBLCLICK" if double else ("RIGHTCLICK" if right else "CLICK")
        return await self._act(f"{verb} {desc}", do)

    async def verb_fill(self, target: str, text: str) -> str:
        loc, desc = await self._locator_for(target)

        async def do() -> None:
            await loc.fill(text, timeout=_ACTION_TIMEOUT_MS)

        return await self._act(f'FILL {desc} = "{_clip(text)}"', do)

    async def verb_type(self, target: str, text: str, enter: bool = False) -> str:
        loc, desc = await self._locator_for(target)

        async def do() -> None:
            await loc.click(timeout=_ACTION_TIMEOUT_MS)  # focus without clearing
            await loc.press_sequentially(text, timeout=_ACTION_TIMEOUT_MS, delay=15)
            if enter:
                await loc.press("Enter", timeout=_ACTION_TIMEOUT_MS)

        suffix = " +Enter" if enter else ""
        return await self._act(f'TYPE {desc} "{_clip(text)}"{suffix}', do)

    async def verb_press(self, keys: str) -> str:
        async def do() -> None:
            await self.page.keyboard.press(keys)

        return await self._act(f"PRESS {keys}", do)

    async def verb_set_checked(self, target: str, checked: bool) -> str:
        loc, desc = await self._locator_for(target)

        async def do() -> None:
            await loc.set_checked(checked, timeout=_ACTION_TIMEOUT_MS)

        return await self._act(f"{'CHECK' if checked else 'UNCHECK'} {desc}", do)

    async def verb_select(self, target: str, value: str) -> str:
        element, desc = await self._element_for(target)
        if element is not None and element.desc.tag != "select":
            # custom dropdown: run the compound state machine (compound.py)
            return await self._select_custom(element, target, value)
        loc, _ = await self._locator_for(target)

        async def do() -> None:
            try:
                await loc.select_option(label=value, timeout=_ACTION_TIMEOUT_MS)
            except Exception:
                await loc.select_option(value=value, timeout=_ACTION_TIMEOUT_MS)

        return await self._act(f'SELECT {desc} = "{_clip(value)}"', do)

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
            bbox = None
            if direction.startswith("@"):
                element, desc = await self._element_for(direction)
                bbox = element.state.bbox if element else None
            else:
                if self.page_mem is None:
                    raise CommandError("run 'ebrowse outline' before scrolling to a section", 2)
                s = self.page_mem.section(direction)
                if s is None:
                    raise CommandError(f"no section '{direction}' — run 'ebrowse outline'", 2)
                bbox, desc = s.bbox, direction
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

        return await self._act(f"UPLOAD {desc} ← {len(files)} file(s)", do)

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
            1,
        )
    if "Timeout" in type(e).__name__ or "timeout" in msg.lower():
        return CommandError(
            f"action timed out: {msg} — the element may be hidden or detached; "
            "run 'ebrowse outline' and retry with a fresh ref",
            1,
        )
    if "not an <input>" in msg or "Element is not an" in msg:
        return CommandError(f"wrong element kind: {msg}", 2)
    return CommandError(f"action failed: {msg}", 1)
