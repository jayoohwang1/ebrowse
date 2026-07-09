"""Summarizer tests: parsing/sanitizing (pure), client+batch against a mock
server, cache round-trips, and the e2e outline flow with ≈ labels."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ebrowse.config import ObserveConfig, SummarizerConfig
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import DomSnapshot
from ebrowse.summarize.batch import (
    build_messages,
    caption_screen,
    parse_summaries,
    summarize_page,
)
from ebrowse.summarize.cache import SummaryCache
from ebrowse.summarize.client import SummarizerClient
from tests.fixture_server import FixtureServer
from tests.mock_summarizer import MockSummarizer

SNAPSHOT_DIR = Path(__file__).parent / "fixtures" / "domsnapshots"


def _page(name: str = "list"):
    snap = DomSnapshot.from_dict(json.loads((SNAPSHOT_DIR / f"{name}.json").read_text()))
    return build_page(snap, RefRegistry(), ObserveConfig(), captured_at=0.0)[0]


# ------------------------------------------------------------------ pure ----


def test_parse_summaries_tolerates_fences_and_junk():
    valid = {"s1", "s2"}
    raw = 'Here you go:\n```json\n[{"sid": "s1", "summary": "Header nav"}, {"sid": "s9", "summary": "bogus"}, {"sid": "s2", "summary": "  \\"Filter form\\"  "}]\n```'
    out = parse_summaries(raw, valid)
    assert out == {"s1": "Header nav", "s2": "Filter form"}
    assert parse_summaries("no json here", valid) == {}
    assert parse_summaries('[{"sid": "s1"}]', valid) == {}


def test_parse_summaries_salvages_truncated_array():
    # Reasoning models often exhaust the token budget mid-array; the closing
    # `]` and the final row are missing. We must keep the complete rows, not
    # drop the whole page (the 0/N regression this guards against).
    valid = {"s1", "s2", "s3"}
    raw = (
        '[{"sid": "s1", "summary": "Header nav"}, '
        '{"sid": "s2", "summary": "Filter form"}, '
        '{"sid": "s3", "summary": "Product li'  # cut off here, no closing brace/bracket
    )
    assert parse_summaries(raw, valid) == {"s1": "Header nav", "s2": "Filter form"}
    # fenced + truncated together
    fenced = '```json\n[{"sid": "s1", "summary": "Header nav"}, {"sid": "s2", "summ'
    assert parse_summaries(fenced, valid) == {"s1": "Header nav"}


def test_parse_summaries_sanitizes():
    out = parse_summaries(
        '[{"sid": "s1", "summary": "line\\nwith (@e5) ref and ctrl\\u0007 chars '
        + "x" * 300
        + '"}]',
        {"s1"},
    )
    s = out["s1"]
    assert "\n" not in s and "(@e5)" not in s and "\x07" not in s
    assert len(s) <= 140


def test_build_messages_budgets_and_skips_cross_origin():
    page = _page("list")
    texts = {s.sid: "word " * 2000 for s in page.sections}
    msgs = build_messages(page, texts, max_input_tokens=500)
    assert msgs[0]["role"] == "system"
    assert len(msgs[1]["content"]) < 500 * 4 + 2500  # budget respected (plus digest overhead)
    assert "s1 type=" in msgs[1]["content"]


def test_cache_round_trip(tmp_path):
    cache = SummaryCache(str(tmp_path / "s.db"))
    cache.put_many({"h1": "one", "h2": "two"})
    assert cache.get_many(["h1", "h2", "h3"]) == {"h1": "one", "h2": "two"}
    cache.put_caption("img1", "a red bicycle")
    assert cache.get_caption("img1") == "a red bicycle"
    assert cache.get_caption("img2") is None
    cache.put_screen("scr1", "a checkout page with a cookie banner")
    assert cache.get_screen("scr1") == "a checkout page with a cookie banner"
    assert cache.get_screen("scr2") is None
    cache.close()


# ------------------------------------------------------- mock-server paths ----


async def test_batch_against_mock_server():
    page = _page("list")
    with MockSummarizer() as mock:
        client = SummarizerClient(SummarizerConfig(base_url=mock.base_url, timeout_s=10))
        out = await summarize_page(client, page, {}, max_input_tokens=10_000)
        await client.aclose()
    assert out
    for sid, summary in out.items():
        assert summary == f"MOCK {sid} summary"
    assert len(mock.requests) == 1  # ONE batched call for the whole page


async def test_caption_screen_uses_default_and_custom_prompt():
    with MockSummarizer() as mock:
        client = SummarizerClient(
            SummarizerConfig(base_url=mock.base_url, timeout_s=10, vision=True)
        )
        # default gist (no prompt) — server sees an image request, returns the gist
        gist = await caption_screen(client, "ZmFrZQ==")
        assert gist == "MOCK visual gist: a page with no overlays"
        assert _is_image_body(mock.requests[-1])
        # a custom prompt rides in the same image message
        gist2 = await caption_screen(client, "ZmFrZQ==", prompt="what color is the button?")
        assert gist2
        texts = [
            p["text"]
            for p in mock.requests[-1]["messages"][0]["content"]
            if p.get("type") == "text"
        ]
        assert texts == ["what color is the button?"]
        await client.aclose()


async def test_caption_screen_disabled_when_vision_off():
    with MockSummarizer() as mock:
        client = SummarizerClient(
            SummarizerConfig(base_url=mock.base_url, timeout_s=10, vision=False)
        )
        assert await caption_screen(client, "ZmFrZQ==") is None
        assert not mock.requests  # never hit the server
        await client.aclose()


def _is_image_body(body: dict) -> bool:
    content = body["messages"][0]["content"]
    return isinstance(content, list) and any(p.get("type") == "image_url" for p in content)


async def test_extra_body_merged_into_request():
    page = _page("list")
    extra = {"chat_template_kwargs": {"enable_thinking": False}}
    with MockSummarizer() as mock:
        client = SummarizerClient(
            SummarizerConfig(base_url=mock.base_url, timeout_s=10, extra_body=extra)
        )
        await summarize_page(client, page, {}, max_input_tokens=10_000)
        await client.aclose()
    assert mock.requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert mock.requests[0]["temperature"] == 0  # base fields still present


async def test_circuit_breaker_opens_after_failures():
    page = _page("article")
    with MockSummarizer(fail_times=99) as mock:
        client = SummarizerClient(SummarizerConfig(base_url=mock.base_url, timeout_s=5))
        for _ in range(3):
            assert await summarize_page(client, page, {}, 10_000) == {}
        assert not client.available  # breaker open
        n_before = len(mock.requests)
        assert await summarize_page(client, page, {}, 10_000) == {}
        assert len(mock.requests) == n_before  # no further traffic while open
        await client.aclose()


# ------------------------------------------------------------------- e2e ----


@pytest.mark.e2e
def test_outline_shows_mock_summaries(tmp_path):
    home = tmp_path / "home"
    (home / ".run").mkdir(parents=True)
    real_browsers = Path(os.environ.get("HOME", "~")).expanduser() / ".cache" / "ms-playwright"
    with FixtureServer() as srv, MockSummarizer() as mock:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_RUNTIME_DIR": str(home / ".run"),
                "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
                    "PLAYWRIGHT_BROWSERS_PATH", str(real_browsers)
                ),
                "EBROWSE_SUMMARIZER_BASE_URL": mock.base_url,
            }
        )

        def run(*args):
            return subprocess.run(
                [sys.executable, "-m", "ebrowse.cli.main", *args],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )

        try:
            # navigation lands; it does NOT run the summarizer
            r = run("open", srv.url("article.html"))
            assert r.returncode == 0, r.stderr
            assert r.stdout.startswith("opened ")
            assert "≈" not in r.stdout and "◉" not in r.stdout

            # the explicit outline synchronously fills ≈ labels AND the ◉ gist,
            # in one shot (no async "backfill running" phase)
            r = run("outline")
            assert r.returncode == 0, r.stderr
            assert re.search(r"≈ MOCK s\d+ summary", r.stdout)
            assert "◉ MOCK visual gist" in r.stdout
            assert "backfill" not in r.stdout

            # opt-outs
            r = run("outline", "--no-summaries")
            assert "≈" not in r.stdout and '| "' in r.stdout
            assert "◉ MOCK visual gist" in r.stdout  # glance still shown
            r = run("outline", "--no-glance")
            assert "◉" not in r.stdout and re.search(r"≈ MOCK s\d+ summary", r.stdout)

            # describe-screen: no prompt → the cached/default gist
            r = run("describe-screen")
            assert r.returncode == 0, r.stderr
            assert r.stdout.strip().startswith("◉ MOCK visual gist")

            # describe-screen with a free-form prompt reaches the VLM
            r = run("describe-screen", "list every button")
            assert r.returncode == 0, r.stderr
            assert r.stdout.strip().startswith("◉ ")
        finally:
            run("daemon", "stop")
