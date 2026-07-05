"""E2E test for the MCP stdio server (ROADMAP R3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixture_server import FixtureServer

pytestmark = pytest.mark.e2e


def _rpc(method: str, msg_id: int | None = None, **params) -> str:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params:
        msg["params"] = params
    return json.dumps(msg)


def test_mcp_stdio_flow(tmp_path):
    home = tmp_path / "home"
    (home / ".run").mkdir(parents=True)
    real_browsers = Path(os.environ.get("HOME", "~")).expanduser() / ".cache" / "ms-playwright"
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
            "EBROWSE_SUMMARIZER_ENABLED": "false",
        }
    )
    with FixtureServer() as srv:
        lines = "\n".join(
            [
                _rpc("initialize", 1, protocolVersion="2024-11-05"),
                _rpc("notifications/initialized"),
                _rpc("tools/list", 2),
                _rpc(
                    "tools/call",
                    3,
                    name="browse_open",
                    arguments={"url": srv.url("dropdown.html")},
                ),  # fmt: skip
                _rpc(
                    "tools/call",
                    4,
                    name="browse_act",
                    arguments={"verb": "select", "target": "#perpage", "value": "100"},
                ),  # fmt: skip
                _rpc("tools/call", 5, name="browse_screenshot", arguments={}),
                _rpc("tools/call", 6, name="browse_expand", arguments={"target": "s99"}),
            ]
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "ebrowse.cli.main", "mcp"],
                input=lines + "\n",
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        finally:
            subprocess.run(
                [sys.executable, "-m", "ebrowse.cli.main", "daemon", "stop"],
                env=env,
                capture_output=True,
                timeout=15,
            )

    replies = {m["id"]: m for m in map(json.loads, proc.stdout.splitlines()) if "id" in m}
    assert replies[1]["result"]["serverInfo"]["name"] == "ebrowse"
    tool_names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert {"browse_open", "browse_act", "browse_query", "browse_screenshot"} <= tool_names

    open_res = replies[3]["result"]
    assert not open_res["isError"]
    assert open_res["content"][0]["text"].startswith("PAGE Dropdowns")

    act_res = replies[4]["result"]
    assert not act_res["isError"], act_res
    assert 'value: "25" → "100"' in act_res["content"][0]["text"]

    shot = replies[5]["result"]["content"][0]
    assert shot["type"] == "image" and shot["mimeType"] == "image/png"
    assert len(shot["data"]) > 5000  # real base64 png

    err = replies[6]["result"]
    assert err["isError"] and "outline" in err["content"][0]["text"]
