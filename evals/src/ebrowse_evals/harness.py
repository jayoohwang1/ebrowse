"""Agent harness boundary.

The runner talks to agents only through the ``AgentHarness`` protocol: give it
a prompt, a working dir, env, and a timeout; get back parsed steps + totals.
``PiHarness`` is the concrete implementation, a port of
``experiments/run-agent.sh`` — tool-guide prepending, pi invocation, JSON
event capture, and the browser-only Pi extension. Tests use a fake harness; nothing
in the runner or trace layer knows what "pi" is.

Session parsing mirrors ``experiments/inspect-session.py``: each assistant
turn may carry ``toolCall`` content blocks; ``toolResult`` messages answer
them by ``toolCallId``. Per-turn ``usage.totalTokens`` is the whole context,
so totals report summed output, summed billed input, and *peak* context.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

PROMPT_FILE = "prompt.txt"
PI_EVENTS_FILE = "pi-events.jsonl"
STDERR_FILE = "stderr.log"
INITIAL_OPEN_FILE = "initial-open.txt"
SESSION_DIR = "session"
SPOOL_DIR = "capture"  # per-call debug-capture payloads: capture/<n>.json
DEBUG_LOG_FILE = "ebrowse-debug.jsonl"  # daemon tier-1 events (EBROWSE_DEBUG_LOG)
SYSTEM_PROMPTS_FILE = "system-prompts.jsonl"
TOOL_POLICY_FILE = "browser-tool-policy.json"
NAVIGATION_BOOTSTRAP_FILE = "navigation-bootstrap.json"
DEFAULT_PI_EVENTS_MAX_BYTES = 64 * 1024 * 1024

BROWSER_SYSTEM_PROMPT = """You are a browser automation agent completing one assigned task.
Use only the provided ebrowse tool to inspect and interact with the task website.
Do not claim success unless the browser state supports it. If a tool action is
blocked by policy, do not try to bypass the restriction; use the standard
browser operations available to you or explain that the task could not be completed."""

SKILL_CLI_INTRO = """`ebrowse` is a CLI. Run it via shell. One background daemon owns the browser;
state (page, refs, logins) persists between commands."""
SKILL_TOOL_INTRO = """`ebrowse` is available through the dedicated `ebrowse` tool. Pass each documented
command without the leading `ebrowse` prefix. Browser state (page, refs, logins)
persists between tool calls."""


@dataclass(slots=True)
class ParsedStep:
    """One agent tool-call, harness-agnostic."""

    command: str
    output: str = ""
    is_error: bool = False
    agent_text: str | None = None
    tokens: dict[str, Any] = field(default_factory=dict)
    latency_s: float | None = None
    message_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    call_index: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedMessage:
    """One finalized agent transcript message."""

    sequence: int
    message_id: str
    parent_id: str | None
    role: str
    content: Any
    ts: float | None = None
    turn: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    model: str | None = None
    provider: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    is_error: bool | None = None
    is_start: bool = False


@dataclass(slots=True)
class HarnessResult:
    steps: list[ParsedStep] = field(default_factory=list)
    messages: list[ParsedMessage] = field(default_factory=list)
    start_prompt: str = ""
    system_prompts: list[str] = field(default_factory=list)
    final_answer: str = ""
    totals: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0
    timed_out: bool = False
    tool_limit_hit: bool = False
    session_path: Path | None = None  # saved agent-session transcript, if any


@runtime_checkable
class AgentHarness(Protocol):
    """The runner's only view of an agent."""

    def describe(self) -> dict[str, Any]:
        """Harness/provider/model identity, persisted into run_meta.agent."""
        ...

    def run(
        self,
        prompt: str,
        workdir: Path,
        env: dict[str, str],
        timeout_s: float | None,
        run_dir: Path,
        start_url: str | None = None,
        tool_call_limit: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> HarnessResult:
        """Execute one task. Artifacts (prompt, events, session) land in run_dir."""
        ...


@runtime_checkable
class PreparatoryHarness(Protocol):
    """Optional phase for setup that contributes dynamic run metadata."""

    def prepare_run(
        self,
        env: dict[str, str],
        run_dir: Path,
        start_url: str | None,
        config: dict[str, Any],
    ) -> None:
        """Complete setup and mutate config before run_meta is serialized."""
        ...


# -- pi session parsing (port of inspect-session.py) ------------------------


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            out.append(c.get("text", ""))
    return " ".join(out).strip()


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):  # pi v3 sessions: epoch milliseconds
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_pi_session(entries: list[dict[str, Any]]) -> HarnessResult:
    """Fold a pi session JSONL into steps + final answer + token totals."""
    result = HarnessResult()
    pending: dict[str, ParsedStep] = {}  # toolCallId -> awaiting its result
    issued_at: dict[str, float] = {}
    call_turn: dict[str, int] = {}
    out_tok = in_tok = peak = turns = 0
    first_user = True
    for e in entries:
        if e.get("type") != "message":
            continue
        m = e["message"]
        role = m.get("role")
        message_id = str(e.get("id") or f"message-{len(result.messages) + 1}")
        turn: int | None = turns or None
        if role == "assistant":
            turns += 1
            turn = turns
            u = m.get("usage") or {}
            out_tok += u.get("output", 0)
            in_tok += u.get("input", 0)
            peak = max(peak, u.get("totalTokens", 0))
            txt = _text(m.get("content"))
            if txt:
                result.final_answer = txt
            call_index = 0
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    call_index += 1
                    args = c.get("arguments") or {}
                    if c.get("name") == "ebrowse" and isinstance(args.get("command"), str):
                        command = f"ebrowse {args['command']}"
                    else:
                        command = args.get("command") or json.dumps(args)
                    call_id = str(c.get("id"))
                    step = ParsedStep(
                        command=str(command),
                        agent_text=txt or None,
                        tokens={k: u[k] for k in ("input", "output", "totalTokens") if k in u},
                        message_id=message_id,
                        tool_call_id=call_id,
                        tool_name=str(c.get("name", "")),
                        call_index=call_index,
                    )
                    result.steps.append(step)
                    pending[call_id] = step
                    call_turn[call_id] = turns
                    at = _ts(m.get("timestamp"))
                    if at is not None:
                        issued_at[call_id] = at
        elif role == "toolResult":
            call_id = str(m.get("toolCallId"))
            turn = call_turn.get(call_id, turns or None)
            step = pending.pop(call_id, None)
            if step is not None:
                step.output = _text(m.get("content"))
                step.is_error = bool(m.get("isError"))
                step.details = dict(m.get("details") or {})
                done = _ts(m.get("timestamp"))
                begun = issued_at.get(call_id)
                if done is not None and begun is not None:
                    step.latency_s = done - begun
        if role in {"user", "assistant", "toolResult"}:
            is_start = role == "user" and first_user
            if is_start:
                first_user = False
            result.messages.append(
                ParsedMessage(
                    sequence=len(result.messages) + 1,
                    message_id=message_id,
                    parent_id=str(e["parentId"]) if e.get("parentId") is not None else None,
                    role=str(role),
                    content=m.get("content", ""),
                    ts=_ts(e.get("timestamp")) or _ts(m.get("timestamp")),
                    turn=turn,
                    tool_call_id=(str(m.get("toolCallId")) if m.get("toolCallId") else None),
                    tool_name=(str(m.get("toolName")) if m.get("toolName") else None),
                    model=(str(m.get("model")) if m.get("model") else None),
                    provider=(str(m.get("provider")) if m.get("provider") else None),
                    usage=dict(m.get("usage") or {}),
                    metadata={
                        key: value
                        for key, value in m.items()
                        if key not in {"role", "content", "timestamp", "usage"}
                    },
                    stop_reason=(str(m.get("stopReason")) if m.get("stopReason") else None),
                    is_error=(bool(m.get("isError")) if role == "toolResult" else None),
                    is_start=is_start,
                )
            )
    result.totals = {
        "turns": turns,
        "tool_calls": len(result.steps),
        "output_tokens": out_tok,
        "input_tokens": in_tok,
        "peak_context": peak,
    }
    return result


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """Best-effort JSONL: skips blank/torn lines (timeout kills mid-write)."""
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# -- pi harness --------------------------------------------------------------


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file (experiments/.env), ignoring comments."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def resolve_navigation_domains(
    start_url: str | None,
    mode: str,
    configured: list[str] | tuple[str, ...],
) -> list[str]:
    """Resolve task navigation scope to ebrowse's allowed-domain format."""
    extras = [str(value).strip().lower().rstrip(".") for value in configured if str(value).strip()]
    if mode == "unrestricted":
        return []
    if mode not in {"task-host", "task-redirects", "allowlist"}:
        raise ValueError(
            f"unknown navigation_policy {mode!r} "
            "(want task-host|task-redirects|allowlist|unrestricted)"
        )
    domains = list(extras)
    if mode in {"task-host", "task-redirects"}:
        host = urlsplit(start_url or "").hostname
        if not host:
            raise ValueError(
                f"navigation_policy={mode!r} requires task.url — set a URL, use "
                "'allowlist' with navigation_allowed_domains, or choose 'unrestricted'"
            )
        domains.insert(0, host.lower().rstrip("."))
    if not domains:
        raise ValueError("navigation allowlist is empty — configure navigation_allowed_domains")
    return list(dict.fromkeys(domains))


