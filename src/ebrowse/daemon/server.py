"""ebrowse daemon: asyncio unix-socket server owning Playwright sessions.

One process per user. Commands within a session are serialized by the session
lock; different sessions run concurrently. Idle shutdown after
daemon.idle_shutdown_minutes without commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from typing import Any

from loguru import logger

from ebrowse import __version__, debug
from ebrowse.config import Config, cache_dir, load_config, socket_path
from ebrowse.daemon.protocol import CommandError, ExitCode, Request, Response
from ebrowse.session import Session

_VERB_TIMEOUT_S = 120  # hard ceiling per command; goto has its own 45s budget
# describe-screen is a patient, agent-initiated visual query that can generate
# thousands of tokens on modest local hardware — a longer ceiling than the
# page-touching verbs (whose 120s cap guards against a hung page). Must exceed
# summarizer.describe_timeout_s; the CLI/MCP transport timeout exceeds this.
_LONG_VERB_TIMEOUT_S: dict[str, int] = {"describe-screen": 210}


def _verb_timeout(verb: str) -> int:
    return _LONG_VERB_TIMEOUT_S.get(verb, _VERB_TIMEOUT_S)


# Verbs allowed while a native dialog blocks the current tab: resolve it, or
# escape to another tab / re-attach / close. Everything else would touch the
# frozen page and is refused with a recovery hint.
_DIALOG_SAFE_VERBS = frozenset({"dialog", "tabs", "tab", "connect", "close"})


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sessions: dict[str, Session] = {}
        self.last_activity = time.monotonic()
        self.started_at = time.time()
        self._server: asyncio.Server | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- server ----

    async def run(self) -> None:
        sock = socket_path()
        if sock.exists():
            if await self._socket_alive(sock):
                logger.error(f"another daemon is already listening on {sock}")
                sys.exit(1)
            sock.unlink()  # stale socket from a crashed daemon
        self._server = await asyncio.start_unix_server(self._handle_conn, path=str(sock))
        pidfile = cache_dir() / "daemon.pid"
        pidfile.write_text(str(os.getpid()))
        logger.info(f"ebrowse daemon {__version__} listening on {sock} (pid {os.getpid()})")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)
        idle_task = asyncio.create_task(self._idle_watchdog())

        await self._stop.wait()
        logger.info("shutting down")
        idle_task.cancel()
        self._server.close()
        for session in self.sessions.values():
            await session.close()
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()
        with contextlib.suppress(FileNotFoundError):
            pidfile.unlink()

    @staticmethod
    async def _socket_alive(sock) -> bool:
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock))
            writer.close()
            await writer.wait_closed()
            del reader
            return True
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            return False

    async def _idle_watchdog(self) -> None:
        limit = self.cfg.daemon.idle_shutdown_minutes * 60
        while True:
            await asyncio.sleep(60)
            if time.monotonic() - self.last_activity > limit:
                logger.info(f"idle for {limit}s — shutting down")
                self._stop.set()
                return

    # ----------------------------------------------------------- handling ----

    async def _handle_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.last_activity = time.monotonic()
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = Request.decode(line)
            except Exception as e:
                writer.write(
                    Response(
                        id="", ok=False, error=f"bad request: {e}", exit_code=ExitCode.INTERNAL
                    ).encode()
                )
                return
            resp = await self._dispatch(req)
            writer.write(resp.encode())
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            self.last_activity = time.monotonic()

    async def _dispatch(self, req: Request) -> Response:
        # Tier-1 debug channel (docs/architecture.md "Debug event channel"):
        # OFF by default. When debug.log is configured, collect this request's
        # events in-memory (contextvar recorder) and flush them as JSONL after
        # the response is built — event shape {request_id, module, event,
        # level, fields, ts, mono}, joined to the CLI call by request_id.
        if not self.cfg.debug.log:
            return await self._dispatch_inner(req)
        with debug.recording(req.id) as rec:
            t0 = time.monotonic()
            debug.emit("daemon", "request_begin", verb=req.verb, session=req.session)
            resp = await self._dispatch_inner(req)
            debug.emit(
                "daemon",
                "request_end",
                verb=req.verb,
                session=req.session,
                ok=resp.ok,
                exit_code=resp.exit_code,
                dur_ms=round((time.monotonic() - t0) * 1000, 1),
            )
        debug.write_jsonl(debug.resolve_log_path(self.cfg.debug.log, req.session), rec.events)
        return resp

    async def _dispatch_inner(self, req: Request) -> Response:
        logger.info(f"[{req.session}] {req.verb} {req.args}")
        # daemon-level verbs (no session)
        if req.verb == "daemon_status":
            return Response(id=req.id, ok=True, output=self._status_text())
        if req.verb == "daemon_stop":
            self._stop.set()
            return Response(id=req.id, ok=True, output="daemon stopping")
        if req.verb == "close_all":
            for s in list(self.sessions.values()):
                await s.close()
            self.sessions.clear()
            return Response(id=req.id, ok=True, output="all sessions closed")

        session = self.sessions.get(req.session)
        if session is None:
            session = Session(req.session, load_config())
            self.sessions[req.session] = session

        verb_timeout = _verb_timeout(req.verb)
        try:
            async with session.lock:
                output = await asyncio.wait_for(
                    self._run_verb(session, req.verb, req.args), timeout=verb_timeout
                )
            return Response(id=req.id, ok=True, output=output)
        except CommandError as e:
            return Response(id=req.id, ok=False, error=str(e), exit_code=e.exit_code)
        except TimeoutError:
            return Response(
                id=req.id,
                ok=False,
                error=f"'{req.verb}' timed out after {verb_timeout}s — the page may be "
                "stuck loading; try 'ebrowse reload' or 'ebrowse close'",
                exit_code=ExitCode.ACTION_FAILED,
            )
        except Exception as e:
            logger.exception(f"[{req.session}] {req.verb} crashed")
            return Response(
                id=req.id,
                ok=False,
                error=f"internal error in '{req.verb}': {e} — see daemon.log",
                exit_code=ExitCode.INTERNAL,
            )

    async def _run_verb(self, session: Session, verb: str, args: dict[str, Any]) -> str:
        # A native confirm/prompt blocks the renderer, so page-touching verbs
        # would hang; refuse them fast with the recovery action. The escape-hatch
        # verbs below (resolve the dialog, switch/close tabs, re-attach) stay open.
        if (
            verb not in _DIALOG_SAFE_VERBS
            and (warn := session.dialog_block_warning(verb)) is not None
        ):
            raise CommandError(warn, ExitCode.ACTION_FAILED)
        if verb in ("open", "goto"):
            return await session.verb_open(args["url"])
        if verb == "reload":
            return await session.verb_reload()
        if verb == "back":
            return await session.verb_back()
        if verb == "forward":
            return await session.verb_forward()
        if verb == "outline":
            return await session.verb_outline(
                refresh=args.get("refresh", False),
                no_summaries=args.get("no_summaries", False),
                no_glance=args.get("no_glance", False),
                preview=args.get("preview", False),
            )
        if verb == "describe-screen":
            return await session.verb_describe(
                prompt=args.get("prompt"), refresh=args.get("refresh", False)
            )
        if verb == "expand":
            return await session.verb_expand(
                args["target"],
                cursor=args.get("cursor", 0),
                show_all=args.get("all", False),
                ax=args.get("ax", False),
            )
        if verb == "screenshot":
            return await session.verb_screenshot(
                output=args.get("output"),
                section=args.get("section"),
                ref=args.get("ref"),
                full=args.get("full", False),
            )
        if verb == "get":
            return await session.verb_get(args["what"], args.get("target"), args.get("attr"))
        if verb == "tabs":
            return await session.verb_tabs()
        if verb == "tab":
            return await session.verb_tab(args["index"])
        if verb == "dialog":
            return await session.verb_dialog(args["response"], text=args.get("text"))
        if verb == "connect":
            return await session.verb_connect(args["target"])
        if verb == "click":
            return await session.verb_click(
                args["target"],
                double=args.get("double", False),
                right=args.get("right", False),
                new_tab=args.get("new_tab", False),
            )
        if verb == "fill":
            return await session.verb_fill(args["target"], args["text"])
        if verb == "type":
            return await session.verb_type(
                args["target"], args["text"], enter=args.get("enter", False)
            )
        if verb == "press":
            return await session.verb_press(args["keys"])
        if verb == "check":
            return await session.verb_set_checked(args["target"], True)
        if verb == "uncheck":
            return await session.verb_set_checked(args["target"], False)
        if verb == "diagnose":
            return await session.verb_diagnose(args["target"])
        if verb == "select":
            values = args.get("values") or [args.get("value", "")]
            return await session.verb_select(args["target"], values)
        if verb == "hover":
            return await session.verb_hover(args["target"])
        if verb == "drag":
            return await session.verb_drag(args["source"], args["target"])
        if verb == "scroll":
            return await session.verb_scroll(
                args["direction"], pages=args.get("pages", 1), inner=args.get("inner")
            )
        if verb == "upload":
            return await session.verb_upload(args["target"], args["files"])
        if verb == "eval":
            return await session.verb_eval(args["js"])
        if verb == "query":
            return await session.verb_query(
                args["section"],
                filter_expr=args.get("filter"),
                cols=args.get("cols"),
                cursor=args.get("cursor", 0),
                limit=args.get("limit"),
            )
        if verb == "fill-form":
            return await session.verb_fill_form(args["section"], args["data"])
        if verb == "search":
            return await session.verb_search(
                args["query"],
                target=args.get("target"),
                pick=args.get("pick"),
                no_submit=args.get("no_submit", False),
            )
        if verb == "close":
            await session.close()
            self.sessions.pop(session.name, None)
            return f"session '{session.name}' closed"
        raise CommandError(f"unknown verb '{verb}' — this daemon is v{__version__}", ExitCode.USAGE)

    def _status_text(self) -> str:
        lines = [
            f"ebrowse daemon v{__version__} pid {os.getpid()} "
            f"up {int(time.time() - self.started_at)}s"
        ]
        for name, s in self.sessions.items():
            state = "browser running" if s._context else "no browser"
            page = s.page_mem.url if s.page_mem else "-"
            lines.append(f"  session {name}: {state}, page: {page}")
        if not self.sessions:
            lines.append("  no sessions")
        return "\n".join(lines)


def main() -> None:
    log_file = cache_dir() / "daemon.log"
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    logger.add(log_file, level="INFO", rotation="5 MB", retention=2)
    cfg = load_config()
    asyncio.run(Daemon(cfg).run())


if __name__ == "__main__":
    main()
