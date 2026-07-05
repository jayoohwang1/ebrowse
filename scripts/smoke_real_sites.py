"""Real-site smoke run: outlines + token accounting on Online-Mind2Web hosts.

Manual tool (network + third-party sites, so not part of pytest):
    uv run python scripts/smoke_real_sites.py [--sites N]

Prints per-site: section count, outline/aria token ratio, and flags suspect
splits (1-section pages, >45 sections). Sites are bot-friendly per the
Online-Mind2Web dataset; interaction here is read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ebrowse.config import load_config
from ebrowse.core import render
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import capture
from ebrowse.dev import _with_page
from ebrowse.model import estimate_tokens

SITES = [
    "https://www.traderjoes.com",
    "https://www.recreation.gov",
    "https://www.drugs.com",
    "https://www.bestbuy.com",
    "https://www.cars.com",
    "https://www.accuweather.com",
    "https://www.apple.com",
    "https://www.healthline.com",
    "https://www.ups.com",
    "https://www.akc.org",
]


async def check(url: str) -> str:
    cfg = load_config()
    pw, browser, page = await _with_page(url)
    try:
        snap = await capture(page)
        pagemem, _ = build_page(snap, RefRegistry(), cfg.observe)
        outline = render.render_outline(pagemem)
        aria = await page.locator("body").aria_snapshot()
        ot, at = estimate_tokens(outline), estimate_tokens(aria)
        n = len(pagemem.sections)
        elems = sum(len(s.elements) for s in pagemem.sections)
        flags = []
        if n <= 1:
            flags.append("SINGLE-SECTION")
        if n >= 45:
            flags.append("MANY-SECTIONS")
        if "Access Denied" in pagemem.title or "denied" in outline[:300].lower():
            flags.append("BLOCKED")
        flag = f"  << {' '.join(flags)}" if flags else ""
        return (
            f"{url:40s} sections={n:3d} elements={elems:4d} "
            f"outline={ot:5d}t aria={at:6d}t ratio={ot / max(at, 1):5.1%}{flag}"
        )
    finally:
        await browser.close()
        await pw.stop()


async def main(n: int) -> None:
    for url in SITES[:n]:
        try:
            print(await asyncio.wait_for(check(url), timeout=120))
        except Exception as e:
            print(f"{url:40s} FAILED: {type(e).__name__}: {str(e)[:80]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", type=int, default=len(SITES))
    sys.exit(asyncio.run(main(ap.parse_args().sites)))
