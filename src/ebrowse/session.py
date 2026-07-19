"""Session: one browser context + observation state + verb implementations.

Owned by the daemon's SessionManager; every command for a session runs under
its asyncio lock, so verb code never races itself.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from loguru import logger

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Dialog, FloatRect, Page, Playwright

    from ebrowse.actions import ActionSnapshot

from ebrowse import debug
from ebrowse.actions import ActionsMixin
from ebrowse.compound import CompoundMixin
from ebrowse.config import Config, cache_dir
from ebrowse.core import render
from ebrowse.core.ax import render_section_ax
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import DomSnapshot, capture
from ebrowse.core.split import RawSection
from ebrowse.errors import CommandError, ExitCode
from ebrowse.model import PageMem, Section
from ebrowse.summarize.batch import caption_image, caption_screen, summarize_page
from ebrowse.summarize.cache import SummaryCache
from ebrowse.summarize.client import SummarizerClient

GOTO_TIMEOUT_MS = 45_000

# Every navigation/landing result ends with this so the agent knows the next
# move (observation is explicit — navigation no longer dumps a full outline).
_OUTLINE_HINT = "run 'ebrowse outline' to read the page"

# Kept in sync with ebrowse.dev: full-chromium new headless + a plain Chrome UA
# passes Akamai fronts that reject the default headless shell (see
# docs/adr/0002-full-chromium-with-plain-ua.md).
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled"]

CDP_PROBE_TIMEOUT_S = 2.0

# Native dialog types the agent must decide on (a real choice / text input); the
# rest (alert, beforeunload) are auto-accepted so they never block.
DECISION_DIALOG_TYPES = ("confirm", "prompt")


@dataclass(slots=True)
class PendingDialog:
    """A native confirm/prompt left open for the agent to resolve. While it is
    open the renderer main thread is blocked, so no page-touching verb can run
    until 'dialog accept|dismiss' resolves it (see docs/adr/0007)."""

    type: str  # "confirm" | "prompt"
    message: str
    default_value: str  # prompt's default text ("" for confirm)
    dialog: Dialog  # live Playwright handle
    # Context of the action that opened it, so resolving can emit the normal
    # post-action diff instead of a bare outline. None if opened outside an action.
    action_line: str | None = None
    begin_state: ActionSnapshot | None = None


async def _check_cdp_reachable(url: str) -> None:
    """Fail fast with a targeted hint when nothing is listening on the CDP
    endpoint. Playwright's connect_over_cdp otherwise surfaces a generic error
    that doesn't name the recovery action (per architecture principle 8)."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme in ("https", "wss") else 80)
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=CDP_PROBE_TIMEOUT_S
        )
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    except (OSError, TimeoutError) as e:
        raise CommandError(
            f"cannot reach Chrome at {host}:{port} — start Chrome with "
            f"'--remote-debugging-port={port}', then retry 'ebrowse connect {port}'",
            ExitCode.USAGE,
        ) from e


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
        self._pw: Playwright | None = None
        self._browser: Browser | None = None  # launch mode only
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._page_mru: list[Page] = []
        self._wired_pages: set[Page] = set()
        self._cdp_url: str | None = cfg.browser.cdp_url or None
        # observation state
        self.page_mem: PageMem | None = None
        self.raw_by_sid: dict[str, RawSection] = {}
        # act-time node bindings (ADR 0015): ref -> backendNodeId from the
        # latest cdp-engine capture; consumed by the locate rescue path
        self.ref_bindings: dict[str, int] = {}
        self._cdp_bridge = None  # CdpBridge | None, lazy, per active page
        self.nav_id = 0
        self._notes: list[str] = []  # dialog/popup events surfaced in the next diff
        # set by a click's occlusion pre-check when a modal blocks the page WITHOUT
        # covering the target (native showModal top-layer / aria-modal focus trap);
        # surfaced as a hint only if that click then registers no change
        self._blocking_modal: str | None = None
        self._hover_delivery_suspect = False
        self.nav_events = 0  # main-frame navigations (same-URL reload detection)
        # sections the agent expanded, fingerprint -> nav_id at expand time; the
        # diff quotes far more new text for these (it is actively reading them).
        # Keyed by fingerprint (stable across re-observations, unlike sids) and
        # self-expiring on navigation via the nav_id check in _expanded_now().
        self._expanded_fps: dict[str, int] = {}
        # native confirm/prompt dialogs awaiting an agent decision, keyed by the
        # page they blocked (a dialog on a background tab must not block the active one)
        self._pending_dialogs: dict[Page, PendingDialog] = {}
        # summarizer sidecar (never load-bearing): outline enrichment (text
        # summaries + visual glance) runs synchronously with a hard timeout;
        # a slow/dead sidecar degrades outlines to deterministic labels.
        self._summarizer = SummarizerClient(cfg.summarizer)
        self._cache: SummaryCache | None = None
        # --- debug-capture support (optional; used by external harnesses) ---
        # page events (console/network_failure/navigation/dialog) accumulated
        # since the last 'debug-capture' drain; capped so an event-storm page
        # cannot grow memory unboundedly between captures
        self._capture_events: list[dict[str, Any]] = []
        # last DomSnapshot from _observe_page, reused by debug-capture when
        # still fresh (no possibly-mutating verb ran since it was taken)
        self.last_snapshot: DomSnapshot | None = None
        self.cmd_seq = 0  # bumped by the daemon per possibly-mutating verb
        self._snapshot_cmd_seq = -1

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
                        "cdp mode configured but no cdp_url — run 'ebrowse connect <port>'",
                        ExitCode.USAGE,
                    )
                await _check_cdp_reachable(url)
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
            raise CommandError(
                f"could not start browser: {e} — try 'ebrowse doctor'", ExitCode.INTERNAL
            ) from e
        self._context.on("page", self._on_new_page)
        pages = self._context.pages
        if pages:
            for page in pages:
                self._wire_page(page)
            self._activate_page(pages[0])
        else:
            # The context "page" event adopts and wires this page. _wire_page
            # is idempotent in case a backend delivers the event late.
            self._activate_page(await self._context.new_page())

    def _activate_page(self, page: Page) -> None:
        """Make page the logical active tab and move it to the MRU front."""
        self._page = page
        self._page_mru = [p for p in self._page_mru if p is not page]
        self._page_mru.insert(0, page)
        self._wire_page(page)

    def _bring_to_front_soon(self, page: Page) -> None:
        """Page events are synchronous; foreground the adopted tab asynchronously."""

        async def bring() -> None:
            with contextlib.suppress(Exception):
                await page.bring_to_front()

        asyncio.create_task(bring())

    def _on_new_page(self, page: Page) -> None:
        # adopt tabs opened by the page (target=_blank etc.) as the active tab
        logger.info(f"[{self.name}] new tab: adopting")
        self._notes.append(f"a new tab opened and is now active: {page.url[:100]}")
        self._activate_page(page)
        self._bring_to_front_soon(page)

    def _wire_page(self, page: Page) -> None:
        if page in self._wired_pages:
            return
        self._wired_pages.add(page)
        page.set_default_timeout(10_000)
        # bind the page so a dialog is attributed to the tab it blocked
        page.on("dialog", lambda d, p=page: self._on_dialog(d, p))
        # outcome evidence: a download has no DOM footprint, and a same-URL
        # reload is invisible to URL comparison — record both for diff notes
        page.on(
            "download",
            lambda d: self._notes.append(f'download started: "{d.suggested_filename}"'),
        )
        page.on("framenavigated", lambda fr, p=page: self._on_framenavigated(fr, p))
        page.on("close", lambda p=page: self._on_page_closed(p))
        # debug-capture event feed: console output and failed requests are
        # unrecoverable if not recorded live; cheap no-ops when nothing drains them
        page.on(
            "console",
            lambda m: self._capture_event("console", level=m.type, text=m.text[:500]),
        )
        page.on(
            "requestfailed",
            lambda r: self._capture_event(
                "network_failure",
                url=r.url[:300],
                method=r.method,
                failure=(r.failure or "")[:200],
            ),
        )

    def _on_page_closed(self, page: Page) -> None:
        """Recover when an adopted popup/tab closes behind the agent's back."""
        self._page_mru = [p for p in self._page_mru if p is not page]
        self._pending_dialogs.pop(page, None)
        if page is not self._page:
            return
        live = [p for p in self._page_mru if not p.is_closed()]
        if not live and self._context is not None:
            live = [p for p in reversed(self._context.pages) if not p.is_closed()]
        if not live:
            self._page = None
            self.page_mem = None
            self.raw_by_sid = {}
            self.ref_bindings = {}
            self._notes.append("the active tab closed; no tabs remain — run 'ebrowse open <url>'")
            return
        fallback = live[0]
        self._activate_page(fallback)
        self.last_snapshot = None
        self.page_mem = None
        self.raw_by_sid = {}
        self.ref_bindings = {}
        self.nav_id += 1
        self._notes.append(
            f"the active tab closed; switched to the most recent live tab: {fallback.url[:100]}"
        )
        self._bring_to_front_soon(fallback)

    def _on_framenavigated(self, frame, page: Page) -> None:
        if page is self._page and frame is page.main_frame:
            self.nav_events += 1
            self._capture_event("navigation", url=frame.url[:300])

    def _on_dialog(self, dialog: Dialog, page: Page) -> None:
        # `alert` and `beforeunload` carry no decision, so auto-accept them to
        # unblock immediately and note it. `confirm`/`prompt` are a real choice
        # (OK/Cancel or text input): leave them OPEN and record them as pending so
        # the agent decides via 'dialog accept|dismiss'. A registered handler that
        # does not resolve keeps the dialog up (blocking the page) — exactly what
        # we want. See docs/adr/0007-agent-resolves-native-dialogs.md.
        self._capture_event("dialog", dialog_type=dialog.type, message=dialog.message[:300])
        if dialog.type not in DECISION_DIALOG_TYPES:
            self._notes.append(f'native {dialog.type} auto-accepted: "{dialog.message[:100]}"')
            task = asyncio.ensure_future(dialog.accept())
            task.add_done_callback(lambda t: t.exception())  # swallow late errors
            return
        self._pending_dialogs[page] = PendingDialog(
            type=dialog.type,
            message=dialog.message,
            default_value=dialog.default_value,
            dialog=dialog,
        )
        self._notes.append(
            f'native {dialog.type} opened (blocking): "{dialog.message[:100]}" — '
            "resolve with 'ebrowse dialog accept|dismiss'"
        )

    def _active_dialog(self) -> PendingDialog | None:
        """The pending dialog blocking the *current* tab, if any."""
        return self._pending_dialogs.get(self._page) if self._page is not None else None

    def dialog_block_warning(self, verb: str) -> str | None:
        """Recovery-action message when `verb` can't run because a native dialog
        is blocking the current tab; None when nothing is pending."""
        d = self._active_dialog()
        if d is None:
            return None
        return (
            f'a native {d.type} dialog is blocking this tab: "{d.message[:100]}" — '
            "resolve it with 'ebrowse dialog accept' or 'ebrowse dialog dismiss' "
            f"(or 'ebrowse tab <n>' to switch tabs), then retry '{verb}'"
        )

    @property
    def page(self) -> Page:
        if self._page is None:
            raise CommandError("no page open — run 'ebrowse open <url>' first", ExitCode.USAGE)
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
        self._page_mru = []
        self._wired_pages = set()
        self.page_mem = None
        self.last_snapshot = None
        self.raw_by_sid = {}
        self.ref_bindings = {}
        self._cdp_bridge = None
        self._pending_dialogs = {}
        with contextlib.suppress(Exception):
            await self._summarizer.aclose()
        if self._cache is not None:
            self._cache.close()
            self._cache = None

    # -------------------------------------------------------- observation ----

    async def _observe_page(self) -> None:
        """Capture -> build PageMem + raw sections. Deterministic, no LLM, no
        render. The shared rebuild behind navigation, actions, and outline —
        this is what keeps durable @refs live after a navigation without an
        explicit outline."""
        # a native confirm/prompt blocks the renderer main thread, so capture()'s
        # evaluate would hang; refuse fast with the recovery action instead
        if (warn := self.dialog_block_warning("outline")) is not None:
            raise CommandError(warn, ExitCode.ACTION_FAILED)
        # enforce allowed_domains on the *landed* URL too — link clicks and
        # redirects can leave the domain even when the opened URL was allowed
        self._check_url_allowed(self.page.url, landed=True)
        with debug.timed("snapshot", "capture"):
            snap = await capture(self.page, self.cfg.browser.capture_engine)
        # retained for debug-capture reuse: fresh as long as no possibly-mutating
        # verb runs after this observation (cmd_seq check in verb_debug_capture)
        self.last_snapshot = snap
        self._snapshot_cmd_seq = self.cmd_seq
        if debug.enabled():  # node counting is O(n); only walk when recording
            iframes = [n for n in snap.root.walk() if n.tag == "iframe"]
            skipped = sum(1 for n in iframes if n.cross_origin)
            debug.emit(
                "snapshot",
                "captured",
                nodes=sum(1 for _ in snap.root.walk()),
                truncated=snap.truncated,
                iframes=len(iframes),
                iframes_captured=sum(1 for n in iframes if n.children),
                iframes_skipped=skipped,
            )
            if snap.truncated:
                debug.emit("snapshot", "snapshot_truncated", level="warn", url=snap.url)
        with debug.timed("pipeline", "build_page"):
            self.page_mem, self.raw_by_sid = build_page(
                snap, self.registry, self.cfg.observe, nav_id=self.nav_id
            )
        # refresh act-time bindings (cdp engine only; js-engine nodes carry
        # no backend ids and the table stays empty — rescue simply never fires)
        self.ref_bindings = {
            n.ref: n.backend_node_id
            for raw in self.raw_by_sid.values()
            for n in raw.iter_walk()
            if n.ref and n.ref.startswith("@e") and n.backend_node_id is not None
        }
        if debug.enabled():
            debug.emit("locate", "bindings_refreshed", count=len(self.ref_bindings))

    async def observe(
        self, no_summaries: bool = False, no_glance: bool = False, preview: bool = False
    ) -> str:
        """Rebuild + synchronous enrichment (summaries + visual glance) + render
        the outline. The ONLY path that runs the summarizer — navigation verbs
        return a landing line and defer this to an explicit `outline`."""
        await self._observe_page()
        page = self._require_page_mem()
        note = None
        if self.cfg.summarizer.enabled:
            # Enrichment is never load-bearing: the deterministic outline must
            # render even if the summarizer/cache stack fails. Each stage guards
            # + logs itself (below); this is the final backstop for anything
            # unforeseen, so a broken sidecar can never fail an `outline`.
            try:
                note = await self._apply_enrichment(no_summaries=no_summaries, no_glance=no_glance)
            except Exception as e:
                logger.warning(
                    f"outline enrichment failed entirely: {type(e).__name__}: {_first_line(e)}"
                )
                note = "enrichment unavailable — deterministic labels shown"
        return render.render_outline(
            page,
            note,
            preview=preview,
            preview_chars=self.cfg.observe.combined_preview_chars,
        )

    # -------------------------------------------------------------- landing ----

    async def _page_ident(self) -> str:
        """`<url>  ·  "<title>"` — the orientation line for navigation results."""
        title = ""
        with contextlib.suppress(Exception):
            title = (await self.page.title()).strip()[:80]
        ident = self.page.url
        if title:
            ident += f'  ·  "{title}"'
        return ident

    def _with_notes(self, text: str) -> str:
        """Append surfaced dialog/popup notes (mirrors the diff's note lines)."""
        return text + "".join(f"\nnote: {n}" for n in self._notes)

    async def _nav_landing(self, action_line: str) -> str:
        """Landing result for an action that navigated (page_mem already rebuilt
        by the caller). Mirrors the navigation-verb landing: the outcome arrow is
        on the FIRST line, compound step lines follow, then orientation + hint."""
        head, _, steps = action_line.partition("\n")
        lines = [f"{head} → navigation"]
        if steps:
            lines.append(steps)
        lines.append(f"now at {await self._page_ident()}")
        lines.append(_OUTLINE_HINT)
        return self._with_notes("\n".join(lines))

    def _no_baseline_landing(self, action_line: str) -> str:
        """Result for an action taken (via CSS) before any outline — same page,
        but no prior observation to diff against."""
        head, _, steps = action_line.partition("\n")
        lines = [f"{head} → done"]
        if steps:
            lines.append(steps)
        lines.append(f"(no prior outline to diff — {_OUTLINE_HINT})")
        return self._with_notes("\n".join(lines))

    # ------------------------------------------------------------ summaries ----

    def _summary_cache(self) -> SummaryCache:
        if self._cache is None:
            self._cache = SummaryCache()
        return self._cache

    def _fill_from_cache(self) -> tuple[int, int]:
        """Apply cached summaries to current sections; returns (cached, total)."""
        page = self._require_page_mem()
        sections = [s for s in page.sections if not s.cross_origin]
        cached = self._summary_cache().get_many([s.content_hash for s in sections])
        for s in sections:
            s.summary = cached.get(s.content_hash)
        return len(cached), len(sections)

    async def _apply_enrichment(self, no_summaries: bool, no_glance: bool) -> str | None:
        """Synchronously fill section summaries and the ◉ visual glance: cache
        hits are free, misses are generated by the sidecar under a hard timeout
        (cfg.sync_timeout_s). Text and glance run concurrently. On timeout/
        failure the outline degrades to deterministic labels (never load-bearing).
        Returns an outline status note, or None when nothing was pending."""
        page = self._require_page_mem()
        cfg = self.cfg.summarizer

        text_pending = False
        if not no_summaries:
            try:
                self._fill_from_cache()
                text_pending = any(s.summary is None and not s.cross_origin for s in page.sections)
            except Exception as e:
                logger.warning(
                    f"outline enrichment: summary cache read failed: "
                    f"{type(e).__name__}: {_first_line(e)}"
                )
        glance_pending = False
        if not no_glance and cfg.glance and cfg.vision:
            try:
                cached = self._summary_cache().get_screen(self._screen_key(page))
            except Exception as e:
                logger.warning(
                    f"outline enrichment: glance cache read failed: "
                    f"{type(e).__name__}: {_first_line(e)}"
                )
                cached = None
            if cached:
                page.screen_gist = cached
            else:
                glance_pending = True

        if not text_pending and not glance_pending:
            return None
        if not self._summarizer.available:
            # Only the kind(s) actually pending are degraded — don't blame
            # summaries when only the glance needed the (unavailable) sidecar.
            unavailable = []
            if text_pending:
                unavailable.append("summaries: sidecar unavailable — deterministic labels shown")
            if glance_pending:
                unavailable.append("glance: sidecar unavailable")
            return " · ".join(unavailable)

        jobs = []
        if text_pending:
            jobs.append(self._gen_summaries(page))
        if glance_pending:
            jobs.append(self._gen_glance(page))
        notes = [n for n in await asyncio.gather(*jobs) if n]
        return " · ".join(notes) if notes else None

    async def _gen_summaries(self, page: PageMem) -> str | None:
        """One synchronous batched summary call; cache + fill results. Returns a
        status note only if some sections remain unlabeled (slow/incomplete).
        Any failure (sidecar or cache write) is caught + logged under this stage
        and degrades to deterministic labels — never fails the outline."""
        cfg = self.cfg.summarizer
        try:
            parsed = await summarize_page(
                self._summarizer,
                page,
                self._section_texts(),
                cfg.max_input_tokens,
                timeout_s=cfg.sync_timeout_s,
                retry=False,
            )
            if parsed:
                by_sid = {s.sid: s for s in page.sections}
                self._summary_cache().put_many(
                    {by_sid[sid].content_hash: summary for sid, summary in parsed.items()}
                )
                self._fill_from_cache()
        except Exception as e:
            logger.warning(
                f"outline enrichment: summaries stage failed: {type(e).__name__}: {_first_line(e)}"
            )
            return "summaries: enrichment failed (deterministic labels shown)"
        pending = sum(1 for s in page.sections if s.summary is None and not s.cross_origin)
        if pending:
            total = sum(1 for s in page.sections if not s.cross_origin)
            return f"summaries: {total - pending}/{total} (sidecar slow or incomplete)"
        return None

    async def _gen_glance(self, page: PageMem) -> str | None:
        """Screenshot the viewport and get one VLM visual gist, synchronously.
        Sets page.screen_gist + caches it. Returns a note only on failure. Each
        stage (screenshot, VLM, cache write) is caught + logged by name so the
        log points at where it broke; failure degrades to no ◉ line."""
        cfg = self.cfg.summarizer
        try:
            png_b64 = await self._viewport_png_b64()
        except Exception as e:
            logger.warning(
                f"outline enrichment: glance screenshot failed: {type(e).__name__}: {_first_line(e)}"
            )
            return "glance: screenshot failed"
        try:
            gist = await caption_screen(
                self._summarizer, png_b64, timeout_s=cfg.sync_timeout_s, retry=False
            )
        except Exception as e:
            logger.warning(
                f"outline enrichment: glance VLM call failed: {type(e).__name__}: {_first_line(e)}"
            )
            return "glance: sidecar slow or unavailable"
        if not gist:
            return "glance: sidecar slow or unavailable"
        page.screen_gist = gist  # shown this outline even if the cache write below fails
        try:
            self._summary_cache().put_screen(self._screen_key(page), gist)
        except Exception as e:
            logger.warning(
                f"outline enrichment: glance cache write failed: {type(e).__name__}: {_first_line(e)}"
            )
        return None

    def _screen_key(self, page: PageMem) -> str:
        """Cache key for the visual glance. Deterministic from the page's DOM
        structure so revisiting the same page state is a cache hit (no
        screenshot, no VLM). Identical structure with different pixels (rare) →
        at worst a one-revision-stale gist."""
        sig = "|".join(
            f"{s.fingerprint}:{s.content_hash}" for s in page.sections if not s.cross_origin
        )
        return hashlib.sha1(f"{page.url}\n{sig}".encode()).hexdigest()[:16]

    async def _viewport_png_b64(self) -> str:
        png = await self.page.screenshot(full_page=False)
        return base64.b64encode(png).decode()

    def _require_page_mem(self) -> PageMem:
        if self.page_mem is None:
            raise CommandError("nothing observed yet — run 'ebrowse outline'", ExitCode.USAGE)
        return self.page_mem

    def _get_section(self, sid: str) -> Section:
        """Section by sid on the current page, or a recovery-action error."""
        page_mem = self._require_page_mem()
        section = page_mem.section(sid)
        if section is None:
            sids = ", ".join(s.sid for s in page_mem.sections)
            raise CommandError(
                f"no section '{sid}' (have: {sids}) — run 'ebrowse outline'", ExitCode.USAGE
            )
        return section

    # -------------------------------------------------------------- verbs ----

    async def verb_open(self, url: str) -> str:
        if "://" not in url:
            url = f"https://{url}"
        self._check_url_allowed(url)
        await self._ensure_browser()
        self._notes = []
        try:
            with debug.timed("session", "navigate", url=url[:200]):
                await self.page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        except Exception as e:
            raise CommandError(
                f"navigation failed: {_first_line(e)}", ExitCode.ACTION_FAILED
            ) from e
        await self._settle()
        self.nav_id += 1
        await self._observe_page()
        return self._with_notes(f"opened {await self._page_ident()}\n{_OUTLINE_HINT}")

    async def verb_reload(self) -> str:
        self._notes = []
        await self.page.reload(wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        await self._settle()
        self.nav_id += 1
        await self._observe_page()
        return self._with_notes(f"reloaded {await self._page_ident()}\n{_OUTLINE_HINT}")

    async def verb_back(self) -> str:
        self._notes = []
        resp = await self.page.go_back(wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        if resp is None:
            raise CommandError("no history to go back to", ExitCode.ACTION_FAILED)
        await self._settle()
        self.nav_id += 1
        await self._observe_page()
        return self._with_notes(f"back to {await self._page_ident()}\n{_OUTLINE_HINT}")

    async def verb_forward(self) -> str:
        self._notes = []
        resp = await self.page.go_forward(wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        if resp is None:
            raise CommandError("no history to go forward to", ExitCode.ACTION_FAILED)
        await self._settle()
        self.nav_id += 1
        await self._observe_page()
        return self._with_notes(f"forward to {await self._page_ident()}\n{_OUTLINE_HINT}")

    async def verb_outline(
        self,
        refresh: bool = False,
        no_summaries: bool = False,
        no_glance: bool = False,
        preview: bool = False,
    ) -> str:
        del refresh  # observation is always fresh; flag reserved for future caching
        await self._ensure_browser()
        if self.page.url in ("about:blank", ""):
            raise CommandError("no page open — run 'ebrowse open <url>' first", ExitCode.USAGE)
        return await self.observe(no_summaries=no_summaries, no_glance=no_glance, preview=preview)

    async def verb_expand(
        self, target: str, cursor: int = 0, show_all: bool = False, ax: bool = False
    ) -> str:
        page_mem = self._require_page_mem()
        sid = target
        if target.startswith("@"):
            found = page_mem.find_element(target)
            if not found:
                raise CommandError(
                    f"unknown ref {target} on current page — run 'ebrowse outline'",
                    ExitCode.USAGE,
                )
            sid = found[0].sid
            # The AX tree always renders the enclosing section. Markdown
            # expansion of a <select> ref instead lists its options.
            if not ax and found[1].desc.tag == "select":
                raw = self.raw_by_sid.get(sid)
                node = next((n for n in raw.iter_walk() if n.ref == target), None) if raw else None
                if node is not None and node.attrs.get("opt"):
                    return render.render_select_options(node, cursor=cursor, show_all=show_all)
        section = self._get_section(sid)
        self._expanded_fps[section.fingerprint] = self.nav_id
        if ax:
            return render_section_ax(
                section, self.raw_by_sid[sid], self.cfg.observe, cursor=cursor, show_all=show_all
            )
        await self._caption_section_images(sid)
        return render.render_section_markdown(
            section, self.raw_by_sid[sid], self.cfg.observe, cursor=cursor, show_all=show_all
        )

    def _expanded_now(self) -> set[str]:
        """Fingerprints of sections expanded on the CURRENT page (entries from
        before the last navigation don't count — same-fingerprint chrome on the
        next page shouldn't inherit the verbose diff budget)."""
        return {fp for fp, nid in self._expanded_fps.items() if nid == self.nav_id}

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
        section = self._get_section(sid)
        if section.type not in ("list", "table"):
            listy = ", ".join(s.sid for s in page_mem.sections if s.type in ("list", "table"))
            raise CommandError(
                f"{sid} is a {section.type} section — query works on list/table "
                f"sections ({listy or 'none on this page'})",
                ExitCode.USAGE,
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
            raise CommandError(out[len("error-cols: ") :], ExitCode.USAGE)
        return out

    async def verb_screenshot(
        self,
        output: str | None = None,
        section: str | None = None,
        ref: str | None = None,
        full: bool = False,
    ) -> str:
        page_mem = self.page_mem
        clip: FloatRect | None = None
        if section or ref:
            if page_mem is None:
                raise CommandError(
                    "run 'ebrowse outline' before section/ref screenshots", ExitCode.USAGE
                )
            if section:
                bbox = self._get_section(section).bbox
            elif (ref or "").startswith("@i"):
                node = next((n for n in self._img_nodes() if n.ref == ref), None)
                if node is None:
                    raise CommandError(
                        f"unknown image ref {ref} — run 'ebrowse outline'", ExitCode.USAGE
                    )
                from ebrowse.model import BBox as _BBox

                bbox = _BBox(*node.rect)
            else:
                found = page_mem.find_element(ref or "")
                if not found:
                    raise CommandError(f"unknown ref {ref} — run 'ebrowse outline'", ExitCode.USAGE)
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

    async def verb_describe(self, prompt: str | None = None, refresh: bool = False) -> str:
        """Free-form visual query over a screenshot via the local VLM (◉,
        untrusted). No prompt → the concise gist (shared with the outline's ◉
        line and cache); a prompt → ask for anything, from a one-line overlay
        check to exhaustive detail. The routing tier between page text and
        spending ~2.4k tokens on the pixels — the answer costs the agent only
        the text, not the image. Works even when the auto ◉ line is disabled."""
        await self._ensure_browser()
        cfg = self.cfg.summarizer
        if not (cfg.enabled and cfg.vision):
            raise CommandError(
                "describe-screen needs a vision summarizer — set summarizer.vision=true "
                "and a multimodal model (see docs/configuration.md)",
                ExitCode.USAGE,
            )
        if self.page.url in ("about:blank", ""):
            raise CommandError("no page open — run 'ebrowse open <url>' first", ExitCode.USAGE)
        if not self._summarizer.available:
            raise CommandError(
                "summarizer unavailable (recent failures) — retry shortly or run 'ebrowse doctor'",
                ExitCode.ACTION_FAILED,
            )
        # no-prompt gist is cached per screen state and shared with the outline
        if prompt is None and self.page_mem is not None and not refresh:
            cached = self._summary_cache().get_screen(self._screen_key(self.page_mem))
            if cached:
                return f"◉ {cached}"
        png_b64 = await self._viewport_png_b64()
        # a free-form prompt may ask for exhaustive detail → generous ceiling;
        # the no-prompt gist stays concise (shared with the outline ◉ line)
        max_tokens = cfg.describe_max_tokens if prompt else 500
        gist = await caption_screen(
            self._summarizer,
            png_b64,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout_s=cfg.describe_timeout_s,
        )
        if not gist:
            raise CommandError(
                "describe-screen got no response from the summarizer — run 'ebrowse doctor'",
                ExitCode.ACTION_FAILED,
            )
        if prompt is None and self.page_mem is not None:
            self._summary_cache().put_screen(self._screen_key(self.page_mem), gist)
        return f"◉ {gist}"

    async def verb_get(self, what: str, target: str | None, attr: str | None) -> str:
        if what == "url":
            return self.page.url
        if what == "title":
            return await self.page.title()
        if not target:
            raise CommandError(f"get {what} needs a target (@ref or CSS selector)", ExitCode.USAGE)
        loc = await self._resolve_locator(target)
        if what == "text":
            return (await loc.inner_text())[:4000]
        if what == "html":
            return (await loc.inner_html())[:8000]
        if what == "value":
            return await loc.input_value()
        if what == "attr":
            if not attr:
                raise CommandError("get attr needs an attribute name", ExitCode.USAGE)
            val = await loc.get_attribute(attr)
            return val if val is not None else "(no such attribute)"
        raise CommandError(f"unknown getter '{what}'", ExitCode.USAGE)

    async def verb_tabs(self) -> str:
        if self._context is None:
            raise CommandError("no browser running — run 'ebrowse open <url>'", ExitCode.USAGE)
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
            raise CommandError("no browser running — run 'ebrowse open <url>'", ExitCode.USAGE)
        pages = self._context.pages
        if not 0 <= index < len(pages):
            raise CommandError(f"no tab {index} (have 0..{len(pages) - 1})", ExitCode.USAGE)
        self._activate_page(pages[index])
        await self.page.bring_to_front()
        self._notes = []
        self.nav_id += 1
        await self._observe_page()
        return self._with_notes(
            f"switched to tab {index}: {await self._page_ident()}\n{_OUTLINE_HINT}"
        )

    async def verb_dialog(self, response: str, text: str | None = None) -> str:
        """Resolve or inspect the native dialog blocking the current tab.
        response ∈ {accept, dismiss, status}. `text` supplies a prompt's answer."""
        d = self._active_dialog()
        if response == "status":
            if d is None:
                return "no native dialog pending on this tab"
            line = f'{d.type} dialog: "{d.message}"'
            if d.type == "prompt":
                line += f' (default: "{d.default_value}")'
            return (
                line + "\nresolve with 'ebrowse dialog accept [text]' or 'ebrowse dialog dismiss'"
            )
        if d is None:
            raise CommandError(
                f"no native dialog pending on this tab — nothing to {response}", ExitCode.USAGE
            )
        if response == "accept":
            # prompt returns the supplied text (or its default); confirm ignores text
            await d.dialog.accept(text) if text is not None else await d.dialog.accept()
            outcome = f"accepted {d.type} dialog"
            if d.type == "prompt":
                outcome += f' with "{text if text is not None else d.default_value}"'
        elif response == "dismiss":
            await d.dialog.dismiss()
            outcome = f"dismissed {d.type} dialog"
        else:
            raise CommandError(
                f"unknown dialog response '{response}' — use accept, dismiss, or status",
                ExitCode.USAGE,
            )
        self._pending_dialogs.pop(self.page, None)  # active_dialog() ⇒ page is set
        # the "opened (blocking)" note was already shown on the opening action; drop it
        # so the replayed diff below doesn't tell the agent to resolve what it just did
        self._notes = []
        # the page is unblocked now; if a verb opened this dialog, emit that verb's
        # normal post-action diff, otherwise fall back to a fresh outline
        if d.action_line is not None and d.begin_state is not None:
            return f"{outcome}\n{await self._finish_action(d.action_line, d.begin_state)}"
        return f"{outcome}\n{await self.observe()}"

    _MAX_CAPTURE_EVENTS = 300

    def _capture_event(self, kind: str, **data: Any) -> None:
        """Record a page event for the next 'debug-capture' drain (harness
        tracing). Capped so an event storm can't grow memory between drains."""
        if len(self._capture_events) < self._MAX_CAPTURE_EVENTS:
            self._capture_events.append({"kind": kind, "ts": time.time(), "data": data})

    async def verb_debug_capture(self) -> str:
        """Machine-readable post-action state dump for external harnesses (eval
        tracing): browser state, viewport screenshot (base64 png), the
        DomSnapshot dict, and page events drained since the previous capture.
        Best-effort by design — each part degrades independently into
        payload['errors'] rather than failing the request, because a tracing
        capture must never break the run it is observing."""
        payload: dict[str, Any] = {
            "events": self._capture_events,
            "browser": {},
            "screenshot_b64": None,
            "dom_snapshot": None,
            "snapshot_reused": False,
            "errors": {},
        }
        self._capture_events = []
        errors: dict[str, str] = payload["errors"]
        page = self._page
        if self._context is None or page is None or page.is_closed():
            errors["browser"] = "no page open"
            return json.dumps(payload)

        browser: dict[str, Any] = {"url": page.url}
        payload["browser"] = browser
        blocked = self._active_dialog()
        if blocked is not None:
            # a native confirm/prompt freezes the renderer: title()/evaluate()/
            # screenshot() on this tab would hang, so report state-only
            browser["tabs"] = [
                {"index": i, "url": p.url[:300], "active": p is page}
                for i, p in enumerate(self._context.pages)
            ]
            browser["dialog"] = {"type": blocked.type, "message": blocked.message[:300]}
            errors["snapshot"] = "native dialog blocking the page"
            errors["screenshot"] = "native dialog blocking the page"
            return json.dumps(payload)

        with contextlib.suppress(Exception):
            browser["title"] = (await page.title())[:200]
        # tab titles are skipped for background tabs: one round trip per tab
        # per step is exactly the per-element chatter the architecture forbids
        browser["tabs"] = [
            {"index": i, "url": p.url[:300], "active": p is page}
            for i, p in enumerate(self._context.pages)
        ]

        snap = self.last_snapshot
        if snap is not None and self._snapshot_cmd_seq == self.cmd_seq:
            payload["snapshot_reused"] = True  # nothing possibly-mutating ran since
        else:
            try:
                snap = await capture(page, self.cfg.browser.capture_engine)
                self.last_snapshot = snap
                self._snapshot_cmd_seq = self.cmd_seq
            except Exception as e:
                snap = None
                errors["snapshot"] = _first_line(e)
        if snap is not None:
            payload["dom_snapshot"] = snap.to_dict()
            browser["viewport"] = {"width": snap.viewport[0], "height": snap.viewport[1]}
            browser["scroll_y"] = snap.scroll_y
            browser["doc_height"] = snap.doc_height

        try:
            png = await page.screenshot(full_page=False, timeout=10_000)
            payload["screenshot_b64"] = base64.b64encode(png).decode()
        except Exception as e:
            errors["screenshot"] = _first_line(e)
        return json.dumps(payload)

    async def verb_connect(self, target: str) -> str:
        url = target if "://" in target else f"http://127.0.0.1:{target}"
        # Probe before tearing down: a dead target must not cost the caller their
        # current session. (_ensure_browser re-probes, but only after close().)
        await _check_cdp_reachable(url)
        await self.close()
        self._cdp_url = url
        await self._ensure_browser()
        assert self._context is not None  # _ensure_browser postcondition
        return f"attached over CDP: {url} ({len(self._context.pages)} tab(s))"

    # ------------------------------------------------------------ helpers ----

    async def _settle(self) -> None:
        t0 = time.monotonic()
        fired = False
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("networkidle", timeout=3000)
            fired = True
        # not firing within the 3s cap is routine on busy pages (level=info,
        # not the wait_timeout anomaly — that one is for the quiesce cap)
        debug.emit("session", "wait", condition="networkidle", fired=fired,
                   dur_ms=round((time.monotonic() - t0) * 1000, 1))  # fmt: skip

    def _check_url_allowed(self, url: str, landed: bool = False) -> None:
        allowed = self.cfg.security.allowed_domains
        if not allowed:
            return
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if landed and parts.scheme not in ("http", "https"):
            return  # about:blank, chrome-error:// etc.
        host = parts.netloc.lower()
        if not any(host == d or host.endswith("." + d) for d in allowed):
            hint = (
                "run 'ebrowse back' or edit security.allowed_domains"
                if landed
                else "edit config to allow it"
            )
            raise CommandError(
                f"domain {host} not in security.allowed_domains — {hint}", ExitCode.USAGE
            )

    async def _resolve_locator(self, target: str):
        """@ref/CSS resolution for getters (refs delegate to core/locate.py;
        no occlusion pre-check — getters don't click)."""
        if not target.startswith("@"):
            loc = self.page.locator(target)
            if await loc.count() == 0:
                raise CommandError(f"no element matches CSS '{target}'", ExitCode.USAGE)
            return loc.first
        page_mem = self._require_page_mem()
        found = page_mem.find_element(target)
        if not found:
            raise CommandError(
                f"stale ref {target}: not on current page — run 'ebrowse outline'", ExitCode.USAGE
            )
        _, element = found
        from ebrowse.core.locate import resolve

        return await resolve(self.page, element.desc, ref=element.ref)


def _first_line(e: Exception) -> str:
    return str(e).splitlines()[0][:200]
