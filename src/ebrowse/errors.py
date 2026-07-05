"""Shared error type. Lives at the package root so core/ never imports daemon/."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Process/wire exit codes (docs/output-contracts.md)."""

    OK = 0
    ACTION_FAILED = 1
    USAGE = 2  # bad usage / stale ref
    INTERNAL = 3  # daemon or browser failure


class CommandError(Exception):
    """Raised by verb handlers and resolvers; carries the agent-facing message
    (which must name a recovery action, per AGENTS.md principle 8) plus the
    exit code."""

    def __init__(self, message: str, exit_code: int = ExitCode.ACTION_FAILED) -> None:
        super().__init__(message)
        self.exit_code = exit_code
