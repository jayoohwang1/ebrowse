"""Trace schema: round-trip, forward compatibility, blob store, validation."""

import json
import subprocess
import sys
from pathlib import Path

from ebrowse_evals.trace import (
    Anomaly,
    RunMeta,
    Step,
    TraceReader,
    TraceWriter,
    record_from_dict,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample-trace"


def test_record_round_trip():
    step = Step(
        step=3,
        ts=1.0,
        mono=2.0,
        command="ebrowse click @e1",
        tokens={"input": 10},
        screenshot="sha256:ab",
    )
    back = record_from_dict(step.to_dict())
    assert isinstance(back, Step)
    assert back.step == 3 and back.command == "ebrowse click @e1"
    assert back.screenshot == "sha256:ab" and back.tokens == {"input": 10}


def test_unknown_fields_and_types_survive():
    d = Step(step=1, ts=0.0, mono=0.0).to_dict()
    d["future_field"] = {"x": 1}
    back = record_from_dict(d)
    assert isinstance(back, Step) and back.extra["future_field"] == {"x": 1}
    assert back.to_dict()["future_field"] == {"x": 1}  # preserved on re-write
    assert record_from_dict({"type": "future_record_type"}) is None


def test_writer_reader_round_trip(tmp_path):
    w = TraceWriter(tmp_path / "run")
    ref = w.put_blob(b"payload", ".json")
    assert ref == w.put_blob(b"payload", ".json")  # content-addressed dedupe
    w.write(RunMeta(run_id="r1", task_id="t1"))
    w.write(Step(step=1, command="ebrowse outline", dom_snapshot=ref))
    w.write(Anomaly(step=1, kind="snapshot_truncated", message="cut at 50k nodes"))

    r = TraceReader(tmp_path / "run")
    assert r.validate() == []
    meta = r.meta()
    assert meta is not None and meta.run_id == "r1"
    assert [s.step for s in r.steps()] == [1]
    assert r.anomalies()[0].kind == "snapshot_truncated"
    assert len(r.for_step(1)) == 2
    assert r.blobs.get(ref) == b"payload"
    assert all(rec.ts is not None and rec.mono is not None for rec in r.records())


def test_reader_skips_garbage_tail(tmp_path):
    w = TraceWriter(tmp_path / "run")
    w.write(RunMeta(run_id="r1"))
    with (tmp_path / "run" / "events.jsonl").open("a") as f:
        f.write('{"type": "step", "step": 1')  # crashed mid-write
    r = TraceReader(tmp_path / "run")
    assert len(list(r.records())) == 1


def test_validate_catches_problems(tmp_path):
    w = TraceWriter(tmp_path / "run")
    w.write(Step(step=2, command="x"))  # no run_meta, first record isn't meta
    w.write(Step(step=1, command="y", screenshot="sha256:" + "0" * 64))
    problems = TraceReader(tmp_path / "run").validate()
    assert any("run_meta" in p for p in problems)
    assert any("increasing" in p for p in problems)
    assert any("missing blob" in p for p in problems)


def test_sample_fixture_is_valid_and_regenerable(tmp_path):
    r = TraceReader(SAMPLE)
    assert r.validate() == []
    assert len(r.steps()) == 3 and len(r.anomalies()) == 1
    end = r.end()
    assert end is not None and end.outcome == "success"
    # steps 1 and 2 share an unchanged-page screenshot blob (dedupe)
    s1, s2, _ = r.steps()
    assert s1.screenshot == s2.screenshot
    # dom snapshot blob is readable JSON
    assert s1.dom_snapshot is not None
    assert json.loads(r.blobs.get(s1.dom_snapshot))["sections"] == 4


def test_generator_matches_committed_fixture():
    """Regenerating produces byte-identical output (edit generator, not output)."""
    before = (SAMPLE / "events.jsonl").read_text()
    subprocess.run(
        [sys.executable, str(FIXTURES / "generate_trace.py")],
        check=True,
        capture_output=True,
    )
    assert (SAMPLE / "events.jsonl").read_text() == before
