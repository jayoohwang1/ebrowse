"""CLI-side dispatch: autostart daemon, send one request, print the response."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

from ebrowse.config import cache_dir, socket_path
from ebrowse.daemon.protocol import ExitCode, Request, Response

_AUTOSTART_WAIT_S = 12.0


def _send(req: Request, timeout_s: float) -> Response:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    sock.connect(str(socket_path()))
    with sock, sock.makefile("rwb") as f:
        f.write(req.encode())
        f.flush()
        line = f.readline()
    if not line:
        raise ConnectionError("daemon closed the connection without replying")
    return Response.decode(line)


def _daemon_running() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(str(socket_path()))
        s.close()
        return True
    except OSError:
        return False


def _autostart_daemon() -> None:
    log = open(cache_dir() / "daemon.log", "a")  # noqa: SIM115 — handed to Popen
    subprocess.Popen(
        [sys.executable, "-m", "ebrowse.daemon"],
        stdout=log,
        stderr=log,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + _AUTOSTART_WAIT_S
    while time.monotonic() < deadline:
        if _daemon_running():
            return
        time.sleep(0.15)
    print(
        f"error: daemon did not start within {_AUTOSTART_WAIT_S}s — "
        f"check {cache_dir() / 'daemon.log'}",
        file=sys.stderr,
    )
    raise SystemExit(ExitCode.INTERNAL)


def _build_request(args: argparse.Namespace) -> Request | None:
    """Map argparse output to a wire request. Returns None for local verbs."""
    verb = args.verb
    a: dict = {}
    if verb in ("open", "goto"):
        a = {"url": args.url}
    elif verb == "outline":
        a = {
            "refresh": args.refresh,
            "no_summaries": args.no_summaries,
            "no_glance": args.no_glance,
            "preview": args.preview,
        }
    elif verb == "describe-screen":
        a = {"prompt": args.prompt, "refresh": args.refresh}
    elif verb == "expand":
        a = {"target": args.target, "cursor": args.cursor, "all": args.all, "ax": args.ax}
    elif verb == "screenshot":
        a = {"output": args.output, "section": args.section, "ref": args.ref, "full": args.full}
    elif verb == "get":
        a = {"what": args.what, "target": args.target, "attr": args.attr}
    elif verb == "tab":
        a = {"index": args.index}
    elif verb == "dialog":
        a = {"response": args.response, "text": args.text}
    elif verb == "connect":
        a = {"target": args.target}
    elif verb == "close":
        if args.all:
            return Request(verb="close_all", session=args.session)
        a = {}
    elif verb == "daemon":
        if args.action == "status":
            return Request(verb="daemon_status", session=args.session)
        return Request(verb="daemon_stop", session=args.session)
    elif verb in ("back", "forward", "reload", "tabs"):
        a = {}
    elif verb == "click":
        a = {
            "target": args.target,
            "double": args.double,
            "right": args.right,
            "new_tab": args.new_tab,
        }
    elif verb == "fill":
        a = {"target": args.target, "text": args.text}
    elif verb == "type":
        a = {"target": args.target, "text": args.text, "enter": args.enter}
    elif verb == "press":
        a = {"keys": args.keys}
    elif verb in ("check", "uncheck", "diagnose"):
        a = {"target": args.target}
    elif verb == "select":
        a = {"target": args.target, "values": args.value}
    elif verb == "hover":
        a = {"target": args.target}
    elif verb == "drag":
        a = {"source": args.source, "target": args.to}
    elif verb == "scroll":
        a = {"direction": args.direction, "pages": args.pages, "inner": args.inner}
    elif verb == "upload":
        a = {"target": args.target, "files": [str(Path(f).resolve()) for f in args.files]}
    elif verb == "eval":
        a = {"js": args.js}
    elif verb == "query":
        a = {
            "section": args.section,
            "filter": args.filter,
            "cols": [c.strip() for c in args.cols.split(",")] if args.cols else None,
            "cursor": args.cursor,
            "limit": args.limit,
        }
    elif verb == "fill-form":
        a = {"section": args.section, "data": args.data}
    elif verb == "search":
        a = {
            "query": args.query,
            "target": args.target,
            "pick": args.pick,
            "no_submit": args.no_submit,
        }
    else:
        return None
    return Request(verb=verb, session=args.session, args=a)


def run_command(args: argparse.Namespace) -> int:
    if args.verb == "doctor":
        from ebrowse.cli.doctor import run_doctor

        return run_doctor()
    if args.verb == "mcp":
        from ebrowse.mcp import serve

        return serve(session=args.mcp_session)

    req = _build_request(args)
    if req is None:
        print(f"error: unhandled verb '{args.verb}'", file=sys.stderr)
        return ExitCode.USAGE

    if not _daemonless_ok(req.verb) and not _daemon_running():
        _autostart_daemon()
    elif _daemonless_ok(req.verb) and not _daemon_running():
        print("daemon: not running")
        return 0

    # describe-screen may legitimately run for minutes (large VLM generations);
    # its socket timeout must exceed the daemon's longer per-verb ceiling.
    default_timeout = 230.0 if req.verb == "describe-screen" else 130.0
    timeout_s = (args.timeout / 1000) if args.timeout else default_timeout
    try:
        resp = _send(req, timeout_s)
    except TimeoutError:
        print(f"error: no reply within {timeout_s:.0f}s — daemon busy or hung", file=sys.stderr)
        return ExitCode.INTERNAL
    except OSError as e:
        print(f"error: cannot reach daemon: {e}", file=sys.stderr)
        return ExitCode.INTERNAL

    if args.json:
        print(json.dumps({"ok": resp.ok, "output": resp.output, "error": resp.error}))
        return resp.exit_code if not resp.ok else 0
    if resp.ok:
        if resp.output:
            print(resp.output)
        return 0
    print(f"error: {resp.error}", file=sys.stderr)
    return resp.exit_code or ExitCode.ACTION_FAILED


def _daemonless_ok(verb: str) -> bool:
    """Verbs that should not spawn a daemon just to answer."""
    return verb in ("daemon_status", "daemon_stop", "close_all")
