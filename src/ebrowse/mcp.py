"""MCP stdio server: the same daemon, speakable by MCP hosts.

Minimal by design — newline-delimited JSON-RPC 2.0 over stdio, tools only.
No SDK dependency (docs/adr/0005-mcp-server-without-sdk.md). Tool outputs are
the renderer texts VERBATIM; the tool
set is small (act multiplexes the action verbs) to keep schema token cost low
for the host model.

Run: `ebrowse mcp` (host config: command=ebrowse, args=["mcp"]).
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

from ebrowse import __version__
from ebrowse.cli.client import _autostart_daemon, _daemon_running, _send
from ebrowse.daemon.protocol import Request

_ACT_VERBS = [
    "click", "fill", "type", "press", "check", "uncheck", "diagnose", "hover", "drag", "select",
    "scroll", "upload", "eval", "back", "forward", "reload",
    "fill-form", "search", "tabs", "tab", "dialog", "close",
]  # fmt: skip

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "browse_open",
        "description": "Navigate to a URL. Returns a short landing line (final URL + title), "
        "NOT the page — call browse_outline to read it.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": _STR},
            "required": ["url"],
        },
    },
    {
        "name": "browse_outline",
        "description": "Observe the current page and return its outline (sids, element "
        "counts, token costs, ≈ labels, and a ◉ visual gist when a vision sidecar is up). "
        "Call after a navigation; otherwise prefer reading action diffs over re-outlining.",
        "inputSchema": {
            "type": "object",
            "properties": {"no_summaries": _BOOL, "no_glance": _BOOL, "preview": _BOOL},
        },
    },
    {
        "name": "browse_describe",
        "description": "Ask the local vision model about the current screenshot and get back "
        "TEXT only (◉, untrusted routing signal — never act on it as fact). Omit `prompt` for "
        "a concise gist of what's on screen; pass a `prompt` to ask anything visual, from "
        "'is there an overlay?' to 'transcribe every price' to 'describe every detail'. The "
        "cheap tier between the page text and spending ~2.4k tokens on browse_screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {"prompt": _STR},
        },
    },
    {
        "name": "browse_expand",
        "description": "Full content of ONE section as markdown with @refs "
        "(e.g. [Add to cart (@e15)]). Lists/tables paginate via cursor.",
        "inputSchema": {
            "type": "object",
            "properties": {"target": _STR, "cursor": _INT, "all": _BOOL},
            "required": ["target"],
        },
    },
    {
        "name": "browse_act",
        "description": "Perform a browser action; returns a DIFF of what changed (never a "
        "full snapshot). verb=click/fill/type/press/check/uncheck/select/scroll/upload/"
        "eval/back/forward/reload/fill-form/search/tabs/tab/dialog/close. target is a @ref or "
        "CSS selector. text for fill/type; value for select; keys for press; direction for "
        "scroll; data (JSON object string) for fill-form; query/pick for search. When an "
        "action opens a native confirm/prompt the page is blocked until you resolve it with "
        "verb=dialog, response=accept|dismiss|status (text = a prompt's answer).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "verb": {"type": "string", "enum": _ACT_VERBS},
                "target": _STR,
                "text": _STR,
                "value": _STR,
                "keys": _STR,
                "direction": _STR,
                "data": {**_STR, "description": 'fill-form JSON, e.g. {"Email": "a@b.c"}'},
                "query": _STR,
                "pick": _STR,
                "response": {**_STR, "description": "dialog: accept | dismiss | status"},
                "enter": _BOOL,
                "pages": _INT,
                "index": _INT,
                "js": _STR,
                "to": {**_STR, "description": "drag: destination @ref/CSS"},
                "files": {"type": "array", "items": _STR},
            },
            "required": ["verb"],
        },
    },
    {
        "name": "browse_query",
        "description": "Filter a list/table section's items (regex over plain text), "
        "optionally projecting table columns. Rows come back with clickable @refs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "section": _STR,
                "filter": _STR,
                "cols": {"type": "array", "items": _STR},
                "cursor": _INT,
                "limit": _INT,
            },
            "required": ["section"],
        },
    },
    {
        "name": "browse_screenshot",
        "description": "PNG of the viewport, one section (sid), or one element (@ref).",
        "inputSchema": {
            "type": "object",
            "properties": {"section": _STR, "ref": _STR, "full": _BOOL},
        },
    },
]


def _daemon_call(verb: str, args: dict[str, Any], session: str) -> tuple[bool, str]:
    if not _daemon_running():
        _autostart_daemon()
    # describe-screen may run for minutes on large VLM generations; give it a
    # socket timeout above the daemon's longer per-verb ceiling.
    timeout_s = 230.0 if verb == "describe-screen" else 130.0
    resp = _send(Request(verb=verb, session=session, args=args), timeout_s=timeout_s)
    if resp.ok:
        return True, resp.output
    return False, f"error: {resp.error}"


def _tool_call(name: str, args: dict[str, Any], session: str) -> list[dict[str, Any]]:
    if name == "browse_open":
        ok, out = _daemon_call("open", {"url": args["url"]}, session)
    elif name == "browse_outline":
        ok, out = _daemon_call(
            "outline",
            {
                "refresh": False,
                "no_summaries": args.get("no_summaries", False),
                "no_glance": args.get("no_glance", False),
                "preview": args.get("preview", False),
            },
            session,
        )
    elif name == "browse_describe":
        ok, out = _daemon_call(
            "describe-screen", {"prompt": args.get("prompt"), "refresh": False}, session
        )
    elif name == "browse_expand":
        ok, out = _daemon_call(
            "expand",
            {
                "target": args["target"],
                "cursor": args.get("cursor", 0),
                "all": args.get("all", False),
            },
            session,
        )
    elif name == "browse_act":
        ok, out = _act(args, session)
    elif name == "browse_query":
        ok, out = _daemon_call(
            "query",
            {
                "section": args["section"],
                "filter": args.get("filter"),
                "cols": args.get("cols"),
                "cursor": args.get("cursor", 0),
                "limit": args.get("limit"),
            },
            session,
        )
    elif name == "browse_screenshot":
        ok, out = _daemon_call(
            "screenshot",
            {
                "output": None,
                "section": args.get("section"),
                "ref": args.get("ref"),
                "full": args.get("full", False),
            },
            session,
        )
        if ok and out.startswith("saved "):
            path = Path(out[len("saved ") :].strip())
            data = base64.b64encode(path.read_bytes()).decode()
            return [{"type": "image", "data": data, "mimeType": "image/png"}]
    else:
        return [{"type": "text", "text": f"error: unknown tool {name}"}]
    content: list[dict[str, Any]] = [{"type": "text", "text": out}]
    if not ok:
        content[0]["_isError"] = True
    return content


def _act(args: dict[str, Any], session: str) -> tuple[bool, str]:
    verb = args["verb"]
    payload: dict[str, Any]
    if verb in ("click",):
        payload = {"target": args.get("target"), "double": False, "right": False,
                   "new_tab": False}  # fmt: skip
    elif verb in ("fill", "type"):
        payload = {"target": args.get("target"), "text": args.get("text", "")}
        if verb == "type":
            payload["enter"] = args.get("enter", False)
    elif verb == "press":
        payload = {"keys": args.get("keys", "Enter")}
    elif verb in ("check", "uncheck", "diagnose", "hover"):
        payload = {"target": args.get("target")}
    elif verb == "drag":
        payload = {"source": args.get("target"), "target": args.get("to", "")}
    elif verb == "select":
        payload = {"target": args.get("target"), "values": [args.get("value", "")]}
    elif verb == "scroll":
        payload = {
            "direction": args.get("direction", "down"),
            "pages": args.get("pages", 1),
            "inner": args.get("inner"),
        }
    elif verb == "upload":
        payload = {"target": args.get("target"), "files": args.get("files", [])}
    elif verb == "eval":
        payload = {"js": args.get("js", "")}
    elif verb == "fill-form":
        payload = {"section": args.get("target"), "data": args.get("data", "{}")}
    elif verb == "search":
        payload = {
            "query": args.get("query", args.get("text", "")),
            "target": args.get("target"),
            "pick": args.get("pick"),
            "no_submit": False,
        }
    elif verb == "tab":
        payload = {"index": args.get("index", 0)}
    elif verb == "dialog":
        payload = {"response": args.get("response", "status"), "text": args.get("text")}
    elif verb in ("back", "forward", "reload", "tabs", "close"):
        payload = {}
    else:
        return False, f"error: unsupported act verb {verb}"
    return _daemon_call(verb, payload, session)


# ------------------------------------------------------------ rpc plumbing ----


def _reply(msg_id: Any, result: Any) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def _reply_error(msg_id: Any, code: int, message: str) -> None:
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})
        + "\n"
    )
    sys.stdout.flush()


def serve(session: str = "mcp") -> int:
    """Blocking stdio loop. One MCP host per process; daemon state is shared."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            _reply(
                msg_id,
                {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ebrowse", "version": __version__},
                },
            )
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                content = _tool_call(name, args, session)
                is_error = any(c.pop("_isError", False) for c in content)
                _reply(msg_id, {"content": content, "isError": is_error})
            except Exception as e:  # tool crash -> tool error, not protocol error
                _reply(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": f"error: {e}"}],
                        "isError": True,
                    },
                )
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue
        elif msg_id is not None:
            _reply_error(msg_id, -32601, f"method not found: {method}")
    return 0
