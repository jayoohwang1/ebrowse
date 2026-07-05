"""E2E tests for compound verbs (ROADMAP R1): fill-form, select machine, search."""

from __future__ import annotations

import json
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
    home = tmp_path_factory.mktemp("ebrowse_home_compound")
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
    out = ebrowse(env, "expand", sid, "--all").stdout
    m = re.search(pattern + r"[^)\]]*\((@e\d+)", out)
    assert m, f"no element matching {pattern!r} in {sid}:\n{out}"
    return m.group(1)


def test_fill_form_whole_signup(server, env):
    r = ebrowse(env, "open", server.url("form.html"))
    assert r.returncode == 0, r.stderr
    data = {
        "Full name": "Jayoo Hwang",
        "Email": "jay@example.com",
        "Password": "hunter2hunter2",
        "Country": "South Korea",
        "Account type": "Business",
        "I agree": True,
        "Bogus field": "x",
    }
    r = ebrowse(env, "fill-form", "s2", "--data", json.dumps(data))
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("✓") == 6
    assert "✗ Bogus field" in r.stdout and "have:" in r.stdout
    assert "(native select)" in r.stdout and "(radio)" in r.stdout
    assert 'value: "Choose a country" → "South Korea"' in r.stdout
    # submit works after compound fill
    submit = ref_for(env, "s2", r"\[Create account")
    r = ebrowse(env, "click", submit)
    assert "Account created!" in r.stdout


def test_fill_form_bad_json_and_no_match(server, env):
    r = ebrowse(env, "fill-form", "s2", "--data", "{not json")
    assert r.returncode == 2 and "not valid JSON" in r.stderr
    r = ebrowse(env, "fill-form", "s2", "--data", '{"Nonexistent": "x"}')
    assert r.returncode == 2 and "Available:" in r.stderr


def test_select_machine_custom_dropdown(server, env):
    ebrowse(env, "open", server.url("dropdown.html"))
    btn = ref_for(env, "s2", r"Sort by: Relevance")
    r = ebrowse(env, "select", btn, "Average rating")
    assert r.returncode == 0, r.stderr
    assert "✓ opened" in r.stdout and "✓ picked" in r.stdout
    assert "→ partial change" in r.stdout.splitlines()[0]


def test_select_machine_no_match_lists_options(server, env):
    ebrowse(env, "reload")
    btn = ref_for(env, "s2", r"Sort by: Relevance")
    r = ebrowse(env, "select", btn, "Zebra order")
    assert r.returncode == 2
    assert "Price: low to high" in r.stderr  # options listed


def test_select_native_still_works(server, env):
    sel = ref_for(env, "s2", r"Results per page")
    r = ebrowse(env, "select", sel, "100")
    assert r.returncode == 0, r.stderr
    assert 'value: "25" → "100"' in r.stdout


def test_search_finds_box_and_submits(server, env):
    ebrowse(env, "open", server.url("list.html"))
    r = ebrowse(env, "search", "espresso", "--no-submit")
    assert r.returncode == 0, r.stderr
    assert "✓ typed into" in r.stdout
    assert re.search(r'value: "" → "espresso"', r.stdout)


def test_search_no_box_is_actionable(server, env):
    ebrowse(env, "open", server.url("dialogs.html"))
    r = ebrowse(env, "search", "anything")
    assert r.returncode == 2
    assert "--in" in r.stderr
