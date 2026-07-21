"""Browser-marked cross-engine parity: cdp capture vs the discover.js walk.

The CDP engine (ADR 0015) must produce byte-identical rendered outlines on
every fixture page — same sections, same refs, same names, same state. Any
divergence here is a translator bug, not an acceptable difference.
"""

from __future__ import annotations

import pytest

from ebrowse.config import ObserveConfig
from ebrowse.core import render
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import capture
from tests.fixture_server import FixtureServer

pytestmark = pytest.mark.browser

PAGES = [
    "form.html",
    "aria_widgets.html",
    "iframe.html",
    "iframe_noid.html",
    "table.html",
    "custom_widgets.html",
    "article.html",
    "list.html",
    "dropdown.html",
    "covers.html",
    "disabled_states.html",
    "dialogs.html",
    "huge.html",
    "anonymous.html",
    "identical_row.html",
    "scrollpanel.html",
    "shadow_slots.html",
]


@pytest.fixture(scope="module")
def server():
    with FixtureServer() as srv:
        yield srv


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page(viewport={"width": 1280, "height": 1280})
    yield page
    await browser.close()
    await pw.stop()


def _outline(snap) -> str:
    pm, _ = build_page(snap, RefRegistry(), ObserveConfig())
    return render.render_outline(pm)


@pytest.mark.parametrize("name", PAGES)
async def test_outline_parity(server, page, name):
    await page.goto(server.url(name), wait_until="load")
    js_outline = _outline(await capture(page, engine="js"))
    cdp_outline = _outline(await capture(page, engine="cdp"))
    assert cdp_outline == js_outline


async def test_cdp_engine_binds_nodes(server, page):
    await page.goto(server.url("form.html"), wait_until="load")
    snap = await capture(page, engine="cdp")
    bound = [n for n in snap.root.walk() if n.backend_node_id is not None]
    assert bound, "cdp captures carry backend node bindings"


async def test_scrolled_coordinates_are_absolute(server, page):
    await page.goto(server.url("huge.html"), wait_until="load")
    await page.evaluate("window.scrollTo(0, 2000)")
    js_snap = await capture(page, engine="js")
    cdp_snap = await capture(page, engine="cdp")
    assert cdp_snap.scroll_y == js_snap.scroll_y == 2000
    a = [n.rect for n in js_snap.root.walk() if n.signals][:10]
    b = [n.rect for n in cdp_snap.root.walk() if n.signals][:10]
    for ra, rb in zip(a, b, strict=True):
        # sub-pixel rounding may differ by 1px; document-absolute must hold
        assert all(abs(x - y) <= 1 for x, y in zip(ra, rb, strict=True))


async def test_inner_scroll_state_parity(server, page):
    """Pins the scrollRects-as-scroll-offset interpretation: a pre-scrolled
    inner container must report the same scr=[scrollTop, maxScroll] from both
    engines (a wrong value silently corrupts 'more below the fold' hints)."""
    await page.goto(server.url("scrollpanel.html"), wait_until="load")

    def scr_of(snap):
        for n in snap.root.walk():
            if n.attrs.get("scr"):
                return n.attrs["scr"]
        return None

    js_scr = scr_of(await capture(page, engine="js"))
    cdp_scr = scr_of(await capture(page, engine="cdp"))
    assert js_scr is not None and js_scr[0] > 0, js_scr  # the panel IS scrolled
    assert cdp_scr is not None, "cdp engine missed the scroll container"
    assert abs(cdp_scr[0] - js_scr[0]) <= 1 and abs(cdp_scr[1] - js_scr[1]) <= 1, (
        js_scr,
        cdp_scr,
    )
