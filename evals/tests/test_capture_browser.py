"""Integration: real daemon + chromium + fixture site -> StepCapture round-trip."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ebrowse_evals.capture import DaemonCaptureClient, StepCapture
from ebrowse_evals.trace.records import BrowserEvent
from ebrowse_evals.trace.store import TraceReader, TraceWriter

# the repo-root tests package (fixture site + server) is not importable when
# pytest roots at evals/tests alone, so add the repo root explicitly
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.fixture_server import FixtureServer  # noqa: E402

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def server():
    with FixtureServer() as srv:
        yield srv


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    home = tmp_path_factory.mktemp("ebrowse_home_capture")
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


def test_capture_round_trip(env, server, tmp_path):
    r = ebrowse(env, "open", server.url("article.html"))
    assert r.returncode == 0, r.stderr

    writer = TraceWriter(tmp_path / "run")
    client = DaemonCaptureClient(socket_path=Path(env["XDG_RUNTIME_DIR"]) / "ebrowse.sock")
    cap = StepCapture(writer, client)

    fields = cap.capture(1)
    assert fields["browser"]["url"] == server.url("article.html")
    assert fields["browser"]["tabs"][0]["active"] is True
    assert fields["browser"]["viewport"]["width"] > 0
    assert "scroll_y" in fields["browser"]

    (tmp_path / "run" / "events.jsonl").touch()  # reader wants the file present
    reader = TraceReader(tmp_path / "run")
    png = reader.blobs.get(fields["screenshot"])
    assert png.startswith(b"\x89PNG")
    snap = json.loads(reader.blobs.get(fields["dom_snapshot"]))
    assert snap["url"] == server.url("article.html")
    assert snap["root"]["c"]  # a real DOM tree, not an empty shell

    # no page mutation between captures -> daemon reuses its snapshot and the
    # blob store dedupes both payload blobs
    fields2 = cap.capture(2)
    assert fields2["dom_snapshot"] == fields["dom_snapshot"]

    # navigate: capture must reflect the new page and surface navigation events
    r = ebrowse(env, "open", server.url("form.html"))
    assert r.returncode == 0, r.stderr
    fields3 = cap.capture(3)
    assert fields3["browser"]["url"] == server.url("form.html")
    assert fields3["dom_snapshot"] != fields["dom_snapshot"]
    events = [r for r in reader.records() if isinstance(r, BrowserEvent)]
    assert any(e.kind == "navigation" and e.step == 3 for e in events)


def test_capture_survives_no_page(env, tmp_path):
    # a fresh session with no browser: capture degrades, never raises
    writer = TraceWriter(tmp_path / "run2")
    client = DaemonCaptureClient(
        socket_path=Path(env["XDG_RUNTIME_DIR"]) / "ebrowse.sock", session="empty-session"
    )
    fields = StepCapture(writer, client).capture(1)
    assert fields["screenshot"] is None and fields["dom_snapshot"] is None
