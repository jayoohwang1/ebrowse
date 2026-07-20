"""Policy boundary for Pi's browser-only ``ebrowse`` tool.

The Pi extension passes one command string to this module. This process tokenizes
it without expansion, validates the argv, launches one fixed executable without a shell, and
performs the synchronous post-call capture used by trace ingestion.  It emits
one JSON envelope for the TypeScript extension; stdout is never the ebrowse
protocol itself.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

POLICY_FILE_ENV = "EBROWSE_EVAL_TOOL_POLICY"

DEFAULT_ALLOWED_VERBS = (
    "open",
    "back",
    "forward",
    "reload",
    "tabs",
    "tab",
    "dialog",
    "outline",
    "describe-screen",
    "expand",
    "screenshot",
    "get",
    "click",
    "fill",
    "type",
    "press",
    "hover",
    "drag",
    "check",
    "uncheck",
    "diagnose",
    "select",
    "scroll",
    "fill-form",
    "query",
    "search",
)


@dataclass(slots=True)
class ToolPolicy:
    executable: str
    argv_prefix: list[str]
    run_dir: Path
    allowed_verbs: frozenset[str]
    allowed_domains: tuple[str, ...]
    timeout_s: float
    max_args_bytes: int
    max_output_bytes: int
    capture: bool

    def __post_init__(self) -> None:
        if not self.allowed_verbs:
            raise ValueError("allowed_verbs must not be empty")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.max_args_bytes <= 0 or self.max_output_bytes <= 0:
            raise ValueError("argument and output byte limits must be positive")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ToolPolicy:
        return cls(
            executable=str(value["executable"]),
            argv_prefix=[str(v) for v in value.get("argv_prefix", [])],
            run_dir=Path(value["run_dir"]).resolve(),
            allowed_verbs=frozenset(str(v) for v in value["allowed_verbs"]),
            allowed_domains=tuple(str(v).lower().rstrip(".") for v in value["allowed_domains"]),
            timeout_s=float(value["timeout_s"]),
            max_args_bytes=int(value["max_args_bytes"]),
            max_output_bytes=int(value["max_output_bytes"]),
            capture=bool(value.get("capture", True)),
        )


class PolicyBlock(ValueError):
    def __init__(self, verb: str | None, reason: str) -> None:
        super().__init__(reason)
        self.verb = verb
        self.reason = reason


def load_policy(path: Path) -> ToolPolicy:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("tool policy must be a JSON object")
    return ToolPolicy.from_dict(value)


def _domain_allowed(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _parse_args(args: list[str]) -> argparse.Namespace:
    from ebrowse.cli.main import build_parser

    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            return build_parser().parse_args(args)
    except SystemExit as exc:
        detail = stderr.getvalue().strip().splitlines()
        reason = detail[-1] if detail else f"invalid ebrowse arguments (exit {exc.code})"
        raise PolicyBlock(args[0] if args else None, reason) from None


def validate_args(args: list[str], policy: ToolPolicy) -> argparse.Namespace:
    if not args:
        raise PolicyBlock(None, "provide an ebrowse verb and its arguments")
    if any("\x00" in arg for arg in args):
        raise PolicyBlock(args[0], "NUL bytes are not allowed in tool arguments")
    total_bytes = sum(len(arg.encode("utf-8")) for arg in args)
    if total_bytes > policy.max_args_bytes:
        raise PolicyBlock(
            args[0],
            f"arguments use {total_bytes} bytes; limit is {policy.max_args_bytes}",
        )
    verb = args[0]
    if verb.startswith("-"):
        raise PolicyBlock(verb, "global ebrowse flags and session overrides are not allowed")
    if verb not in policy.allowed_verbs:
        raise PolicyBlock(verb, f"verb '{verb}' is not enabled by the browser-only policy")
    parsed = _parse_args(args)
    if parsed.verb != verb:
        raise PolicyBlock(verb, "verb aliases are not allowed by the browser-only policy")
    if verb == "screenshot" and parsed.output is not None:
        raise PolicyBlock(verb, "screenshot output paths are selected by the eval harness")
    if verb == "open":
        raw_url = str(parsed.url)
        url = raw_url if "://" in raw_url else f"https://{raw_url}"
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise PolicyBlock(verb, "only http:// and https:// navigation is allowed")
        if parts.username is not None or parts.password is not None:
            raise PolicyBlock(verb, "URLs containing credentials are not allowed")
        host = parts.hostname
        if not host:
            raise PolicyBlock(verb, "navigation URL has no hostname")
        if policy.allowed_domains and not _domain_allowed(host, policy.allowed_domains):
            raise PolicyBlock(verb, f"domain '{host}' is outside the task navigation policy")
    return parsed


def _allocate_call(run_dir: Path) -> int:
    spool = run_dir / "capture"
    spool.mkdir(parents=True, exist_ok=True)
    counter = spool / "seq"
    try:
        current = int(counter.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        current = 0
    value = current + 1
    counter.write_text(str(value), encoding="utf-8")
    return value


def _clip_output(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text, False
    marker = f"\n… output truncated by eval policy at {limit} bytes"
    room = max(0, limit - len(marker.encode()))
    clipped = raw[:room].decode("utf-8", errors="replace")
    return clipped + marker, True


def execute(args: list[str], policy: ToolPolicy) -> dict[str, Any]:
    try:
        validate_args(args, policy)
    except PolicyBlock as exc:
        return {
            "ok": False,
            "output": f"error: blocked by browser-only policy: {exc.reason}",
            "details": {
                "error_class": "policy_block",
                "verb": exc.verb,
                "reason": exc.reason,
            },
        }

    call = _allocate_call(policy.run_dir)
    env = dict(os.environ)
    env["EBROWSE_REQUEST_ID"] = f"call-{call}"
    command = [policy.executable, *policy.argv_prefix, *args]
    timed_out = False
    try:
        proc = subprocess.run(
            command,
            cwd=policy.run_dir / "workdir",
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=policy.timeout_s,
            check=False,
        )
        exit_code = proc.returncode
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -1
        stdout = (
            exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        )
        stderr = (
            exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        )
        output = (stdout or "") + (stderr or "")
        output += f"\nerror: ebrowse tool timed out after {policy.timeout_s:g}s"

    if policy.capture:
        from ebrowse_evals.capture_hook import main as capture_main

        capture_main([str(policy.run_dir / "capture" / f"{call}.json")])
    output, truncated = _clip_output(output.strip(), policy.max_output_bytes)
    return {
        "ok": exit_code == 0,
        "output": output,
        "details": {
            "error_class": None
            if exit_code == 0
            else "tool_timeout"
            if timed_out
            else "tool_error",
            "verb": args[0],
            "call": call,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output_truncated": truncated,
        },
    }


def parse_command(command: str, max_bytes: int) -> list[str]:
    size = len(command.encode("utf-8"))
    if size > max_bytes:
        raise PolicyBlock(None, f"command uses {size} bytes; limit is {max_bytes}")
    try:
        return shlex.split(command, posix=True)
    except ValueError as exc:
        raise PolicyBlock(None, f"could not parse command arguments: {exc}") from None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    path_value = os.environ.get(POLICY_FILE_ENV)
    if not path_value:
        result = {
            "ok": False,
            "output": f"error: {POLICY_FILE_ENV} is not configured",
            "details": {"error_class": "policy_setup"},
        }
    else:
        try:
            policy = load_policy(Path(path_value))
            if len(args) != 2 or args[0] != "--command":
                raise PolicyBlock(None, "browser tool requires one --command value")
            result = execute(parse_command(args[1], policy.max_args_bytes), policy)
        except PolicyBlock as exc:
            result = {
                "ok": False,
                "output": f"error: blocked by browser-only policy: {exc.reason}",
                "details": {
                    "error_class": "policy_block",
                    "verb": exc.verb,
                    "reason": exc.reason,
                },
            }
        except Exception as exc:  # fail closed: never fall back to arbitrary execution
            result = {
                "ok": False,
                "output": f"error: browser tool policy failed closed: {type(exc).__name__}: {exc}",
                "details": {"error_class": "policy_setup"},
            }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
