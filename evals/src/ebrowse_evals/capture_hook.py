"""Post-call capture hook, invoked by the eval shim after every `ebrowse` call.

Usage: python -m ebrowse_evals.capture_hook <spool-file.json>

Runs the daemon's debug-capture verb and spools the raw payload to the given
file; the runner joins spool entries to trace steps after the run (ingest.py).
Runs *synchronously between agent tool-calls* — the only moment post-action
browser state is observable — so it must be fast and it must NEVER fail the
agent's command: any error becomes a ``hook_error`` payload and exit 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m ebrowse_evals.capture_hook <spool-file.json>", file=sys.stderr)
        return 0  # never fail the wrapped command, even on misuse
    out = Path(args[0])
    try:
        from ebrowse_evals.capture import DaemonCaptureClient

        payload = DaemonCaptureClient().debug_capture()
    except Exception as e:  # noqa: BLE001 — isolation: the shim ignores us on error too
        payload = {"hook_error": f"{type(e).__name__}: {e}"}
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.rename(out)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
