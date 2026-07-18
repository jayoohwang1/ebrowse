"""Pure tests for the per-step capture layer (faked daemon, no browser)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ebrowse_evals.capture import StepCapture
from ebrowse_evals.trace.records import Anomaly, BrowserEvent, Step
from ebrowse_evals.trace.store import TraceReader, TraceWriter

PNG = b"\x89PNG\r\n\x1a\nfakepixels"
SNAP = {"url": "http://x/", "vw": 1280, "vh": 720, "root": {"t": "body", "r": [0, 0, 10, 10]}}


def payload(**over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "browser": {
            "url": "http://x/",
            "title": "X",
            "tabs": [{"index": 0, "url": "http://x/", "active": True}],
            "viewport": {"width": 1280, "height": 720},
            "scroll_y": 0,
            "doc_height": 900,
        },
        "screenshot_b64": base64.b64encode(PNG).decode(),
        "dom_snapshot": SNAP,
        "snapshot_reused": False,
        "events": [],
        "errors": {},
    }
    p.update(over)
    return p


class FakeClient:
    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = list(payloads)

    def debug_capture(self) -> dict[str, Any]:
        p = self.payloads.pop(0)
        if isinstance(p, Exception):
            raise p
        return p


def read(run_dir: Path) -> TraceReader:
    # capture alone writes no step records; touch events.jsonl for the reader
    (run_dir / "events.jsonl").touch()
    return TraceReader(run_dir)


def test_capture_fills_step_fields_and_blobs(tmp_path: Path) -> None:
    w = TraceWriter(tmp_path)
    cap = StepCapture(w, FakeClient([payload()]))
    fields = cap.capture(1)
    assert fields["browser"]["url"] == "http://x/"
    assert fields["browser"]["tabs"][0]["active"] is True
    assert fields["screenshot"] and fields["screenshot"].startswith("sha256:")
    assert fields["dom_snapshot"] and fields["dom_snapshot"].startswith("sha256:")
    r = read(tmp_path)
    assert r.blobs.get(fields["screenshot"]) == PNG
    assert json.loads(r.blobs.get(fields["dom_snapshot"])) == SNAP
    assert r.blobs.path(fields["screenshot"]).suffix == ".png"
    assert r.blobs.path(fields["dom_snapshot"]).suffix == ".json"
    assert not [a for a in r.records() if isinstance(a, Anomaly)]


def test_on_step_fills_step_record_in_place(tmp_path: Path) -> None:
    w = TraceWriter(tmp_path)
    cap = StepCapture(None, FakeClient([payload()]))  # writer supplied per call
    step = Step(step=7, command="ebrowse click @b1")
    cap.on_step(w, step)
    assert step.browser["url"] == "http://x/"
    assert step.screenshot and step.screenshot.startswith("sha256:")
    assert step.dom_snapshot and step.dom_snapshot.startswith("sha256:")
    assert step.command == "ebrowse click @b1"  # untouched


def test_identical_payloads_dedupe_to_same_blobs(tmp_path: Path) -> None:
    w = TraceWriter(tmp_path)
    cap = StepCapture(w, FakeClient([payload(), payload()]))
    f1, f2 = cap.capture(1), cap.capture(2)
    assert f1["screenshot"] == f2["screenshot"]
    assert f1["dom_snapshot"] == f2["dom_snapshot"]
    blobs = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert len(blobs) == 2  # one png + one json, not four


def test_events_become_browser_event_records(tmp_path: Path) -> None:
    events = [
        {"kind": "console", "ts": 5.0, "data": {"level": "error", "text": "boom"}},
        {"kind": "network_failure", "ts": 6.0, "data": {"url": "http://x/a", "method": "GET"}},
    ]
    w = TraceWriter(tmp_path)
    StepCapture(w, FakeClient([payload(events=events)])).capture(3)
    recs = [r for r in read(tmp_path).records() if isinstance(r, BrowserEvent)]
    assert [r.kind for r in recs] == ["console", "network_failure"]
    assert all(r.step == 3 for r in recs)
    assert recs[0].ts == 5.0
    assert recs[0].data["text"] == "boom"


def test_client_failure_degrades_to_partial_step(tmp_path: Path) -> None:
    w = TraceWriter(tmp_path)
    cap = StepCapture(w, FakeClient([ConnectionError("daemon gone")]))
    fields = cap.capture(4)  # must not raise
    assert fields == {"browser": {}, "screenshot": None, "dom_snapshot": None}
    anomalies = [a for a in read(tmp_path).records() if isinstance(a, Anomaly)]
    assert len(anomalies) == 1
    assert anomalies[0].kind == "capture_failed"
    assert anomalies[0].step == 4
    assert "daemon gone" in anomalies[0].message


def test_partial_payload_yields_partial_anomaly(tmp_path: Path) -> None:
    p = payload(
        screenshot_b64=None,
        dom_snapshot=None,
        errors={"snapshot": "native dialog blocking the page", "screenshot": "timed out"},
    )
    w = TraceWriter(tmp_path)
    fields = StepCapture(w, FakeClient([p])).capture(2)
    assert fields["screenshot"] is None and fields["dom_snapshot"] is None
    assert fields["browser"]["url"] == "http://x/"
    anomalies = [a for a in read(tmp_path).records() if isinstance(a, Anomaly)]
    assert len(anomalies) == 1
    assert anomalies[0].kind == "capture_partial"
    assert anomalies[0].fields["snapshot"] == "native dialog blocking the page"


def test_garbage_payload_is_contained(tmp_path: Path) -> None:
    w = TraceWriter(tmp_path)
    fields = StepCapture(w, FakeClient([{"browser": "not-a-dict", "events": [1, 2]}])).capture(1)
    assert fields["browser"] == {}
    fields = StepCapture(w, FakeClient([{"screenshot_b64": "%%%not-base64%%%"}])).capture(2)
    assert fields["screenshot"] is None
    anomalies = [a for a in read(tmp_path).records() if isinstance(a, Anomaly)]
    assert any(a.step == 2 for a in anomalies)  # bad screenshot flagged, not raised