def _navigation_urls(payload: dict[str, Any], start_url: str) -> tuple[list[str], str]:
    """Extract the ordered main-frame navigation chain from debug-capture."""
    urls = [start_url]
    for event in payload.get("events") or []:
        if not isinstance(event, dict) or event.get("kind") != "navigation":
            continue
        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue
        candidate = data.get("to") or data.get("url")
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            urls.append(candidate)
    browser = payload.get("browser") or {}
    final_url = browser.get("url") if isinstance(browser, dict) else None
    if isinstance(final_url, str) and final_url.startswith(("http://", "https://")):
        urls.append(final_url)
    return list(dict.fromkeys(urls)), str(final_url or start_url)


def _local_task_url(url: str) -> bool:
    import ipaddress

    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _string_list_config(config: dict[str, Any], key: str) -> list[str]:
    value = config.get(key) or []
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings")
    return list(value)


@dataclass(slots=True)
class PiHarness:
    """Drives the pi coding agent as a subprocess, like run-agent.sh does."""

    provider: str
    model: str
    tool: str = "none"  # "ebrowse" | "agent-browser" | "none"
    repo_root: Path = Path(".")
    worktree: bool = False  # shim `ebrowse` to repo_root/.venv (run-agent.sh -w)
    capture: bool = False  # instrument each ebrowse call (spool + debug log)
    pi_bin: str = "pi"
    ebrowse_bin: str | None = None  # explicit target for the shim (tests)
    pi_events_max_bytes: int = DEFAULT_PI_EVENTS_MAX_BYTES

    def describe(self) -> dict[str, Any]:
        return {
            "harness": "pi",
            "provider": self.provider,
            "model": self.model,
            "tool": self.tool,
            "tool_mode": "browser-only" if self.tool == "ebrowse" else "builtin",
        }

    def tool_preamble(self) -> str:
        """Each tool is driven from its own documented guide (fair across runs)."""
        if self.tool == "ebrowse":
            skill = self.repo_root / "SKILL.md"
            guide = skill.read_text(encoding="utf-8") if skill.is_file() else ""
            guide = guide.replace(SKILL_CLI_INTRO, SKILL_TOOL_INTRO, 1)
            return f"You control a web browser using the `ebrowse` tool. Its operating guide follows.\n\n{guide}\n"
        if self.tool == "agent-browser":
            proc = subprocess.run(
                ["agent-browser", "skills", "get", "core", "--full"],
                capture_output=True,
                text=True,
                check=False,
            )
            guide = proc.stdout if proc.returncode == 0 else ""
            return f"You control a web browser using the 'agent-browser' CLI. Its operating guide follows.\n\n{guide}\n"
        if self.tool == "none":
            return ""
        raise ValueError(f"unknown tool: {self.tool} (want ebrowse|agent-browser|none)")

    def _ebrowse_target(self) -> tuple[str, list[str]]:
        """Fixed executable + prefix used by setup and the policy launcher."""
        if self.worktree:
            venv_py = self.repo_root / ".venv" / "bin" / "python"
            if not venv_py.exists():
                raise FileNotFoundError(
                    f"no venv at {venv_py} — run 'uv sync' in {self.repo_root} first"
                )
            return str(venv_py), ["-m", "ebrowse.cli.main"]
        real = self.ebrowse_bin or shutil.which("ebrowse")
        if real is None:
            raise FileNotFoundError(
                "no `ebrowse` on PATH to instrument — install it (uv tool install) "
                "or pass --worktree to use this checkout's venv"
            )
        return str(real), []

    def _eval_env(self, run_dir: Path, env: dict[str, str]) -> tuple[dict[str, str], Path]:
        """Build the isolated per-run environment shared by prepare and run."""
        full_env = {**os.environ, **env}
        runtime_key = hashlib.sha256(str(run_dir).encode()).hexdigest()[:16]
        runtime_dir = Path(tempfile.gettempdir()) / "ebrowse-eval-runtime" / runtime_key
        cache_dir = run_dir / "cache"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        full_env["XDG_RUNTIME_DIR"] = str(runtime_dir)
        if "PLAYWRIGHT_BROWSERS_PATH" not in full_env:
            shared_browsers = Path.home() / ".cache" / "ms-playwright"
            if shared_browsers.is_dir():
                full_env["PLAYWRIGHT_BROWSERS_PATH"] = str(shared_browsers)
        full_env["XDG_CACHE_HOME"] = str(cache_dir)
        return full_env, runtime_dir

    @staticmethod
    def _stop_ebrowse_daemon(
        executable: str,
        prefix: list[str],
        env: dict[str, str],
        timeout_s: float = 15.0,
    ) -> None:
        """Stop and wait for full cleanup, including Chromium profile release.

        The stop response only acknowledges the shutdown request. The daemon
        removes its socket after awaiting every browser session's close, making
        socket disappearance the lifecycle boundary safe for a replacement.
        """
        subprocess.run(
            [executable, *prefix, "daemon", "stop"],
            env=env,
            capture_output=True,
            check=False,
        )
        socket_file = Path(env["XDG_RUNTIME_DIR"]) / "ebrowse.sock"
        deadline = time.monotonic() + timeout_s
        while socket_file.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"ebrowse daemon did not release its socket within {timeout_s:g}s"
                )
            time.sleep(0.05)

    def prepare_run(
        self,
        env: dict[str, str],
        run_dir: Path,
        start_url: str | None,
        config: dict[str, Any],
    ) -> None:
        """Discover redirect scope before the runner writes authoritative metadata."""
        if self.tool != "ebrowse" or config.get("navigation_policy") != "task-redirects":
            return
        if config.get("navigation_bootstrap"):
            return
        domains = resolve_navigation_domains(
            start_url,
            "task-redirects",
            _string_list_config(config, "navigation_allowed_domains"),
        )
        assert start_url is not None
        full_env, runtime_dir = self._eval_env(run_dir, env)
        executable, prefix = self._ebrowse_target()
        bootstrap_timeout = float(config.get("navigation_bootstrap_timeout_s", 15.0))
        bootstrap_max_hosts = int(config.get("navigation_bootstrap_max_hosts", 5))
        if bootstrap_timeout <= 0 or bootstrap_max_hosts <= 0:
            raise ValueError("navigation bootstrap timeout and max hosts must be positive")
        full_env.pop("EBROWSE_SECURITY_ALLOWED_DOMAINS", None)
        full_env["EBROWSE_SECURITY_BOOTSTRAP_NAVIGATION"] = "true"
        full_env["EBROWSE_SECURITY_BOOTSTRAP_MAX_HOSTS"] = str(bootstrap_max_hosts)
        full_env["EBROWSE_SECURITY_BLOCK_PRIVATE_NETWORK"] = (
            "false" if _local_task_url(start_url) else "true"
        )
        self._stop_ebrowse_daemon(executable, prefix, full_env)
        try:
            bootstrap_open = subprocess.run(
                [executable, *prefix, "open", start_url],
                env=full_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=bootstrap_timeout,
            )
            bootstrap_error = (
                bootstrap_open.stdout + bootstrap_open.stderr
                if bootstrap_open.returncode != 0
                else None
            )
        except subprocess.TimeoutExpired as exc:
            bootstrap_error = f"navigation bootstrap timed out: {exc}"
        payload: dict[str, Any] = {}
        try:
            from ebrowse_evals.capture import DaemonCaptureClient

            payload = DaemonCaptureClient(
                socket_path=runtime_dir / "ebrowse.sock", timeout_s=10
            ).debug_capture()
        except Exception as exc:  # noqa: BLE001 - recorded, then scope freezes safely
            bootstrap_error = bootstrap_error or f"capture failed: {type(exc).__name__}: {exc}"
        observed_urls, final_url = _navigation_urls(payload, start_url)
        observed_hosts = [
            host.lower().rstrip(".") for url in observed_urls if (host := urlsplit(url).hostname)
        ]
        observed_hosts = list(dict.fromkeys(observed_hosts))
        if len(observed_hosts) > bootstrap_max_hosts:
            raise ValueError(
                f"navigation bootstrap observed {len(observed_hosts)} hosts; "
                f"limit is {bootstrap_max_hosts}"
            )
        domains = list(dict.fromkeys([*observed_hosts, *domains]))
        bootstrap_record = {
            "requested_url": start_url,
            "observed_urls": observed_urls,
            "final_url": final_url,
            "observed_hosts": observed_hosts,
            "resolved_domains": domains,
            "timeout_s": bootstrap_timeout,
            "max_hosts": bootstrap_max_hosts,
            "error": bootstrap_error,
        }
        (run_dir / NAVIGATION_BOOTSTRAP_FILE).write_text(
            json.dumps(bootstrap_record, indent=2) + "\n", encoding="utf-8"
        )
        config["navigation_bootstrap"] = bootstrap_record
        config["resolved_navigation_domains"] = domains
        self._stop_ebrowse_daemon(executable, prefix, full_env)

    def run(
        self,
        prompt: str,
        workdir: Path,
        env: dict[str, str],
        timeout_s: float | None,
        run_dir: Path,
        start_url: str | None = None,
        tool_call_limit: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> HarnessResult:
        if shutil.which(self.pi_bin) is None:
            raise FileNotFoundError(
                f"'{self.pi_bin}' not on PATH — npm i -g @earendil-works/pi-coding-agent"
            )
        config = config or {}
        navigation_mode = str(config.get("navigation_policy", "task-host"))
        if navigation_mode == "task-redirects" and not config.get("navigation_bootstrap"):
            # Direct callers retain redirect discovery. The runner invokes it
            # earlier so run_meta is visible before the long-running Pi call.
            self.prepare_run(env, run_dir, start_url, config)
        full_env, _runtime_dir = self._eval_env(run_dir, env)
        ebrowse_target: tuple[str, list[str]] | None = None
        if self.tool == "ebrowse":
            ebrowse_target = self._ebrowse_target()
            resolved = config.get("resolved_navigation_domains")
            domains = (
                _string_list_config(config, "resolved_navigation_domains")
                if resolved is not None
                else resolve_navigation_domains(
                    start_url,
                    navigation_mode,
                    _string_list_config(config, "navigation_allowed_domains"),
                )
            )
            executable, prefix = ebrowse_target
            full_env.pop("EBROWSE_SECURITY_BOOTSTRAP_NAVIGATION", None)
            full_env.pop("EBROWSE_SECURITY_BOOTSTRAP_MAX_HOSTS", None)
            if navigation_mode == "task-redirects" and start_url is not None:
                full_env["EBROWSE_SECURITY_BLOCK_PRIVATE_NETWORK"] = (
                    "false" if _local_task_url(start_url) else "true"
                )
            if domains:
                full_env["EBROWSE_SECURITY_ALLOWED_DOMAINS"] = ",".join(domains)
            else:
                full_env.pop("EBROWSE_SECURITY_ALLOWED_DOMAINS", None)
            if self.capture:
                full_env["EBROWSE_DEBUG_LOG"] = str(run_dir / DEBUG_LOG_FILE)
            self._stop_ebrowse_daemon(executable, prefix, full_env)
            allowed_verbs = _string_list_config(config, "ebrowse_allowed_verbs")
            from ebrowse_evals.pi_tool import DEFAULT_ALLOWED_VERBS

            if not allowed_verbs:
                allowed_verbs = list(DEFAULT_ALLOWED_VERBS)
            policy_path = run_dir / TOOL_POLICY_FILE
            policy_path.write_text(
                json.dumps(
                    {
                        "executable": executable,
                        "argv_prefix": prefix,
                        "run_dir": str(run_dir),
                        "allowed_verbs": allowed_verbs,
                        "allowed_domains": domains,
                        "timeout_s": float(config.get("ebrowse_tool_timeout_s", 150.0)),
                        "max_args_bytes": int(config.get("ebrowse_tool_args_max_bytes", 16_384)),
                        "max_output_bytes": int(
                            config.get("ebrowse_tool_output_max_bytes", 262_144)
                        ),
                        "capture": self.capture,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            full_env["EBROWSE_EVAL_TOOL_POLICY"] = str(policy_path)
            full_env["EBROWSE_EVAL_PYTHON"] = sys.executable
        opened = False
        if start_url and ebrowse_target is not None:
            executable, prefix = ebrowse_target
            try:
                initial = subprocess.run(
                    [executable, *prefix, "open", start_url],
                    env=full_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=150,
                )
            except subprocess.TimeoutExpired as e:
                initial_text = f"initial navigation timed out: {e}\n"
                initial_returncode = -1
            else:
                initial_text = initial.stdout + initial.stderr
                initial_returncode = initial.returncode
            (run_dir / INITIAL_OPEN_FILE).write_text(initial_text, encoding="utf-8")
            opened = initial_returncode == 0
        site_instruction = ""
        if start_url:
            state = "is already open" if opened else "should be opened"
            site_instruction = (
                f"\n# Browser starting state\nThe target website {state} at {start_url}. "
                "Complete the task on that target website. Do not use search engines or "
                "unrelated websites.\n"
            )
        if self.tool == "ebrowse":
            site_instruction += (
                "Use the `ebrowse` tool and omit the `ebrowse` prefix from its command. "
                'For example, `ebrowse outline` is {"command":"outline"}. Shell '
                "operators, redirection, and expansion are unavailable.\n"
            )
        full_prompt = f"{self.tool_preamble()}{site_instruction}\n# Task\n{prompt}"
        (run_dir / PROMPT_FILE).write_text(full_prompt, encoding="utf-8")
        workdir.mkdir(parents=True, exist_ok=True)
        session_dir = run_dir / SESSION_DIR
        session_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.pi_bin,
            "--provider",
            self.provider,
            "--model",
            self.model,
            "--session-dir",
            str(session_dir),
            "--name",
            run_dir.name,
        ]
        if self.tool == "ebrowse":
            cmd.extend(
                [
                    "--system-prompt",
                    BROWSER_SYSTEM_PROMPT,
                    "--no-builtin-tools",
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "--no-context-files",
                    "--no-approve",
                    "--offline",
                ]
            )
        system_prompts_path = run_dir / SYSTEM_PROMPTS_FILE
        observer = Path(__file__).resolve().parents[2] / "pi_extensions" / "trace-observer.ts"
        if observer.is_file():
            cmd.extend(["--extension", str(observer)])
            full_env["EBROWSE_EVAL_SYSTEM_PROMPTS"] = str(system_prompts_path)
        if self.tool == "ebrowse":
            browser_extension = (
                Path(__file__).resolve().parents[2] / "pi_extensions" / "ebrowse-tool.ts"
            )
            if not browser_extension.is_file():
                raise FileNotFoundError(f"missing Pi browser extension: {browser_extension}")
            cmd.extend(["--extension", str(browser_extension), "--tools", "ebrowse"])
        elif self.tool == "agent-browser":
            cmd.extend(["--tools", "bash"])
        cmd.extend(["--mode", "json", "-p", full_prompt])
        timed_out = False
        tool_limit_hit = False
        # Stream stdout/stderr straight to files: a timeout kill then loses
        # nothing (subprocess capture buffers would), and the event stream
        # doubles as the step source when pi never got to write its session.
        events_path = run_dir / PI_EVENTS_FILE
        fallback_entries: list[dict[str, Any]] = []
        with events_path.open("wb") as out_f, (run_dir / STDERR_FILE).open("wb") as err_f:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                env=full_env,
                stdout=subprocess.PIPE,
                stderr=err_f,
                start_new_session=True,
            )
            assert proc.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout_s if timeout_s is not None else None
            completed_tool_calls = 0
            persisted_bytes = 0
            truncation_recorded = False
            truncation_marker = (
                json.dumps(
                    {
                        "type": "events_truncated",
                        "max_bytes": self.pi_events_max_bytes,
                    },
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )

            def process_line(line: bytes) -> dict[str, Any] | None:
                """Keep fallback messages in memory and persist only bounded,
                non-cumulative diagnostics."""
                nonlocal persisted_bytes, truncation_recorded
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    event = None
                if isinstance(event, dict) and event.get("type") == "message_end":
                    message = event.get("message")
                    if isinstance(message, dict):
                        fallback_entries.append({"type": "message", "message": message})
                # These contain the full message-so-far on every token; saving
                # them makes the raw event file grow quadratically.
                if isinstance(event, dict) and event.get("type") == "message_update":
                    return event
                if not truncation_recorded:
                    # Reserve enough space to always explain why the artifact
                    # stopped when a later event reaches the ceiling.
                    if (
                        persisted_bytes + len(line) + len(truncation_marker)
                        <= self.pi_events_max_bytes
                    ):
                        out_f.write(line)
                        out_f.flush()
                        persisted_bytes += len(line)
                    else:
                        if persisted_bytes + len(truncation_marker) <= self.pi_events_max_bytes:
                            out_f.write(truncation_marker)
                            out_f.flush()
                            persisted_bytes += len(truncation_marker)
                        truncation_recorded = True
                return event if isinstance(event, dict) else None

            while proc.poll() is None:
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    with suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGTERM)
                    break
                events = selector.select(timeout=0.25)
                for _, _ in events:
                    line = proc.stdout.readline()
                    if not line:
                        continue
                    event = process_line(line)
                    if event is None:
                        continue
                    if event.get("type") != "message_end":
                        continue
                    message = event.get("message") or {}
                    if message.get("role") != "toolResult":
                        continue
                    completed_tool_calls += 1
                    if tool_call_limit and completed_tool_calls >= tool_call_limit:
                        tool_limit_hit = True
                        with suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGTERM)
                        break
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            # Drain bytes already written by pi before termination.
            rest = proc.stdout.read()
            for line in rest.splitlines(keepends=True):
                process_line(line)
            exit_code = proc.returncode
            selector.close()
        # The session file (written by pi at exit) is the ground truth for
        # steps; on timeout it never lands, so fall back to the live event
        # stream's message_end records (same message payloads).
        sessions = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        entries: list[dict[str, Any]] = []
        session_path: Path | None = None
        if sessions:
            session_path = sessions[-1]
            entries = _jsonl(session_path)
        if not any(e.get("type") == "message" for e in entries):
            entries = fallback_entries
        result = parse_pi_session(entries)
        result.start_prompt = full_prompt
        for entry in _jsonl(system_prompts_path):
            value = entry.get("systemPrompt")
            if isinstance(value, str) and (
                not result.system_prompts or value != result.system_prompts[-1]
            ):
                result.system_prompts.append(value)
        # A very fast subprocess can enqueue another event before SIGTERM is
        # delivered. Keep the trace and reported total at the configured
        # boundary even if those bytes were already buffered in stdout.
        if tool_limit_hit and tool_call_limit and len(result.steps) > tool_call_limit:
            result.steps = result.steps[:tool_call_limit]
            result.totals["tool_calls"] = len(result.steps)
        result.session_path = session_path
        result.exit_code = exit_code
        result.timed_out = timed_out
        result.tool_limit_hit = tool_limit_hit
        if ebrowse_target is not None:
            executable, prefix = ebrowse_target
            with suppress(TimeoutError):
                self._stop_ebrowse_daemon(executable, prefix, full_env)
        return result
