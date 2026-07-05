"""Compound verbs: multi-step interactions as single commands (ROADMAP R1).

Deterministic state machines over the same act→diff machinery as atomic verbs
(adapted from WebChallenger submit_form / dropdown_action / search workflows).
Zero LLM. Ambiguity or no-match degrades to an actionable error listing the
choices — never a silent guess. Several internal steps produce ONE final diff.
"""

from __future__ import annotations

import contextlib
import json

from ebrowse.actions import _map_playwright_error
from ebrowse.core.diff import diff_pages
from ebrowse.core.locate import resolve
from ebrowse.errors import CommandError
from ebrowse.model import Element, PageMem, Section

_TEXTY_INPUTS = {
    "text", "email", "password", "tel", "url", "number", "date", "time",
    "datetime-local", "month", "week", "textarea",
}  # fmt: skip


def _match_rank(needle: str, hay: str | None) -> int:
    """3 exact, 2 prefix, 1 substring, 0 no match (casefolded)."""
    if not hay:
        return 0
    n, h = needle.casefold().strip(), hay.casefold().strip()
    if not n:
        return 0
    if n == h:
        return 3
    if h.startswith(n):
        return 2
    if n in h:
        return 1
    return 0


def _element_rank(needle: str, e: Element) -> int:
    return max(
        _match_rank(needle, e.desc.name),
        _match_rank(needle, e.desc.placeholder),
        _match_rank(needle, e.desc.text_head),
    )


# NOTE: no "combobox" here — a combobox (incl. native <select>, implicit role)
# is a leaf trigger the agent acts on, not a container of visible items.
# Dropping it silently broke fill-form's Country match.
_CONTAINER_ROLES = {"listbox", "menu", "list", "radiogroup", "group", "tree"}


def _prefer_leaves(pool: list[Element]) -> list[Element]:
    """Drop container elements (a listbox's text contains every option, so it
    substring-matches anything — clicking its center hits an arbitrary item;
    seen on recreation.gov suggestions). Keep containers only if that's all
    there is."""
    leaves = [e for e in pool if (e.desc.role or "") not in _CONTAINER_ROLES]
    return leaves or pool


def _best_matches(needle: str, pool: list[Element]) -> list[Element]:
    """All elements sharing the top nonzero rank for `needle` (leaves first)."""
    ranked = [(e, _element_rank(needle, e)) for e in _prefer_leaves(pool)]
    top = max((r for _e, r in ranked), default=0)
    if top == 0:
        return []
    return [e for e, r in ranked if r == top]


def _describe_pool(pool: list[Element], limit: int = 15) -> str:
    names = []
    for e in pool[:limit]:
        label = e.desc.name or e.desc.placeholder or e.desc.text_head
        if label:
            names.append(label[:40])
    more = f", … +{len(pool) - limit} more" if len(pool) > limit else ""
    return ", ".join(names) + more


