"""Agent harness boundary.

The runner talks to agents only through the ``AgentHarness`` protocol: give it
a prompt, a working dir, env, and a timeout; get back parsed steps + totals.
``PiHarness`` is the concrete implementation, a port of
``experiments/run-agent.sh`` — tool-guide prepending, pi invocation, JSON
event capture, and the worktree PATH shim. Tests use a fake harness; nothing
in the runner or trace layer knows what "pi" is.

Session parsing mirrors ``experiments/inspect-session.py``: each assistant
turn may carry ``toolCall`` content blocks; ``toolResult`` messages answer
them by ``toolCallId``. Per-turn ``usage.totalTokens`` is the whole context,
so totals report summed output, summed billed input, and *peak* context.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

PROMPT_FILE = "prompt.txt"
PI_EVENTS_FILE = "pi-events.jsonl"
STDERR_FILE = "stderr.log"
SESSION_DIR = "session"
SPOOL_DIR = "capture"  # per-call debug-capture payloads: capture/<n>.json
DEBUG_LOG_FILE = "ebrowse-debug.jsonl"  # daemon tier-1 events (EBROWSE_DEBUG_LOG)


@dataclass(slots=True)
class ParsedStep:
    """One agent tool-call, harness-agnostic."""

    command: str
    output: str = ""
    is_error: bool = False
    agent_text: str | None = None
    tokens: dict[str, Any] = field(default_factory=dict)
    latency_s: float | None = None


@dataclass(slots=True)
class HarnessResult:
    steps: list[ParsedStep] = field(default_factory=list)
    final_answer: str = ""
    totals: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0
    timed_out: bool = False
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
    ) -> HarnessResult:
        """Execute one task. Artifacts (prompt, events, session) land in run_dir."""
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
    out_tok = in_tok = peak = turns = 0
    for e in entries:
        if e.get("type") != "message":
            continue
        m = e["message"]
        role = m.get("role")
        if role == "assistant":
            turns += 1
            u = m.get("usage") or {}
            out_tok += u.get("output", 0)
            in_tok += u.get("input", 0)
            peak = max(peak, u.get("totalTokens", 0))
            txt = _text(m.get("content"))
            if txt:
                result.final_answer = txt
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    args = c.get("arguments") or {}
                    command = args.get("command") or json.dumps(args)
                    step = ParsedStep(
                        command=str(command),
                        agent_text=txt or None,
                        tokens={k: u[k] for k in ("input", "output", "totalTokens") if k in u},
                    )
                    result.steps.append(step)
                    call_id = str(c.get("id"))
                    pending[call_id] = step
                    at = _ts(m.get("timestamp"))
                    if at is not None:
                        issued_at[call_id] = at
        elif role == "toolResult":
            call_id = str(m.get("toolCallId"))
            step = pending.pop(call_id, None)
            if step is None:
                continue
            step.output = _text(m.get("content"))
            step.is_error = bool(m.get("isError"))
            done = _ts(m.get("timestamp"))
            begun = issued_at.get(call_id)
            if done is not None and begun is not None:
                step.latency_s = done - begun
    result.totals = {
        "turns": turns,
        "tool_calls": len(result.steps),
        "output_tokens": out_tok,
        "input_tokens": in_tok,
        "peak_context": peak,
    }
    return result


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

    def describe(self) -> dict[str, Any]:
        return {
            "harness": "pi",
            "provider": self.provider,
            "model": self.model,
            "tool": self.tool,
        }

    def tool_preamble(self) -> str:
        """Each tool is driven from its own documented guide (fair across runs)."""
        if self.tool == "ebrowse":
            skill = self.repo_root / "SKILL.md"
            guide = skill.read_text(encoding="utf-8") if skill.is_file() else ""
            return f"You control a web browser using the 'ebrowse' CLI. Its operating guide follows.\n\n{guide}\n"
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

    def _shim_target(self) -> str:
        """The real ebrowse invocation the shim wraps (quoted shell fragment)."""
        if self.worktree:
            venv_py = self.repo_root / ".venv" / "bin" / "python"
            if not venv_py.exists():
                raise FileNotFoundError(
                    f"no venv at {venv_py} — run 'uv sync' in {self.repo_root} first"
                )
            return f'"{venv_py}" -m ebrowse.cli.main'
        real = self.ebrowse_bin or shutil.which("ebrowse")
        if real is None:
            raise FileNotFoundError(
                "no `ebrowse` on PATH to instrument — install it (uv tool install) "
                "or pass --worktree to use this checkout's venv"
            )
        return f'"{real}"'

    def _install_shim(self, run_dir: Path, env: dict[str, str]) -> None:
        """Wrap `ebrowse` for worktree redirection and/or per-call capture.

        The shim is the ONE place in the whole harness that runs at exactly the
        right moment for post-action state: synchronously after each ebrowse
        call, before the agent's next turn. With capture on it (a) numbers the
        call, (b) exports EBROWSE_REQUEST_ID=call-<n> so the daemon's debug
        events join deterministically to this call, and (c) spools a
        debug-capture payload to capture/<n>.json. EBROWSE_EVAL_NOHOOK skips
        the instrumentation (used for our own setup calls below)."""
        target = self._shim_target()
        shim_dir = run_dir / "bin"
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim = shim_dir / "ebrowse"
        if self.capture:
            spool = run_dir / SPOOL_DIR
            spool.mkdir(parents=True, exist_ok=True)
            # The agent drives commands serially, so the naive counter file is
            # race-free in practice; a torn read only skips a spool slot.
            shim.write_text(
                "#!/usr/bin/env bash\n"
                f'if [ -n "$EBROWSE_EVAL_NOHOOK" ]; then exec {target} "$@"; fi\n'
                f'ctr="{spool}/seq"\n'
                'n=$(( $(cat "$ctr" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$ctr"\n'
                'export EBROWSE_REQUEST_ID="call-$n"\n'
                f'{target} "$@"\n'
                "rc=$?\n"
                f'"{sys.executable}" -m ebrowse_evals.capture_hook "{spool}/$n.json" || true\n'
                "exit $rc\n"
            )
            # The daemon reads its config env at startup; it inherits ours via
            # the first shimmed call (we stop any old daemon below).
            env["EBROWSE_DEBUG_LOG"] = str(run_dir / DEBUG_LOG_FILE)
        else:
            shim.write_text(f'#!/usr/bin/env bash\nexec {target} "$@"\n')
        shim.chmod(0o755)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', os.defpath)}"
        # Free the socket so the agent's first call restarts the daemon on the
        # shimmed code/env (local venv and/or EBROWSE_DEBUG_LOG).
        subprocess.run(
            [str(shim), "daemon", "stop"],
            env={**env, "EBROWSE_EVAL_NOHOOK": "1"},
            capture_output=True,
            check=False,
        )

    def run(
        self,
        prompt: str,
        workdir: Path,
        env: dict[str, str],
        timeout_s: float | None,
        run_dir: Path,
    ) -> HarnessResult:
        if shutil.which(self.pi_bin) is None:
            raise FileNotFoundError(
                f"'{self.pi_bin}' not on PATH — npm i -g @earendil-works/pi-coding-agent"
            )
        full_env = {**os.environ, **env}
        if self.worktree or self.capture:
            self._install_shim(run_dir, full_env)
        full_prompt = f"{self.tool_preamble()}\n# Task\n{prompt}"
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
            "--mode",
            "json",
            "-p",
            full_prompt,
        ]
        timed_out = False
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                env=full_env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
            exit_code = -1
        (run_dir / PI_EVENTS_FILE).write_text(stdout, encoding="utf-8")
        (run_dir / STDERR_FILE).write_text(stderr, encoding="utf-8")
        # The session file (written by pi itself) is the ground truth for steps.
        sessions = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if sessions:
            entries = [
                json.loads(line)
                for line in sessions[-1].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            result = parse_pi_session(entries)
            result.session_path = sessions[-1]
        else:
            result = HarnessResult()
        result.exit_code = exit_code
        result.timed_out = timed_out
        return result
