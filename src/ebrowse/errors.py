"""Shared error type. Lives at the package root so core/ never imports daemon/."""

from __future__ import annotations


class CommandError(Exception):
    """Raised by verb handlers and resolvers; carries the agent-facing message
    (which must name a recovery action, per AGENTS.md principle 8) plus the
    §4.4 exit code: 1 action failed, 2 bad usage/stale ref, 3 daemon/browser."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code