class CompoundMixin:
    """Requires ActionsMixin (same host class: Session)."""

    # ------------------------------------------------------------- select ----

    async def _select_custom(self, element: Element, target: str, value: str) -> str:
        """Custom-dropdown machine: click trigger → diff → match option → click."""
        await self._ensure_browser()
        begin_state = self._begin_action()
        steps: list[str] = [f'SELECT {target} ({element.desc.short_desc()}) = "{value}"']

        # step 1: open the dropdown
        loc = await resolve(self.page, element.desc)
        with contextlib.suppress(Exception):
            await loc.scroll_into_view_if_needed(timeout=2000)
        try:
            await loc.click(timeout=5000)
        except Exception as e:
            raise _map_playwright_error(e) from e
        await self._quiesce()

        # step 2: what did it reveal?
        prev = begin_state[0]
        await self.observe(no_summaries=True)
        mid = diff_pages(prev, self.page_mem) if prev else None
        revealed: list[Element] = []
        if mid:
            for sd in mid.sections:
                revealed.extend(sd.added)
                if sd.kind == "appeared" and sd.section:
                    revealed.extend(sd.section.elements)
        if not revealed:
            steps.append("  ✗ clicking it revealed no options — is it a dropdown?")
            final = await self._finish_action("\n".join(steps), begin_state)
            return final

        # step 3: match the option
        matches = _best_matches(value, revealed)
        if not matches:
            # close it back if possible (Escape is harmless when not applicable)
            with contextlib.suppress(Exception):
                await self.page.keyboard.press("Escape")
            raise CommandError(
                f'no option matching "{value}" — revealed: {_describe_pool(revealed)}',
                2,
            )
        if len(matches) > 1:
            raise CommandError(
                f'"{value}" is ambiguous among: {_describe_pool(matches)} — '
                "use the exact option text",
                2,
            )
        option = matches[0]
        steps.append(f"  ✓ opened, {len(revealed)} options revealed")

        # step 4: click the option
        opt_loc = await resolve(self.page, option.desc)
        try:
            await opt_loc.click(timeout=5000)
        except Exception as e:
            raise _map_playwright_error(e) from e
        steps.append(f"  ✓ picked {option.desc.short_desc()}")
        return await self._finish_action("\n".join(steps), begin_state)

    # ---------------------------------------------------------- fill-form ----

    async def verb_fill_form(self, sid: str, data_json: str) -> str:
        try:
            data = json.loads(data_json)
        except json.JSONDecodeError as e:
            raise CommandError(f"--data is not valid JSON: {e}", 2) from e
        if not isinstance(data, dict) or not data:
            raise CommandError('--data must be a non-empty JSON object {"field": value}', 2)

        page_mem = self._require_page_mem()
        section = page_mem.section(sid)
        if section is None:
            sids = ", ".join(s.sid for s in page_mem.sections)
            raise CommandError(f"no section '{sid}' (have: {sids}) — run 'ebrowse outline'", 2)

        controls = [
            e
            for e in section.elements
            if e.desc.tag in ("input", "textarea", "select") or e.desc.role == "combobox"
            or (e.desc.tag == "button" and e.state.expanded is not None)
        ]  # fmt: skip
        if not controls:
            raise CommandError(f"{sid} has no form controls — expand it to check", 2)

        await self._ensure_browser()
        begin_state = self._begin_action()
        steps: list[str] = [f"FILL-FORM {sid} ({len(data)} fields)"]
        filled = 0
        for key, value in data.items():
            try:
                line = await self._fill_one(section, controls, key, value)
                steps.append(f"  ✓ {line}")
                filled += 1
            except CommandError as e:
                steps.append(f"  ✗ {key} — {e}")
            except Exception as e:  # playwright errors: keep going, report
                steps.append(f"  ✗ {key} — {str(e).splitlines()[0][:120]}")

        if filled == 0:
            raise CommandError(
                f"no fields matched. Available: {_describe_pool(controls)}",
                2,
            )
        return await self._finish_action("\n".join(steps), begin_state)

    async def _fill_one(self, section: Section, controls: list[Element], key, value) -> str:
        # radio groups: match the *option label*, not the field key
        if isinstance(value, str):
            radios = [e for e in controls if e.desc.input_type == "radio"]
            radio_hit = next(
                (e for e in radios if _element_rank(value, e) >= 2 and _element_rank(key, e) >= 1),
                None,
            ) or next((e for e in radios if _element_rank(value, e) >= 2), None)
        else:
            radio_hit = None

        matches = _best_matches(str(key), controls)
        if radio_hit is not None and (not matches or matches[0].desc.input_type == "radio"):
            loc = await resolve(self.page, radio_hit.desc)
            await loc.set_checked(True, timeout=5000)
            return f'{key} = "{value}" (radio)'
        if not matches:
            raise CommandError(f"no matching field (have: {_describe_pool(controls)})", 2)
        if len(matches) > 1:
            raise CommandError(f"ambiguous among: {_describe_pool(matches)}", 2)
        el = matches[0]
        d = el.desc

        if isinstance(value, bool):
            if d.input_type not in ("checkbox", "radio"):
                raise CommandError(f"{d.short_desc()} is not a checkbox", 2)
            loc = await resolve(self.page, d)
            await loc.set_checked(value, timeout=5000)
            return f"{key} = {str(value).lower()}"

        value = str(value)
        if d.tag == "select":
            loc = await resolve(self.page, d)
            try:
                await loc.select_option(label=value, timeout=5000)
            except Exception:
                await loc.select_option(value=value, timeout=5000)
            return f'{key} = "{value}" (native select)'
        if d.tag == "input" and d.input_type == "checkbox":
            loc = await resolve(self.page, d)
            await loc.set_checked(value.casefold() in ("true", "yes", "on", "1"), timeout=5000)
            return f"{key} = {value}"
        if (d.tag in ("input", "textarea") and (d.input_type or "text") in _TEXTY_INPUTS) or (
            d.input_type == "search"
        ):
            loc = await resolve(self.page, d)
            await loc.fill(value, timeout=5000)
            return f'{key} = "{value}"'
        if d.tag == "button" or d.role == "combobox":
            # custom dropdown trigger: delegate to the select machine mid-form.
            # It runs its own observe; our final diff still covers everything.
            await self._select_custom_inline(el, value)
            return f'{key} = "{value}" (dropdown)'
        raise CommandError(f"don't know how to fill {d.short_desc()}", 2)

    async def _select_custom_inline(self, element: Element, value: str) -> None:
        """Open→match→click without emitting its own diff (used by fill-form)."""
        loc = await resolve(self.page, element.desc)
        prev = self.page_mem
        await loc.click(timeout=5000)
        await self._quiesce()
        await self.observe(no_summaries=True)
        revealed: list[Element] = []
        for sd in diff_pages(prev, self.page_mem).sections:
            revealed.extend(sd.added)
            if sd.kind == "appeared" and sd.section:
                revealed.extend(sd.section.elements)
        matches = _best_matches(value, revealed)
        if len(matches) != 1:
            with contextlib.suppress(Exception):
                await self.page.keyboard.press("Escape")
            pool = _describe_pool(matches or revealed)
            word = "ambiguous among" if matches else "no option matching value; revealed"
            raise CommandError(f"{word}: {pool}", 2)
        opt_loc = await resolve(self.page, matches[0].desc)
        await opt_loc.click(timeout=5000)

    # -------------------------------------------------------------- search ----

    _SEARCH_WORDS = ("search", "find", "query", "look up")

    def _find_search_box(self, page_mem: PageMem) -> Element:
        boxes: list[tuple[int, Element]] = []
        for s in page_mem.sections:
            for e in s.elements:
                d = e.desc
                if d.tag not in ("input", "textarea"):
                    continue
                if d.input_type == "search" or d.role == "searchbox":
                    boxes.append((2, e))
                elif any(
                    w in (d.placeholder or "").casefold() or w in (d.name or "").casefold()
                    for w in self._SEARCH_WORDS
                ):
                    boxes.append((1, e))
        if not boxes:
            raise CommandError(
                "no search box found on this page — pass one explicitly: "
                "ebrowse search <query> --in @ref",
                2,
            )
        top = max(r for r, _e in boxes)
        best = [e for r, e in boxes if r == top]
        if len(best) > 1:
            names = ", ".join(f"{e.ref} ({e.desc.short_desc()})" for e in best[:6])
            raise CommandError(f"multiple search boxes: {names} — pick one with --in", 2)
        return best[0]

    async def verb_search(
        self,
        query: str,
        target: str | None = None,
        pick: str | None = None,
        no_submit: bool = False,
    ) -> str:
        await self._ensure_browser()
        page_mem = self._require_page_mem()
        if target:
            found = page_mem.find_element(target) if target.startswith("@") else None
            if target.startswith("@") and not found:
                raise CommandError(f"stale ref {target} — run 'ebrowse outline'", 2)
            box = found[1] if found else None
        else:
            box = self._find_search_box(page_mem)

        begin_state = self._begin_action()
        steps = [f'SEARCH "{query}"']

        loc = (
            await resolve(self.page, box.desc)
            if box
            else self.page.locator(target).first  # CSS --in
        )
        try:
            await loc.click(timeout=5000)
            await loc.fill(query, timeout=5000)
        except Exception as e:
            raise _map_playwright_error(e) from e
        steps.append(f"  ✓ typed into {box.ref if box else target} "
                     f"({box.desc.short_desc() if box else 'css'})")  # fmt: skip
        await self._quiesce()

        # suggestions?
        prev = begin_state[0]
        await self.observe(no_summaries=True)
        revealed: list[Element] = []
        for sd in diff_pages(prev, self.page_mem).sections:
            revealed.extend(sd.added)
            if sd.kind == "appeared" and sd.section:
                revealed.extend(sd.section.elements)

        if pick:
            matches = _best_matches(pick, revealed)
            attempts = 0
            while not matches and attempts < 2:
                # Two slow paths: (a) suggestions were already open before this
                # command (prior --no-submit) so nothing diffs as "revealed";
                # (b) the suggestion XHR outlives the quiescence window (DOM is
                # quiet while the request is in flight — recreation.gov). Wait
                # briefly, re-observe, and match option-ish elements page-wide.
                import asyncio as _asyncio

                await _asyncio.sleep(1.2)
                await self.observe(no_summaries=True)
                fallback = [
                    e
                    for s in self.page_mem.sections
                    for e in s.elements
                    if e.desc.role in ("option", "menuitem", "menuitemcheckbox", "listitem")
                ]
                matches = _best_matches(pick, fallback)
                revealed = revealed or fallback
                attempts += 1
            if len(matches) != 1:
                pool = _describe_pool(matches or revealed)
                word = "ambiguous among" if matches else f'no suggestion matching "{pick}"; saw'
                raise CommandError(f"{word}: {pool}", 2)
            opt = await resolve(self.page, matches[0].desc)
            await opt.click(timeout=5000)
            steps.append(f"  ✓ picked suggestion {matches[0].desc.short_desc()}")
        elif not no_submit:
            await loc.press("Enter", timeout=5000)
            steps.append(
                f"  ✓ pressed Enter ({len(revealed)} suggestions were shown)"
                if revealed
                else "  ✓ pressed Enter"
            )
        else:
            steps.append(f"  ✓ not submitted ({len(revealed)} suggestions shown)")
        return await self._finish_action("\n".join(steps), begin_state)
