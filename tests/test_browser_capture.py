"""Browser-marked tests: live discover.js capture against the fixture server."""

from __future__ import annotations

import time

import pytest

from ebrowse.config import ObserveConfig
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import capture
from tests.fixture_server import FixtureServer

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def server():
    with FixtureServer() as srv:
        yield srv


# NOTE: browser/page fixtures are function-scoped on purpose. Module-scoped
# async fixtures deadlock under pytest-asyncio's per-function event loop (the
# browser's loop dies with the first test). ~300ms launch per test is fine.
@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page(viewport={"width": 1280, "height": 1280})
    yield page
    await browser.close()
    await pw.stop()


async def test_capture_shape(server, page):
    await page.goto(server.url("form.html"))
    snap = await capture(page)
    assert snap.title == "Create Account — Fixture Shop"
    assert snap.viewport == (1280, 1280)
    assert not snap.truncated
    tags = {n.tag for n in snap.root.walk()}
    assert {"header", "form", "input", "select", "button"} <= tags
    # curated attrs made it through
    selects = [n for n in snap.root.walk() if n.tag == "select"]
    assert selects and "United States" in (selects[0].attrs.get("opt") or [])
    # label[for] resolved into accessible names
    named = {n.attrs.get("nm") for n in snap.root.walk() if n.attrs.get("nm")}
    assert "Full name" in named


async def test_capture_prunes_hidden(server, page):
    await page.goto(server.url("dialogs.html"))
    snap = await capture(page)
    # the cookie modal is display:none until opened — must not appear
    texts = " ".join(n.text for n in snap.root.walk() if n.text)
    assert "We use cookies" not in texts
    # open it via the page and re-capture
    await page.click("#modal-btn")
    snap2 = await capture(page)
    texts2 = " ".join(n.text for n in snap2.root.walk() if n.text)
    assert "We use cookies" in texts2


async def test_capture_stitches_same_origin_iframe(server, page):
    await page.goto(server.url("iframe.html"))
    await page.wait_for_selector("#payframe")
    snap = await capture(page)
    framed = [n for n in snap.root.walk() if n.iframe_path]
    assert framed, "iframe children should be stitched with iframe_path set"
    phs = {n.attrs.get("ph") for n in framed if n.attrs.get("ph")}
    assert "MM/YY" in phs
    # coordinates offset into page space: frame content sits below the h1
    card = next(n for n in framed if n.attrs.get("ph") == "4242 4242 4242 4242")
    assert card.rect[1] > 100


async def test_capture_performance_on_huge_page(server, page):
    await page.goto(server.url("huge.html"))
    start = time.monotonic()
    snap = await capture(page)
    elapsed = time.monotonic() - start
    assert elapsed < 1.5, f"capture took {elapsed:.2f}s"
    page_mem, _ = build_page(snap, RefRegistry(), ObserveConfig())
    assert len(page_mem.sections) <= ObserveConfig().max_sections


async def test_dropdown_reveal_appears_in_recapture(server, page):
    """Pre-Phase-3 sanity for the diff story: hidden menu items appear after click."""
    await page.goto(server.url("dropdown.html"))
    snap1 = await capture(page)
    n_before = sum(1 for n in snap1.root.walk() if n.attrs.get("role") == "menuitem")
    assert n_before == 0
    await page.click("#sort-btn")
    snap2 = await capture(page)
    n_after = sum(1 for n in snap2.root.walk() if n.attrs.get("role") == "menuitem")
    assert n_after == 5
