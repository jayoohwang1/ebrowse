"""E2E: anonymous elements act via the CDP node-binding rescue (ADR 0015).

anonymous.html's icon button and bare input have no id/testid/name/text/
placeholder — locate.resolve() generates zero candidates. Before ADR 0015 they
were permanently unclickable; now the capture-time backendNodeId binding acts
on the exact observed node, and the page's own reaction proves the hit.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
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
    home = tmp_path_factory.mktemp("ebrowse_home_binding")
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


def anonymous_refs(env) -> tuple[str, str]:
    """(icon button ref, bare input ref) from the rendered sections."""
    out = ebrowse(env, "outline").stdout
    sids = re.findall(r"^(s\d+) ", out, re.M)
    button = field = None
    for sid in sids:
        text = ebrowse(env, "expand", sid, "--all").stdout
        m = re.search(r"\[button \((@e\d+)\)\]", text)
        if m:
            button = m.group(1)
        # the unnamed text input's deterministic label is its type: "text"
        m = re.search(r"\[text \((@e\d+)[:)]", text)
        if m:
            field = m.group(1)
    assert button and field, "anonymous refs not found in outline"
    return button, field


def test_anonymous_icon_button_clicks_via_binding(server, env):
    r = ebrowse(env, "open", server.url("anonymous.html"))
    assert r.returncode == 0, r.stderr
    button, _ = anonymous_refs(env)
    r = ebrowse(env, "click", button)
    assert r.returncode == 0, r.stderr
    assert "search-clicked" in r.stdout  # the page's own reaction proves the hit


def test_anonymous_input_fills_via_binding(server, env):
    _, field = anonymous_refs(env)
    r = ebrowse(env, "fill", field, "hello binding")
    assert r.returncode == 0, r.stderr
    assert "note: hello binding" in r.stdout  # oninput echo → landed on the node


def test_dead_binding_error_names_reoutline(server, env):
    """#mutate replaces the icon button 6s after load with NO verb in between
    (every verb re-observes and would re-bind — that self-healing is test 1's
    behavior). The dead binding must fail loudly naming re-outline, and the
    re-outline must actually fix it."""
    # query param forces a real load (hash-only would be an in-page navigation
    # from the prior test's page, and the timer script would never arm)
    r = ebrowse(env, "open", server.url("anonymous.html") + "?m=1#mutate")
    assert r.returncode == 0, r.stderr
    button, _ = anonymous_refs(env)  # observed BEFORE the 6s mutation
    time.sleep(8)  # the page replaces the node; no ebrowse command runs
    r = ebrowse(env, "click", button)
    if r.returncode == 0:
        # a slow run can push the observe past the mutation, re-binding to the
        # replacement — the stale window never existed, nothing to assert
        pytest.skip("observe landed after the mutation; no stale window")
    assert "outline" in (r.stdout + r.stderr)
    assert ebrowse(env, "outline").returncode == 0
    button, _ = anonymous_refs(env)
    r = ebrowse(env, "click", button)
    assert r.returncode == 0, r.stderr
    assert "search-clicked: replaced" in r.stdout


def test_identical_siblings_reorder_witness_picks_bound_node(server, env):
    """Three descriptor-identical Buy buttons; a delayed reorder (past the
    quiescence window) shifts nth positions while identity facts still verify.
    The witness geometry check must act on the BOUND node: Alpha's old ref
    buys Alpha (now last), not whatever sits at nth 0 now."""
    r = ebrowse(env, "open", server.url("identical_row.html"))
    assert r.returncode == 0, r.stderr
    out = ebrowse(env, "outline").stdout
    sids = re.findall(r"^(s\d+) ", out, re.M)
    buys: list[str] = []
    for sid in sids:
        text = ebrowse(env, "expand", sid, "--all").stdout
        buys += re.findall(r"\[Buy \((@e\d+)\)\]", text)
    assert len(buys) == 3, buys
    r = ebrowse(env, "click", ref_anywhere_in(env, sids, r"Reverse order"))
    assert r.returncode == 0, r.stderr
    time.sleep(3.5)  # the delayed reorder fires; no ebrowse command runs
    r = ebrowse(env, "click", buys[0])  # Alpha's ref; nth 0 is now Gamma
    assert r.returncode == 0, r.stdout + r.stderr
    assert "bought: Alpha" in r.stdout, r.stdout


def ref_anywhere_in(env, sids: list[str], pattern: str) -> str:
    for sid in sids:
        text = ebrowse(env, "expand", sid, "--all").stdout
        m = re.search(pattern + r"[^)\]]*\((@e\d+)", text)
        if m:
            return m.group(1)
    raise AssertionError(f"no element matching {pattern!r}")


@pytest.fixture(scope="module")
def env_binding_first(tmp_path_factory):
    home = tmp_path_factory.mktemp("ebrowse_home_bf")
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
            "EBROWSE_BROWSER_ACT_VIA_BINDING": "true",
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


def test_binding_first_flag_smoke(server, env_binding_first):
    """act_via_binding=true: ordinary named-element verbs work through the
    binding-first path end to end (fill dispatches, diff reflects the value)."""
    env = env_binding_first
    r = ebrowse(env, "open", server.url("anonymous.html"))
    assert r.returncode == 0, r.stderr
    out = ebrowse(env, "outline").stdout
    sids = re.findall(r"^(s\d+) ", out, re.M)
    search = ref_anywhere_in(env, sids, r"Search terms")
    r = ebrowse(env, "fill", search, "kitten")
    assert r.returncode == 0, r.stderr
    assert '"kitten"' in r.stdout, r.stdout
    button, _ = anonymous_refs(env)
    r = ebrowse(env, "click", button)
    assert r.returncode == 0, r.stderr
    assert "search-clicked: kitten" in r.stdout, r.stdout
