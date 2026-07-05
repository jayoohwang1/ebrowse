"""Dev harness: exercise the core page model without the daemon.

    uv run python -m ebrowse.dev <url> outline
    uv run python -m ebrowse.dev <url> expand s2 [--cursor N]
    uv run python -m ebrowse.dev <url> capture out.json     # save DomSnapshot fixture
    uv run python -m ebrowse.dev <url> stats                # token accounting vs aria

Launches its own headless chromium per invocation (slow but stateless).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path

from ebrowse.config import load_config
from ebrowse.core import render
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import capture
from ebrowse.model import estimate_tokens


# Default headless UA advertises HeadlessChrome, which basic bot filters
# (e.g. Akamai on traderjoes.com) reject outright. A plain Chrome UA plus
# language/timezone gets past UA-string checks; this is NOT stealth tooling,
# just not volunteering to be blocked. Shared with the daemon later.
def context_kwargs() -> dict:
    return {
        "viewport": {"width": 1280, "height": 1280},
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
        ),
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
    }


async def _with_page(url: str):
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    # channel="chromium" forces the FULL chromium build in new-headless mode.
    # Playwright's default headless build (chrome-headless-shell) is rejected
    # by Akamai-fronted sites (traderjoes.com, drugs.com) even with a normal
    # UA; the full build passes. Keep this in sync with the daemon's launcher.
    browser = await pw.chromium.launch(
        headless=True,
        channel="chromium",
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = await browser.new_page(**context_kwargs())
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=4000)
    return pw, browser, page


async def run(url: str, cmd: str, arg: str | None, cursor: int) -> int:
    cfg = load_config()
    pw, browser, page = await _with_page(url)
    try:
        snap = await capture(page)
        if cmd == "capture":
            out = Path(arg or "snapshot.json")
            out.write_text(json.dumps(snap.to_dict(), indent=1))
            print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
            return 0

        registry = RefRegistry()
        pagemem, raw_by_sid = build_page(snap, registry, cfg.observe)

        if cmd == "outline":
            print(render.render_outline(pagemem))
        elif cmd == "expand":
            if not arg:
                print("error: expand needs a section id (e.g. s2)", file=sys.stderr)
                return 2
            section = pagemem.section(arg)
            if section is None:
                print(
                    f"error: no section {arg} — sections: "
                    f"{', '.join(s.sid for s in pagemem.sections)}",
                    file=sys.stderr,
                )
                return 2
            print(render.render_section_markdown(section, raw_by_sid[arg], cfg.observe, cursor))
        elif cmd == "stats":
            outline = render.render_outline(pagemem)
            aria = await page.locator("body").aria_snapshot()
            full = sum(s.token_estimate for s in pagemem.sections)
            print(f"sections:        {len(pagemem.sections)}")
            print(f"elements:        {sum(len(s.elements) for s in pagemem.sections)}")
            print(f"outline tokens:  {estimate_tokens(outline)}")
            print(f"aria tokens:     {estimate_tokens(aria)}")
            print(f"all-expand toks: {full}")
            ratio = estimate_tokens(outline) / max(estimate_tokens(aria), 1)
            print(f"outline/aria:    {ratio:.1%}")
        else:
            print(f"error: unknown command {cmd}", file=sys.stderr)
            return 2
        return 0
    finally:
        await browser.close()
        await pw.stop()


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m ebrowse.dev")
    ap.add_argument("url")
    ap.add_argument("cmd", choices=["outline", "expand", "capture", "stats"])
    ap.add_argument("arg", nargs="?", help="section id (expand) or output path (capture)")
    ap.add_argument("--cursor", type=int, default=0)
    args = ap.parse_args()
    url = args.url if "://" in args.url else f"https://{args.url}"
    return asyncio.run(run(url, args.cmd, args.arg, args.cursor))


if __name__ == "__main__":
    sys.exit(main())
