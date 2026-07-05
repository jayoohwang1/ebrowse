"""End-to-end tests: real CLI -> autostarted daemon -> headless chromium.

Each test module run gets an isolated HOME/XDG so the daemon, socket, profile,
and config never touch the user's real ones.
"""

from __future__ import annotations

import os
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
    home = tmp_path_factory.mktemp("ebrowse_home")
    # keep Playwright's browser binaries findable despite the fake HOME
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


def test_open_returns_outline(server, env):
    r = ebrowse(env, "open", server.url("list.html"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("PAGE Espresso Gear")
    assert "s4 list" in r.stdout and "32 items" in r.stdout


def test_expand_section(server, env):
    r = ebrowse(env, "expand", "s2")
    assert r.returncode == 0, r.stderr
    assert "## s2 form" in r.stdout
    assert "(@e" in r.stdout


def test_expand_unknown_section_names_recovery(server, env):
    r = ebrowse(env, "expand", "s99")
    assert r.returncode == 2
    assert "outline" in r.stderr  # recovery hint


def test_get_title_and_url(server, env):
    assert "Fixture Shop" in ebrowse(env, "get", "title").stdout
    assert "list.html" in ebrowse(env, "get", "url").stdout


def test_get_value_by_ref(server, env):
    import re

    r = ebrowse(env, "expand", "s1")
    m = re.search(r"\[Search products \((@e\d+)", r.stdout)
    assert m, f"search input not found in: {r.stdout}"
    r2 = ebrowse(env, "get", "value", m.group(1))
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip() == ""


def test_screenshot_section(server, env, tmp_path):
    out = tmp_path / "sec.png"
    r = ebrowse(env, "screenshot", "--section", "s4", "-o", str(out))
    assert r.returncode == 0, r.stderr
    assert out.is_file() and out.stat().st_size > 5000


def test_navigation_updates_outline(server, env):
    r = ebrowse(env, "open", server.url("form.html"))
    assert "Create Account" in r.stdout
    r = ebrowse(env, "back")
    assert r.returncode == 0, r.stderr
    assert "Espresso Gear" in r.stdout


def test_refs_stable_across_navigation(server, env):
    """The shared header nav links must keep refs across page loads."""
    out1 = ebrowse(env, "open", server.url("list.html")).stdout
    exp1 = ebrowse(env, "expand", "s1").stdout
    ebrowse(env, "open", server.url("form.html"))
    exp2 = ebrowse(env, "expand", "s1").stdout
    del out1

    def ref_of(md: str, needle: str) -> str | None:
        for line in md.splitlines():
            if needle in line:
                after = line.split(needle, 1)[1]
                if "(@e" in after:
                    return after.split("(@", 1)[1].split(")")[0]
        return None

    ref_products_1 = ref_of(exp1, "Products")
    ref_products_2 = ref_of(exp2, "Products")
    assert ref_products_1 and ref_products_1 == ref_products_2


def test_bad_url_fails_cleanly(server, env):
    r = ebrowse(env, "open", "http://127.0.0.1:1/nope")
    assert r.returncode == 1
    assert "navigation failed" in r.stderr


def test_daemon_status_and_stop(server, env):
    r = ebrowse(env, "daemon", "status")
    assert "session default" in r.stdout
    r = ebrowse(env, "daemon", "stop")
    assert r.returncode == 0
