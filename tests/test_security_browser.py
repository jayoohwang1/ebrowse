"""Browser-level enforcement of security.allowed_domains."""

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
    with FixtureServer() as value:
        yield value


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    home = tmp_path_factory.mktemp("ebrowse_navigation_policy")
    real_browsers = Path(os.environ.get("HOME", "~")).expanduser() / ".cache" / "ms-playwright"
    value = os.environ.copy()
    value.update(
        {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_RUNTIME_DIR": str(home / ".run"),
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
                "PLAYWRIGHT_BROWSERS_PATH", str(real_browsers)
            ),
            "EBROWSE_SUMMARIZER_ENABLED": "false",
            "EBROWSE_SECURITY_ALLOWED_DOMAINS": "127.0.0.1",
        }
    )
    (home / ".run").mkdir()
    yield value
    _run(value, "daemon", "stop")


def _run(env, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ebrowse.cli.main", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_click_navigation_to_disallowed_host_is_blocked(server, env):
    opened = _run(env, "open", server.url("navigation_policy.html"))
    assert opened.returncode == 0, opened.stderr
    result = _run(env, "click", "#leave")
    assert result.returncode != 0
    assert "navigation blocked" in result.stderr
    current = _run(env, "get", "url")
    assert current.stdout.strip() == server.url("navigation_policy.html")


def test_redirect_to_disallowed_host_is_blocked(server, env):
    result = _run(env, "open", server.url("redirect-to-localhost"))
    assert result.returncode != 0
    assert "not in security.allowed_domains" in result.stderr


def test_popup_navigation_to_disallowed_host_is_blocked(server, env):
    opened = _run(env, "open", server.url("navigation_policy.html"))
    assert opened.returncode == 0, opened.stderr
    _run(env, "click", "#popup")
    tabs = _run(env, "tabs")
    assert "http://localhost" not in tabs.stdout
