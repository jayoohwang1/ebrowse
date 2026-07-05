"""Session: one browser context + observation state + verb implementations.

Owned by the daemon's SessionManager; every command for a session runs under
its asyncio lock, so verb code never races itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

from ebrowse.actions import ActionsMixin
from ebrowse.compound import CompoundMixin
from ebrowse.config import Config, cache_dir
from ebrowse.core import render
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import capture
from ebrowse.core.split import RawSection
from ebrowse.errors import CommandError
from ebrowse.model import PageMem
from ebrowse.summarize.batch import caption_image, summarize_page
from ebrowse.summarize.cache import SummaryCache
from ebrowse.summarize.client import SummarizerClient

GOTO_TIMEOUT_MS = 45_000

# Kept in sync with ebrowse.dev: full-chromium new headless + a plain Chrome UA
# passes Akamai fronts that reject the default headless shell (see
# IMPLEMENTATION_LOG.md Phase 1 smoke findings).
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled"]
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


def context_kwargs(cfg: Config) -> dict[str, Any]:
    w, h = (cfg.browser.viewport + [1280, 1280])[:2]
    return {
        "viewport": {"width": int(w), "height": int(h)},
        "user_agent": USER_AGENT,
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
    }


class Session(CompoundMixin, ActionsMixin):
    def __init__(self, name: str, cfg: Config) -> None:
        self.name = name
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self.registry = RefRegistry()
        self._pw = None
        self._browser = None  # launch mode only
        self._context = None
        self._page = None
        self._cdp_url: str | None = cfg.browser.cdp_url or None
        # observation state
        self.page_mem: PageMem | None = None
        self.raw_by_sid: dict[str, RawSection] = {}
        self.nav_id = 0
        self._notes: list[str] = []  # dialog/popup events surfaced in the next diff
        # summarizer sidecar (never load-bearing)
        self._summarizer = SummarizerClient(cfg.summarizer)
        self._cache: SummaryCache | None = None
        self._backfill_task: asyncio.Task | None = None
        self._backfill_sig: frozenset[str] = frozenset()

    # ------------------------------------------------------------ browser ----

    async def _ensure_browser(self) -> None:
        if self._context is not None:
            return
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            if self.cfg.browser.mode == "cdp" or self._cdp_url:
                url = self._cdp_url or self.cfg.browser.cdp_url
                if not url:
                    raise CommandError(
                        "cdp mode configured but no cdp_url — run 'ebrowse connect <port>'", 2
                    )
                self._browser = await self._pw.chromium.connect_over_cdp(url)
                contexts = self._browser.contexts
                self._context = contexts[0] if contexts else await self._browser.new_context()
            else:
                profile = self.cfg.browser.profile_dir or str(cache_dir() / "profiles" / self.name)
                Path(profile).mkdir(parents=True, exist_ok=True)
                self._context = await self._pw.chromium.launch_persistent_context(
                    profile,
                    headless=self.cfg.browser.headless,
                    channel="chromium",
                    args=BROWSER_ARGS,
                    **context_kwargs(self.cfg),
                )
        except CommandError:
            raise
        except Exception as e:
            raise CommandError(f"could not start browser: {e} — try 'ebrowse doctor'", 3) from e
        self._context.on("page", self._on_new_page)
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._wire_page(self._page)

    def _on_new_page(self, page) -> None:
        # adopt tabs opened by the page (target=_blank etc.) as the active tab
        logger.info(f"[{self.name}] new tab: adopting")
        self._notes.append(f"a new tab opened and is now active: {page.url[:100]}")
        self._page = page
        self._wire_page(page)

    def _wire_page(self, page) -> None:
        page.set_default_timeout(10_000)
        page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog) -> None:
        # Native dialogs block everything; default policy is accept (dismiss for
        # prompts, which would otherwise inject empty text) and surface the event
        # in the next diff's notes so the agent knows it happened.
        action = "dismissed" if dialog.type == "prompt" else "accepted"
        self._notes.append(f'native {dialog.type} auto-{action}: "{dialog.message[:100]}"')
        coro = dialog.dismiss() if dialog.type == "prompt" else dialog.accept()
        task = asyncio.ensure_future(coro)
        task.add_done_callback(lambda t: t.exception())  # swallow late errors

    @property
    def page(self):
        if self._page is None:
            raise CommandError("no page open — run 'ebrowse open <url>' first", 2)
        return self._page

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._context:
                await self._context.close()
        with contextlib.suppress(Exception):
            if self._browser:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw:
                await self._pw.stop()
        self._pw = self._browser = self._context = self._page = None
        self.page_mem = None
        self.raw_by_sid = {}
        if self._backfill_task and not self._backfill_task.done():
            self._backfill_task.cancel()
        with contextlib.suppress(Exception):
            await self._summarizer.aclose()
        if self._cache is not None:
            self._cache.close()
            self._cache = None

    # -------------------------------------------------------- observation ----

    async def observe(self, wait_summaries: bool = False, no_summaries: bool = False) -> str:
        """Capture -> build PageMem -> apply/queue summaries -> outline text.
        The one observation path (actions and navigation verbs go through it)."""
        snap = await capture(self.page)
        self.page_mem, self.raw_by_sid = build_page(
            snap, self.registry, self.cfg.observe, nav_id=self.nav_id
        )
        note = None
        if not no_summaries and self.cfg.summarizer.enabled:
            note = await self._apply_summaries(wait_summaries)
        return render.render_outline(self.page_mem, note)

    # ------------------------------------------------------------ summaries ----

    def _summary_cache(self) -> SummaryCache:
        if self._cache is None:
            self._cache = SummaryCache()
        return self._cache

    def _fill_from_cache(self) -> tuple[int, int]:
        """Apply cached summaries to current sections; returns (cached, total)."""
        page = self.page_mem
        sections = [s for s in page.sections if not s.cross_origin]
        cached = self._summary_cache().get_many([s.content_hash for s in sections])
        for s in sections:
            s.summary = cached.get(s.content_hash)
        return len(cached), len(sections)

    async def _apply_summaries(self, wait: bool) -> str | None:
        """Fill summaries from cache; backfill misses (inline when `wait`).
        Returns the outline status note, or None when nothing is pending."""
        have, total = self._fill_from_cache()
        if have == total:
            return None
        if not self._summarizer.available:
            return "summaries: unavailable (deterministic labels shown)"
        if wait:
            await self._backfill(self.page_mem, self._section_texts())
            self._fill_from_cache()
            return None
        missing = frozenset(
            s.content_hash
            for s in self.page_mem.sections
            if s.summary is None and not s.cross_origin
        )
        already_running = (
            self._backfill_task and not self._backfill_task.done() and missing <= self._backfill_sig
        )
        if not already_running:
            # snapshot page + texts now: the task runs outside the session lock
            # and must not race later observations
            page, texts = self.page_mem, self._section_texts()
            self._backfill_sig = missing
            self._backfill_task = asyncio.create_task(self._backfill(page, texts))
            self._backfill_task.add_done_callback(_log_task_error)
        return f"summaries: {have}/{total} cached · backfill running (rerun outline to see them)"

    async def _backfill(self, page: PageMem, texts: dict[str, str]) -> None:
        parsed = await summarize_page(
            self._summarizer, page, texts, self.cfg.summarizer.max_input_tokens
        )
        if parsed:
            by_sid = {s.sid: s for s in page.sections}
            self._summary_cache().put_many(
                {by_sid[sid].content_hash: summary for sid, summary in parsed.items()}
            )

    def _require_page_mem(self) -> PageMem:
        if self.page_mem is None:
            raise CommandError("nothing observed yet — run 'ebrowse outline'", 2)
        return self.page_mem

    # -------------------------------------------------------------- verbs ----

    async def verb_open(self, url: str) -> str:
        if "://" not in url:
            url = f"https://{url}"
        self._check_url_allowed(url)
        await self._ensure_browser()
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        except Exception as e:
            raise CommandError(f"navigation failed: {_first_line(e)}", 1) from e
        await self._settle()
        self.nav_id += 1
        return await self.observe()

    async def verb_reload(self) -> str:
        await self.page.reload(wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        await self._settle()
        self.nav_id += 1
        return await self.observe()

    async def verb_back(self) -> str:
        resp = await self.page.go_back(wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        if resp is None:
            raise CommandError("no history to go back to", 1)
        await self._settle()
        self.nav_id += 1
        return await self.observe()

    async def verb_forward(self) -> str:
        resp = await self.page.go_forward(wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        if resp is None:
            raise CommandError("no history to go forward to", 1)
        await self._settle()
        self.nav_id += 1
        return await self.observe()

    async def verb_outline(
        self,
        refresh: bool = False,
        wait_summaries: bool = False,
        no_summaries: bool = False,
    ) -> str:
        del refresh  # observation is always fresh; flag reserved for future caching
        await self._ensure_browser()
        if self.page.url in ("about:blank", ""):
            raise CommandError("no page open — run 'ebrowse open <url>' first", 2)
        return await self.observe(wait_summaries=wait_summaries, no_summaries=no_summaries)

    async def verb_expand(self, target: str, cursor: int = 0, show_all: bool = False) -> str:
        page_mem = self._require_page_mem()
        sid = target
        if target.startswith("@"):
            found = page_mem.find_element(target)
            if not found:
                raise CommandError(
                    f"unknown ref {target} on current page — run 'ebrowse outline'", 2
                )
            sid = found[0].sid
        section = page_mem.section(sid)
        if section is None:
            sids = ", ".join(s.sid for s in page_mem.sections)
            raise CommandError(f"no section '{sid}' (have: {sids}) — run 'ebrowse outline'", 2)
        await self._caption_section_images(sid)
        return render.render_section_markdown(
            section, self.raw_by_sid[sid], self.cfg.observe, cursor=cursor, show_all=show_all
        )

    _MAX_CAPTIONS_PER_EXPAND = 4

    def _img_nodes(self, sid: str | None = None):
        raws = [self.raw_by_sid[sid]] if sid else list(self.raw_by_sid.values())
        for raw in raws:
            for n in raw.iter_walk():
                if n.tag == "img" and n.ref:
                    yield n

    async def _caption_section_images(self, sid: str) -> None:
        """Fill attrs['cap'] on @i-ref'd images in a section (expand-time only,
        cached by src hash; screenshots the live element — no re-download)."""
        if not (self.cfg.summarizer.enabled and self.cfg.summarizer.vision):
            return
        import base64
        import hashlib

        cache = self._summary_cache()
        budget = self._MAX_CAPTIONS_PER_EXPAND
        for node in self._img_nodes(sid):
            src = node.attrs.get("src") or f"bbox:{node.rect}"
            key = hashlib.sha1(src.encode()).hexdigest()[:16]
            cached = cache.get_caption(key)
            if cached:
                node.attrs["cap"] = cached
                continue
            if budget <= 0 or not self._summarizer.available or node.attrs.get("alt"):
                continue  # alt text is already a decent label; spend budget on alt-less imgs
            budget -= 1
            try:
                x, y, w, h = node.rect
                png = await self.page.screenshot(
                    full_page=True,
                    clip={"x": x, "y": y, "width": max(16, w), "height": max(16, h)},
                )
                cap = await caption_image(self._summarizer, base64.b64encode(png).decode())
            except Exception as e:
                logger.debug(f"caption failed for {src[:60]}: {e}")
                continue
            if cap:
                cache.put_caption(key, cap)
                node.attrs["cap"] = cap

    async def verb_query(
        self,
        sid: str,
        filter_expr: str | None = None,
        cols: list[str] | None = None,
        cursor: int = 0,
        limit: int | None = None,
    ) -> str:
        page_mem = self._require_page_mem()
        section = page_mem.section(sid)
        if section is None:
            sids = ", ".join(s.sid for s in page_mem.sections)
            raise CommandError(f"no section '{sid}' (have: {sids}) — run 'ebrowse outline'", 2)
        if section.type not in ("list", "table"):
            listy = ", ".join(s.sid for s in page_mem.sections if s.type in ("list", "table"))
            raise CommandError(
                f"{sid} is a {section.type} section — query works on list/table "
                f"sections ({listy or 'none on this page'})",
                2,
            )
        out = render.render_query(
            section,
            self.raw_by_sid[sid],
            self.cfg.observe,
            filter_expr=filter_expr,
            cols=cols,
            cursor=cursor,
            limit=limit,
        )
        if out.startswith("error-cols: "):
            raise CommandError(out[len("error-cols: ") :], 2)
        return out

    async def verb_screenshot(
        self,
        output: str | None = None,
        section: str | None = None,
        ref: str | None = None,
        full: bool = False,
    ) -> str:
        page_mem = self.page_mem
        clip = None
        if section or ref:
            if page_mem is None:
                raise CommandError("run 'ebrowse outline' before section/ref screenshots", 2)
            if section:
                s = page_mem.section(section)
                if s is None:
                    raise CommandError(f"no section '{section}' — run 'ebrowse outline'", 2)
                bbox = s.bbox
            elif (ref or "").startswith("@i"):
                node = next((n for n in self._img_nodes() if n.ref == ref), None)
                if node is None:
                    raise CommandError(f"unknown image ref {ref} — run 'ebrowse outline'", 2)
                from ebrowse.model import BBox as _BBox

                bbox = _BBox(*node.rect)
            else:
                found = page_mem.find_element(ref or "")
                if not found:
                    raise CommandError(f"unknown ref {ref} — run 'ebrowse outline'", 2)
                bbox = found[1].state.bbox
            clip = {
                "x": max(0, bbox.x - 4),
                "y": max(0, bbox.y - 4),
                "width": max(16, bbox.width + 8),
                "height": max(16, bbox.height + 8),
            }
        if output is None:
            shots = Path(tempfile.gettempdir()) / "ebrowse_screenshots"
            shots.mkdir(exist_ok=True)
            output = str(shots / f"shot_{int(time.time())}.png")
        # clip coordinates are document-absolute, which requires full_page mode;
        # plain viewport screenshots interpret clip relative to the viewport.
        await self.page.screenshot(path=output, full_page=bool(clip) or full, clip=clip)
        return f"saved {output}"

    async def verb_get(self, what: str, target: str | None, attr: str | None) -> str:
        if what == "url":
            return self.page.url
        if what == "title":
            return await self.page.title()
        if not target:
            raise CommandError(f"get {what} needs a target (@ref or CSS selector)", 2)
        loc = await self._resolve_locator(target)
        if what == "text":
            return (await loc.inner_text())[:4000]
        if what == "html":
            return (await loc.inner_html())[:8000]
        if what == "value":
            return await loc.input_value()
        if what == "attr":
            if not attr:
                raise CommandError("get attr needs an attribute name", 2)
            val = await loc.get_attribute(attr)
            return val if val is not None else "(no such attribute)"
        raise CommandError(f"unknown getter '{what}'", 2)

    async def verb_tabs(self) -> str:
        if self._context is None:
            raise CommandError("no browser running — run 'ebrowse open <url>'", 2)
        lines = []
        for i, p in enumerate(self._context.pages):
            marker = "*" if p == self._page else " "
            title = ""
            with contextlib.suppress(Exception):
                title = await p.title()
            lines.append(f"{marker} {i}: {title[:60]} — {p.url[:100]}")
        return "\n".join(lines) if lines else "no tabs"

    async def verb_tab(self, index: int) -> str:
        if self._context is None:
            raise CommandError("no browser running — run 'ebrowse open <url>'", 2)
        pages = self._context.pages
        if not 0 <= index < len(pages):
            raise CommandError(f"no tab {index} (have 0..{len(pages) - 1})", 2)
        self._page = pages[index]
        await self._page.bring_to_front()
        self.nav_id += 1
        return await self.observe()

    async def verb_connect(self, target: str) -> str:
        url = target if "://" in target else f"http://127.0.0.1:{target}"
        await self.close()
        self._cdp_url = url
        await self._ensure_browser()
        return f"attached over CDP: {url} ({len(self._context.pages)} tab(s))"

    # ------------------------------------------------------------ helpers ----

    async def _settle(self) -> None:
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("networkidle", timeout=3000)

    def _check_url_allowed(self, url: str) -> None:
        allowed = self.cfg.security.allowed_domains
        if not allowed:
            return
        from urllib.parse import urlsplit

        host = urlsplit(url).netloc.lower()
        if not any(host == d or host.endswith("." + d) for d in allowed):
            raise CommandError(
                f"domain {host} not in security.allowed_domains — edit config to allow it", 2
            )

    async def _resolve_locator(self, target: str):
        """Minimal @ref/CSS resolution for getters. Phase 3 hardens this into
        core/locate.py with the full descriptor chain + occlusion checks."""
        if not target.startswith("@"):
            loc = self.page.locator(target)
            if await loc.count() == 0:
                raise CommandError(f"no element matches CSS '{target}'", 2)
            return loc.first
        page_mem = self._require_page_mem()
        found = page_mem.find_element(target)
        if not found:
            raise CommandError(
                f"stale ref {target}: not on current page — run 'ebrowse outline'", 2
            )
        _, element = found
        from ebrowse.core.locate import resolve

        return await resolve(self.page, element.desc)


def _first_line(e: Exception) -> str:
    return str(e).splitlines()[0][:200]


def _log_task_error(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.warning(f"summary backfill failed: {type(exc).__name__}: {str(exc)[:150]}")
