"""Per-step browser-state capture: the harness-side hook the runner calls
after every agent tool-call.

Capture is UNCONDITIONAL — regardless of what the agent asked for, each step's
trace gets post-action browser state, a screenshot, the DomSnapshot, and the
page events accumulated since the previous capture.

How it talks to the browser (the key design decision): via the running ebrowse
daemon's additive ``debug-capture`` verb, over the same unix-socket protocol
the CLI uses. The daemon owns the Playwright session (launch mode uses a
persistent context with no CDP endpoint to share), so a second Playwright
connection is impossible in the default mode and would race the session lock in
CDP mode. ``debug-capture`` runs inside the session lock, reuses the session's
own ``core.snapshot.capture()`` machinery, and returns one JSON payload:

    {"browser": {...}, "screenshot_b64": ..., "dom_snapshot": {...},
     "snapshot_reused": bool, "events": [...], "errors": {...}}

The daemon reuses the DomSnapshot it already took for the previous verb's
observation when no possibly-mutating verb ran since (``snapshot_reused``), so
tracing adds no second snapshot walk on the common path; the content-addressed
blob store dedupes identical payloads across steps on top of that.

Failure isolation (binding): ``StepCapture.capture`` never raises. Any failure
degrades to a partial Step (fields None) plus an ``anomaly`` record.
"""

from __future__ import annotations

import base64
import contextlib
import json
import socket
from pathlib import Path
from typing import Any, Protocol

from ebrowse_evals.trace.records import Anomaly, BrowserEvent, Step
from ebrowse_evals.trace.store import TraceWriter

CAPTURE_VERB = "debug-capture"


class CaptureError(Exception):
    """A capture request that could not produce a payload."""


class CaptureClient(Protocol):
    """Anything that can run the debug-capture verb and return its payload."""

    def debug_capture(self) -> dict[str, Any]: ...


class DaemonCaptureClient:
    """Talks to the ebrowse daemon over its unix socket (one request per
    connection, newline-delimited JSON — same wire protocol as the CLI)."""

    def __init__(
        self,
        socket_path: Path | None = None,
        session: str = "default",
        timeout_s: float = 60.0,
    ) -> None:
        if socket_path is None:
            from ebrowse.config import socket_path as ebrowse_socket_path

            socket_path = ebrowse_socket_path()
        self.socket_path = socket_path
        self.session = session
        self.timeout_s = timeout_s

    def debug_capture(self) -> dict[str, Any]:
        from ebrowse.daemon.protocol import Request, Response

        req = Request(verb=CAPTURE_VERB, session=self.session)
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout_s)
            sock.connect(str(self.socket_path))
            with sock, sock.makefile("rwb") as f:
                f.write(req.encode())
                f.flush()
                line = f.readline()
        except OSError as e:
            raise CaptureError(f"cannot reach ebrowse daemon at {self.socket_path}: {e}") from e
        if not line:
            raise CaptureError("daemon closed the connection without replying")
        resp = Response.decode(line)
        if not resp.ok:
            raise CaptureError(f"debug-capture failed: {resp.error}")
        try:
            payload = json.loads(resp.output)
        except json.JSONDecodeError as e:
            raise CaptureError(f"debug-capture returned non-JSON output: {e}") from e
        if not isinstance(payload, dict):
            raise CaptureError("debug-capture payload is not an object")
        return payload


