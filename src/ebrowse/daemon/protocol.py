"""Wire protocol: newline-delimited JSON over a unix socket, one request per
connection. Deliberately dumb — the CLI sends one line, reads one line, exits.

Request : {"id": str, "session": str, "verb": str, "args": {...}}
Response: {"id": str, "ok": bool, "output": str, "error": str|null, "exit_code": int}

`output` is the human/agent-facing text (docs/output-contracts.md formats).
`exit_code` follows the contract: 0 ok, 1 action failed, 2 bad usage/stale ref,
3 daemon/browser failure.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from ebrowse.errors import CommandError, ExitCode

__all__ = ["Request", "Response", "CommandError", "ExitCode"]


@dataclass(slots=True)
class Request:
    verb: str
    session: str = "default"
    args: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def encode(self) -> bytes:
        return (
            json.dumps(
                {"id": self.id, "session": self.session, "verb": self.verb, "args": self.args}
            )
            + "\n"
        ).encode()

    @classmethod
    def decode(cls, line: bytes) -> Request:
        d = json.loads(line)
        return cls(
            verb=d["verb"],
            session=d.get("session", "default"),
            args=d.get("args", {}),
            id=d.get("id", ""),
        )


@dataclass(slots=True)
class Response:
    id: str
    ok: bool
    output: str = ""
    error: str | None = None
    exit_code: int = 0

    def encode(self) -> bytes:
        return (
            json.dumps(
                {
                    "id": self.id,
                    "ok": self.ok,
                    "output": self.output,
                    "error": self.error,
                    "exit_code": self.exit_code,
                }
            )
            + "\n"
        ).encode()

    @classmethod
    def decode(cls, line: bytes) -> Response:
        d = json.loads(line)
        return cls(
            id=d.get("id", ""),
            ok=d.get("ok", False),
            output=d.get("output", ""),
            error=d.get("error"),
            exit_code=d.get("exit_code", ExitCode.INTERNAL),
        )
