"""Debug-event channel (src/ebrowse/debug.py): pure tests.

Covers: event shapes (the eval harness's ebrowse_log record), pipeline/diff
emission via fixture snapshots, anomaly events, JSONL sink, and — critically —
the instrumentation-OFF path: no recorder ⇒ no events, no file, byte-identical
outputs (golden tests in test_core_pure.py cover the rendering side).
"""

from __future__ import annotations

import json
from pathlib import Path

from ebrowse import debug
from ebrowse.config import ObserveConfig
from ebrowse.core.diff import diff_pages
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import DomSnapshot

SNAPSHOT_DIR = Path(__file__).parent / "fixtures" / "domsnapshots"

_REQUIRED_KEYS = {"request_id", "module", "event", "level", "fields", "ts", "mono"}


def _load_raw(name: str) -> dict:
    return json.loads((SNAPSHOT_DIR / f"{name}.json").read_text())


def _build(raw: dict, registry: RefRegistry):
    return build_page(DomSnapshot.from_dict(raw), registry, ObserveConfig(), captured_at=0.0)[0]


def _walk_dicts(node: dict):
    yield node
    for c in node.get("c", []) or []:
        yield from _walk_dicts(c)


# ------------------------------------------------------------------- off ----


def test_emit_is_noop_when_off():
    debug.emit("m", "e", foo=1)  # must not raise, must not require a recorder
    assert not debug.enabled()


def test_off_path_no_file_and_identical_output(tmp_path):
    registry_a, registry_b = RefRegistry(), RefRegistry()
    raw = _load_raw("form")
    plain = _build(raw, registry_a)
    with debug.recording("req1"):
        recorded = _build(raw, registry_b)
    # instrumentation must not change the built PageMem
    assert [s.fingerprint for s in plain.sections] == [s.fingerprint for s in recorded.sections]
    assert [s.content_hash for s in plain.sections] == [s.content_hash for s in recorded.sections]
    assert [e.ref for s in plain.sections for e in s.elements] == [
        e.ref for s in recorded.sections for e in s.elements
    ]
    # nothing on disk unless someone explicitly flushes a recorder
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------------------- shapes ----


def test_pipeline_events_and_shapes():
    raw = _load_raw("form")
    with debug.recording("req42") as rec:
        _build(raw, RefRegistry())
    events = [e.to_dict() for e in rec.events]
    assert events, "pipeline emitted no events"
    for ev in events:
        assert set(ev) == _REQUIRED_KEYS
        assert ev["request_id"] == "req42"
        assert ev["level"] in ("info", "warn")
        assert isinstance(ev["fields"], dict)
        json.dumps(ev)  # JSONL-serializable
    split = [e for e in events if e["module"] == "split" and e["event"] == "phase"]
    assert split and split[0]["fields"]["phase"] == "split"
    assert "dur_ms" in split[0]["fields"]
    refs = [e for e in events if e["module"] == "fingerprint" and e["event"] == "refs_assigned"]
    assert len(refs) == 1
    f = refs[0]["fields"]
    assert f["minted"] + f["reused"] == f["total"] and f["minted"] > 0 and f["reused"] == 0


def test_refs_reused_on_rebuild():
    raw = _load_raw("form")
    registry = RefRegistry()
    _build(raw, registry)
    with debug.recording("r2") as rec:
        _build(raw, registry)
    f = [e for e in rec.events if e.event == "refs_assigned"][0].fields
    assert f["minted"] == 0 and f["reused"] == f["total"] > 0


# ------------------------------------------------------------------- diff ----


def test_diff_verdicts_and_summary():
    registry = RefRegistry()
    raw = _load_raw("form")
    prev = _build(raw, registry)
    for n in _walk_dicts(raw["root"]):
        if n.get("a", {}).get("id") == "fullname":
            n["a"]["val"] = "Jayoo"
    new = _build(raw, registry)
    with debug.recording("rd") as rec:
        diff_pages(prev, new)
    verdicts = [e for e in rec.events if e.event == "section_verdict"]
    assert verdicts and all(e.module == "diff" for e in verdicts)
    assert any(e.fields["verdict"] == "changed" and e.fields["state_changes"] >= 1
               for e in verdicts)  # fmt: skip
    summary = [e for e in rec.events if e.event == "summary"][0].fields
    assert summary["matched"] == summary["changed"] + summary["unchanged"]
    assert summary["changed"] >= 1


def test_ref_gone_anomaly_on_removed_element():
    registry = RefRegistry()
    raw = _load_raw("form")
    prev = _build(raw, registry)
    for n in _walk_dicts(raw["root"]):
        kids = n.get("c")
        if kids:
            n["c"] = [c for c in kids if c.get("a", {}).get("id") != "submit-btn"]
    new = _build(raw, registry)
    with debug.recording("rg") as rec:
        diff_pages(prev, new)
    gone = [e for e in rec.events if e.event == "ref_gone"]
    assert gone and all(e.level == "warn" for e in gone)
    assert all(e.fields["ref"].startswith("@e") for e in gone)


def test_section_reshaped_anomaly():
    registry = RefRegistry()
    raw = _load_raw("article")
    prev = _build(raw, registry)
    # churn the fingerprint inputs (class) of every node while content stays put
    for n in _walk_dicts(raw["root"]):
        a = n.setdefault("a", {})
        a["cls"] = (a.get("cls", "") + " reshapedwrapper").strip()
    new = _build(raw, registry)
    assert [s.fingerprint for s in prev.sections] != [s.fingerprint for s in new.sections]
    with debug.recording("rs") as rec:
        diff_pages(prev, new)
    reshaped = [e for e in rec.events if e.event == "section_reshaped"]
    assert reshaped and all(e.level == "warn" and e.module == "diff" for e in reshaped)
    assert reshaped[0].fields["new_fingerprint"] != reshaped[0].fields["old_fingerprint"]


def test_no_events_without_recorder_in_diff():
    registry = RefRegistry()
    raw = _load_raw("form")
    prev = _build(raw, registry)
    new = _build(raw, registry)
    d = diff_pages(prev, new)  # off: must not raise, no recorder involved
    assert d.kind == "no_change"


# ------------------------------------------------------------------- sink ----


def test_write_jsonl_and_session_path(tmp_path):
    with debug.recording("abc") as rec:
        debug.emit("daemon", "request_begin", verb="outline", session="default")
        debug.emit("snapshot", "snapshot_truncated", level="warn", url="http://x")
    path = debug.resolve_log_path(str(tmp_path / "{session}.jsonl"), "default")
    assert path.name == "default.jsonl"
    debug.write_jsonl(path, rec.events)
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(lines) == 2
    assert set(lines[0]) == _REQUIRED_KEYS
    assert lines[0]["request_id"] == "abc"
    assert lines[1]["event"] in debug.ANOMALY_EVENTS and lines[1]["level"] == "warn"
    # appending a second request keeps prior lines
    debug.write_jsonl(path, rec.events)
    assert len(path.read_text().splitlines()) == 4


def test_write_jsonl_empty_creates_no_file(tmp_path):
    p = tmp_path / "dbg.jsonl"
    debug.write_jsonl(p, [])
    assert not p.exists()