class StepCapture:
    """Fills a Step record's browser/screenshot/dom_snapshot fields and emits
    BrowserEvent records, given a TraceWriter and a daemon connection.

    Runner contract (per-step hook): after executing the agent's tool-call for
    step N and before writing the Step record, call ``on_step(writer, step)`` —
    it fills ``step.browser`` / ``step.screenshot`` / ``step.dom_snapshot`` in
    place and appends BrowserEvent records via that writer. ``capture(step_id)``
    is the same operation returning the fields as a dict (keys exactly
    ``browser``, ``screenshot``, ``dom_snapshot``).
    """

    def __init__(self, writer: TraceWriter | None, client: CaptureClient) -> None:
        self.writer = writer
        self.client = client

    def on_step(self, writer: TraceWriter, step: Step) -> None:
        """Fill the Step record's capture fields in place (runner hook shape)."""
        fields = self.capture(step.step or 0, writer=writer)
        step.browser = fields["browser"]
        step.screenshot = fields["screenshot"]
        step.dom_snapshot = fields["dom_snapshot"]

    def capture(self, step_id: int, writer: TraceWriter | None = None) -> dict[str, Any]:
        writer = writer or self.writer
        fields: dict[str, Any] = {"browser": {}, "screenshot": None, "dom_snapshot": None}
        if writer is None:  # constructed without a writer and none passed in
            return fields
        try:
            payload = self.client.debug_capture()
        except Exception as e:  # noqa: BLE001 — isolation: capture never raises
            self._anomaly(writer, step_id, "capture_failed", f"{type(e).__name__}: {e}")
            return fields
        try:
            self._ingest(writer, step_id, payload, fields)
        except Exception as e:  # noqa: BLE001 — a bad payload must not kill the run
            self._anomaly(writer, step_id, "capture_failed", f"payload ingest failed: {e}")
        return fields

    # ------------------------------------------------------------------ impl --

    def _ingest(
        self, writer: TraceWriter, step_id: int, payload: dict[str, Any], fields: dict[str, Any]
    ) -> None:
        ingest_payload(writer, step_id, payload, fields)

    def _anomaly(
        self,
        writer: TraceWriter,
        step_id: int,
        kind: str,
        message: str,
        fields: dict[str, Any] | None = None,
    ) -> None:
        write_capture_anomaly(writer, step_id, kind, message, fields)


def ingest_payload(
    writer: TraceWriter,
    step_id: int,
    payload: dict[str, Any],
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn one debug-capture payload into trace state: fills (and returns)
    the Step fields dict and appends BrowserEvent/Anomaly records. Shared by
    the live ``StepCapture`` hook and the post-run spool ingest (ingest.py)."""
    if fields is None:
        fields = {"browser": {}, "screenshot": None, "dom_snapshot": None}
    browser = payload.get("browser")
    if isinstance(browser, dict):
        fields["browser"] = browser

    for ev in payload.get("events") or []:
        if not isinstance(ev, dict):
            continue
        writer.write(
            BrowserEvent(
                step=step_id,
                ts=ev.get("ts"),
                kind=str(ev.get("kind", "")),
                data=ev.get("data") or {},
            )
        )

    shot_b64 = payload.get("screenshot_b64")
    if shot_b64:
        try:
            fields["screenshot"] = writer.put_blob(base64.b64decode(shot_b64), ".png")
        except Exception as e:  # noqa: BLE001
            write_capture_anomaly(
                writer, step_id, "capture_partial", f"screenshot blob failed: {e}"
            )

    snap = payload.get("dom_snapshot")
    if snap is not None:
        # canonical serialization so identical snapshots across steps hash
        # to the same blob (the store dedupes by content)
        data = json.dumps(snap, sort_keys=True, separators=(",", ":")).encode()
        fields["dom_snapshot"] = writer.put_blob(data, ".json")

    errors = payload.get("errors") or {}
    if errors:
        msg = "; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))
        write_capture_anomaly(writer, step_id, "capture_partial", msg, fields=dict(errors))
    return fields


def write_capture_anomaly(
    writer: TraceWriter,
    step_id: int | None,
    kind: str,
    message: str,
    fields: dict[str, Any] | None = None,
) -> None:
    """Contained anomaly write — even a trace-write failure never raises."""
    with contextlib.suppress(Exception):
        writer.write(Anomaly(step=step_id, kind=kind, message=message, fields=fields or {}))
