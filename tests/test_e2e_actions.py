"""End-to-end action-verb tests: CLI -> daemon -> chromium -> diff output."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixture_server import FixtureServer

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def server():
    with FixtureServer() as srv:
        yield srv


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    home = tmp_path_factory.mktemp("ebrowse_home_actions")
    real_browsers = Path(os.environ.get("HOME", "~")).expanduser() / ".cache" / "ms-playwright"
    e = os.environ.copy()
    e.update(
        {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_RUNTIME_DIR": str(home / ".run"),
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
                "PLAYWRIGHT_BROWSERS_PATH", str(real_browsers)
            ),
            "EBROWSE_SUMMARIZER_ENABLED": "false",
        }
    )
    (home / ".run").mkdir()
    yield e
    subprocess.run(
        [sys.executable, "-m", "ebrowse.cli.main", "daemon", "stop"],
        env=e,
        capture_output=True,
        timeout=15,
    )


def ebrowse(env, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ebrowse.cli.main", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def ref_for(env, sid: str, pattern: str) -> str:
    """Find the ref of the element whose rendered line matches `pattern`."""
    out = ebrowse(env, "expand", sid, "--all").stdout
    m = re.search(pattern + r"[^)\]]*\((@e\d+)", out)
    assert m, f"no element matching {pattern!r} in {sid}:\n{out}"
    return m.group(1)


def test_dropdown_click_reveals_options(server, env):
    r = ebrowse(env, "open", server.url("dropdown.html"))
    assert r.returncode == 0, r.stderr
    btn = ref_for(env, "s2", r"Sort by: Relevance")
    r = ebrowse(env, "click", btn)
    assert r.returncode == 0, r.stderr
    assert "→ partial change" in r.stdout
    assert "Price: high to low" in r.stdout  # revealed option with a fresh ref
    assert re.search(r"~ @e\d+ expanded: \"false\" → \"true\"", r.stdout)


def test_click_revealed_option(server, env):
    opt = ref_for(env, "s2", r"Average rating")
    r = ebrowse(env, "click", opt)
    assert r.returncode == 0, r.stderr
    assert "→ partial change" in r.stdout


def test_native_select(server, env):
    sel = ref_for(env, "s2", r"Results per page")
    r = ebrowse(env, "select", sel, "50")
    assert r.returncode == 0, r.stderr
    assert 'value: "25" → "50"' in r.stdout


def test_form_fill_and_submit_quotes_new_text(server, env):
    ebrowse(env, "open", server.url("form.html"))
    for pattern, value in [
        (r"Full name", "Jayoo"),
        (r"Email address", "jay@example.com"),
        (r"Password", "hunter2hunter2"),
    ]:
        ref = ref_for(env, "s2", rf"\[{pattern}")
        r = ebrowse(env, "fill", ref, value)
        assert r.returncode == 0, r.stderr
        assert "value:" in r.stdout
    # password value is masked, never echoed
    assert "hunter2hunter2" not in ebrowse(env, "expand", "s2").stdout
    tos = ref_for(env, "s2", r"I agree to the")
    assert ebrowse(env, "check", tos).returncode == 0
    submit = ref_for(env, "s2", r"\[Create account")
    r = ebrowse(env, "click", submit)
    assert r.returncode == 0, r.stderr
    assert "Account created!" in r.stdout


def test_native_alert_auto_accepted_with_note(server, env):
    ebrowse(env, "open", server.url("dialogs.html"))
    r = ebrowse(env, "click", "#alert-btn")
    assert r.returncode == 0, r.stderr
    assert "note: native alert auto-accepted" in r.stdout
    assert "Alert was shown." in r.stdout


def test_occluded_click_blocked(server, env):
    ebrowse(env, "click", "#modal-btn")
    r = ebrowse(env, "click", "#covered-btn")
    assert r.returncode == 1
    assert "covered by" in r.stderr
    # dismiss the modal, then the click goes through
    r = ebrowse(env, "click", "#accept-cookies")
    assert r.returncode == 0
    r = ebrowse(env, "click", "#covered-btn")
    assert r.returncode == 0, r.stderr
    assert "Purchase started." in r.stdout


def test_spa_mutation_and_noop(server, env):
    ebrowse(env, "open", server.url("spa.html"))
    inp = ref_for(env, "s2", r"New task title")
    add = ref_for(env, "s2", r"\[Add task")
    ebrowse(env, "fill", inp, "Buy beans")
    r = ebrowse(env, "click", add)
    assert r.returncode == 0, r.stderr
    assert "→ partial change" in r.stdout and "Buy beans" in r.stdout
    r = ebrowse(env, "click", "#noop-btn")
    assert r.returncode == 0
    assert "no change detected" in r.stdout


def test_spa_route_swap_shows_sections(server, env):
    stats = ref_for(env, "s1", r"\[Stats")
    r = ebrowse(env, "click", stats)
    assert r.returncode == 0, r.stderr
    assert "[appeared]" in r.stdout
    assert "disappeared" in r.stdout


def test_iframe_form_flow(server, env):
    ebrowse(env, "open", server.url("iframe.html"))
    card = ref_for(env, "s2", r"Card number")
    r = ebrowse(env, "fill", card, "4242 4242 4242 4242")
    assert r.returncode == 0, r.stderr
    pay = ref_for(env, "s2", r"\[Pay")
    r = ebrowse(env, "click", pay)
    assert r.returncode == 0, r.stderr
    assert "Payment accepted." in r.stdout


def test_link_click_is_navigation_with_unchanged_marks(server, env):
    ebrowse(env, "open", server.url("list.html"))
    ebrowse(env, "open", server.url("form.html"))
    link = ref_for(env, "s1", r"\[Products")
    r = ebrowse(env, "click", link)
    assert r.returncode == 0, r.stderr
    assert "→ navigation" in r.stdout
    assert "PAGE Espresso Gear" in r.stdout


def test_scroll_reports_position(server, env):
    ebrowse(env, "open", server.url("huge.html"))
    r = ebrowse(env, "scroll", "down", "--pages", "2")
    assert r.returncode == 0, r.stderr
    assert re.search(r"scroll position y=\d+", r.stdout)


def test_eval_returns_result(server, env):
    r = ebrowse(env, "eval", "1 + 41")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("result: 42")


def test_stale_ref_errors_cleanly(server, env):
    r = ebrowse(env, "click", "@e99999")
    assert r.returncode == 2
    assert "stale ref" in r.stderr or "outline" in r.stderr
