"""ebrowse doctor: environment checks with fix hints. Runs CLI-side (no daemon)."""

from __future__ import annotations

import os
import shutil
import sys

from ebrowse.config import cache_dir, config_path, load_config, runtime_dir, socket_path


def _line(status: str, name: str, detail: str) -> str:
    return f"[{status:^4}] {name}: {detail}"


def run_doctor() -> int:
    lines: list[str] = []
    failures = 0

    # python
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    lines.append(_line("ok" if ok else "FAIL", "python", f"{v.major}.{v.minor}.{v.micro}"))
    failures += 0 if ok else 1

    # playwright + chromium channel
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True, channel="chromium")
                browser.close()
                lines.append(_line("ok", "chromium", "full build launches (channel=chromium)"))
            except Exception as e:
                lines.append(
                    _line(
                        "FAIL",
                        "chromium",
                        f"{str(e).splitlines()[0][:120]} — run 'uv run playwright install chromium'",
                    )
                )
                failures += 1
    except ImportError:
        lines.append(_line("FAIL", "playwright", "not installed — run 'make setup'"))
        failures += 1

    # socket dir
    rd = runtime_dir()
    ok = os.access(rd, os.W_OK)
    lines.append(_line("ok" if ok else "FAIL", "socket dir", f"{rd} ({socket_path().name})"))
    failures += 0 if ok else 1

    # config
    cp = config_path()
    cfg = load_config()
    lines.append(_line("ok", "config", f"{cp} ({'present' if cp.is_file() else 'defaults'})"))

    # summarizer (warn-only: the tool is fully functional without it)
    if cfg.summarizer.enabled:
        try:
            import httpx

            r = httpx.get(f"{cfg.summarizer.base_url.rstrip('/')}/models", timeout=3)
            detail = f"{cfg.summarizer.base_url} (HTTP {r.status_code})"
            lines.append(_line("ok" if r.status_code < 500 else "warn", "summarizer", detail))
        except Exception as e:
            lines.append(
                _line(
                    "warn",
                    "summarizer",
                    f"{cfg.summarizer.base_url} unreachable ({type(e).__name__}) — "
                    "outlines fall back to deterministic labels",
                )
            )
    else:
        lines.append(_line("ok", "summarizer", "disabled in config"))

    # cdp (only if configured)
    if cfg.browser.mode == "cdp" or cfg.browser.cdp_url:
        lines.append(_line("ok", "cdp", f"configured: {cfg.browser.cdp_url or '(via connect)'}"))

    # daemon log location
    lines.append(_line("ok", "logs", str(cache_dir() / "daemon.log")))

    # xvfb hint for headed mode on servers
    if not cfg.browser.headless and not os.environ.get("DISPLAY"):
        has_xvfb = shutil.which("xvfb-run") is not None
        lines.append(
            _line(
                "warn",
                "display",
                "headless=false but no $DISPLAY"
                + ("; xvfb-run available" if has_xvfb else " and no xvfb-run"),
            )
        )

    print("\n".join(lines))
    if failures:
        print(f"\n{failures} check(s) failed")
    return 3 if failures else 0
