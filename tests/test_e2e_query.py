"""E2E tests for the query verb (ROADMAP R2)."""

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
    home = tmp_path_factory.mktemp("ebrowse_home_query")
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


def test_query_table_filter_and_cols(server, env):
    r = ebrowse(env, "open", server.url("table.html"))
    assert r.returncode == 0, r.stderr
    r = ebrowse(env, "query", "s4", "--filter", "Pending", "--cols", "Description,Total")
    assert r.returncode == 0, r.stderr
    assert "matched 8 of 25 items" in r.stdout
    assert "Naples" in r.stdout and "(@e" in r.stdout
    assert "Status" not in r.stdout.splitlines()[1]  # column projection applied


def test_query_bad_col_lists_columns(server, env):
    r = ebrowse(env, "query", "s4", "--cols", "Nonexistent")
    assert r.returncode == 2
    assert "description" in r.stderr.lower()


def test_query_list_regex_on_plain_text(server, env):
    ebrowse(env, "open", server.url("list.html"))
    r = ebrowse(env, "query", "s4", "--filter", "^Gaggia", "--limit", "2")
    assert r.returncode == 0, r.stderr
    assert "matched 4 of 32 items" in r.stdout
    assert "--cursor 2" in r.stdout  # pagination hint


def test_query_non_list_section_is_actionable(server, env):
    r = ebrowse(env, "query", "s1")
    assert r.returncode == 2
    assert "list/table" in r.stderr
