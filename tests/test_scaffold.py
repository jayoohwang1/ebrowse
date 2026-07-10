"""Scaffold smoke tests: model round-trips, config loading, CLI stub, fixture server."""

from __future__ import annotations

import urllib.request

from ebrowse.cli.main import build_parser, main
from ebrowse.config import Config, load_config
from ebrowse.model import (
    BBox,
    Diff,
    Element,
    ElementDesc,
    ElementState,
    PageMem,
    Section,
    SectionDiff,
    estimate_tokens,
)
from tests.fixture_server import PAGES_DIR, FixtureServer

FIXTURE_PAGES = [
    "article.html",
    "form.html",
    "list.html",
    "table.html",
    "dropdown.html",
    "spa.html",
    "iframe.html",
    "frame_child.html",
    "dialogs.html",
    "huge.html",
]


def _sample_page() -> PageMem:
    desc = ElementDesc(tag="button", role="button", name="Add to cart", text_head="Add to cart")
    el = Element(
        ref="@e1",
        desc=desc,
        state=ElementState(bbox=BBox(10, 20, 100, 30), value=None),
    )
    section = Section(
        sid="s1",
        fingerprint="fp_abc",
        type="content",
        heading="Products",
        preview="Espresso gear and more",
        elements=[el],
        content_hash="h1",
        token_estimate=42,
        bbox=BBox(0, 0, 1280, 600),
    )
    return PageMem(url="http://x/", title="X", sections=[section], captured_at=1.0, nav_id=0)


def test_model_round_trip():
    page = _sample_page()
    restored = PageMem.from_dict(page.to_dict())
    assert restored.to_dict() == page.to_dict()
    assert restored.sections[0].elements[0].ref == "@e1"
    assert restored.find_element("@e1") is not None
    assert restored.section("s1").counts_desc() == "1 button"
    old_payload = page.to_dict()
    old_payload.pop("truncated")
    assert PageMem.from_dict(old_payload).truncated is False


def test_diff_round_trip():
    page = _sample_page()
    sd = SectionDiff(
        sid="s1",
        kind="changed",
        added=[page.sections[0].elements[0]],
        removed=[page.sections[0].elements[0].desc],
        state_changes=[("@e1", "value", "", "hello")],
    )
    diff = Diff(kind="partial", sections=[sd], notes=["note"])
    restored = Diff.from_dict(diff.to_dict())
    assert restored.to_dict() == diff.to_dict()


def test_desc_match_key_ignores_nth():
    a = ElementDesc(tag="a", href="/x", nth_hint=0)
    b = ElementDesc(tag="a", href="/x", nth_hint=3)
    assert a.match_key() == b.match_key()


def test_estimate_tokens():
    assert estimate_tokens("abcd" * 10) == 10
    assert estimate_tokens("") == 1


def test_config_defaults_and_toml(tmp_path):
    assert load_config(tmp_path / "missing.toml") == Config()
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[summarizer]
base_url = "http://localhost:9999/v1"
enabled = false

[observe]
preview_chars = 80
unknown_key = 1

[bogus_section]
x = 1
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.summarizer.base_url == "http://localhost:9999/v1"
    assert cfg.summarizer.enabled is False
    assert cfg.observe.preview_chars == 80
    assert cfg.observe.quiescence_ms == 300  # untouched default


def test_config_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("EBROWSE_BROWSER_HEADLESS", "false")
    monkeypatch.setenv("EBROWSE_OBSERVE_MAX_SECTIONS", "10")
    monkeypatch.setenv("EBROWSE_OBSERVE_MAX_SECTION_TOKENS", "2048")
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.browser.headless is False
    assert cfg.observe.max_sections == 10
    assert cfg.observe.max_section_tokens == 2048


def test_cli_parses_all_verbs():
    parser = build_parser()
    for argv in [
        ["open", "http://x"],
        ["outline", "--no-summaries"],
        ["expand", "s3", "--cursor", "20"],
        ["click", "@e1", "--double"],
        ["fill", "@e2", "hi"],
        ["type", "@e2", "hi", "--enter"],
        ["press", "Enter"],
        ["scroll", "down", "--pages", "2"],
        ["get", "value", "@e3"],
        ["screenshot", "--section", "s1"],
        ["daemon", "status"],
        ["doctor"],
        ["select", "@e7", "Canada"],
        ["upload", "@e9", "a.pdf", "b.pdf"],
        ["tab", "2"],
    ]:
        args = parser.parse_args(argv)
        assert args.verb


def test_cli_unknown_verb_exits_2(capsys):
    import pytest as _pytest

    with _pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_cli_help_exits_0(capsys):
    rc = main([])
    assert rc == 0
    assert "outline" in capsys.readouterr().out


def test_fixture_pages_exist():
    for page in FIXTURE_PAGES:
        assert (PAGES_DIR / page).is_file(), page


def test_fixture_server_serves():
    with FixtureServer() as srv, urllib.request.urlopen(srv.url("form.html"), timeout=5) as resp:
        body = resp.read().decode()
    assert "Create your account" in body


def test_allowed_domains_enforced_on_landed_urls():
    """security.allowed_domains must catch link-click/redirect navigations, not
    just opened URLs — observe() checks the landed URL (landed=True)."""
    import pytest

    from ebrowse.errors import CommandError
    from ebrowse.session import Session

    cfg = Config()
    cfg.security.allowed_domains = ["example.com"]
    s = Session("t", cfg)
    s._check_url_allowed("https://example.com/x")  # exact domain ok
    s._check_url_allowed("https://shop.example.com/")  # subdomain ok
    s._check_url_allowed("about:blank", landed=True)  # non-http landed pages skip
    with pytest.raises(CommandError) as ei:
        s._check_url_allowed("https://evil.test/", landed=True)
    assert "ebrowse back" in str(ei.value)
    with pytest.raises(CommandError):
        s._check_url_allowed("https://evil.test/")
