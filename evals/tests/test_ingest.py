"""Post-run join of shim spool + debug log into the trace (ingest.py), and the
instrumented shim itself (harness._install_shim) executed against a stub."""

import base64
import json
import os
import subprocess
from pathlib import Path

from ebrowse_evals import ingest
from ebrowse_evals.harness import DEBUG_LOG_FILE, SPOOL_DIR, PiHarness
from ebrowse_evals.trace.records import Anomaly, BrowserEvent, EbrowseLog, RunMeta, Step
from ebrowse_evals.trace.store import TraceReader, TraceWriter


def _steps() -> list[Step]:
    return [
        Step(step=1, command="ebrowse open http://x/list.html"),
        Step(step=2, command="ls workdir"),  # non-ebrowse: no capture join
        Step(step=3, command="ebrowse click @e1 && echo done"),
    ]


def _spool(dirpath: Path, n: int, payload: dict) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{n}.json").write_text(json.dumps(payload))


def test_ebrowse_call_steps_matching():
    steps = _steps() + [
        Step(step=4, command="cat ebrowse.log"),  # word in an argument, not a call
        Step(step=5, command="/usr/local/bin/ebrowse outline"),
    ]
    calls = ingest.ebrowse_call_steps(steps)
    assert {n: s.step for n, s in calls.items()} == {1: 1, 2: 3, 3: 5}


def test_attach_spool_fills_steps_and_events(tmp_path):
    writer = TraceWriter(tmp_path / "run")
    steps = _steps()
    shot = base64.b64encode(b"png-bytes").decode()
    _spool(
        tmp_path / "spool",
        1,
        {
            "browser": {"url": "http://x/list.html", "title": "L"},
            "screenshot_b64": shot,
            "dom_snapshot": {"url": "http://x/list.html"},
            "events": [{"kind": "console", "ts": 5.0, "data": {"text": "warn"}}],
        },
    )
    _spool(tmp_path / "spool", 2, {"hook_error": "ConnectionRefusedError: daemon down"})
    ingest.attach_spool(writer, steps, tmp_path / "spool")

    assert steps[0].browser["url"] == "http://x/list.html"
    assert steps[0].screenshot and steps[0].dom_snapshot
    assert steps[1].screenshot is None  # non-ebrowse step untouched
    assert steps[2].screenshot is None  # call 2 spooled a hook_error
    events = [r for r in _written(tmp_path / "run") if isinstance(r, BrowserEvent)]
    assert len(events) == 1 and events[0].step == 1 and events[0].kind == "console"
    anomalies = [r for r in _written(tmp_path / "run") if isinstance(r, Anomaly)]
    assert [a.kind for a in anomalies] == ["capture_failed"]
    assert anomalies[0].step == 3


def test_attach_spool_count_mismatch_flagged(tmp_path):
    writer = TraceWriter(tmp_path / "run")
    steps = _steps()
    _spool(tmp_path / "spool", 1, {"browser": {}})
    _spool(tmp_path / "spool", 3, {"browser": {}})  # 3 spooled but only 2 ebrowse steps
    ingest.attach_spool(writer, steps, tmp_path / "spool")
    anomalies = [r for r in _written(tmp_path / "run") if isinstance(r, Anomaly)]
    assert anomalies and anomalies[0].kind == "join_mismatch" and anomalies[0].step is None


def test_attach_debug_log_joins_and_promotes(tmp_path):
    writer = TraceWriter(tmp_path / "run")
    steps = _steps()
    lines = [
        {
            "request_id": "call-1",
            "module": "snapshot",
            "event": "phase",
            "level": "info",
            "fields": {"phase": "capture", "dur_ms": 200.0},
            "ts": 1.0,
            "mono": 1.0,
        },
        {
            "request_id": "call-2",
            "module": "interaction",
            "event": "element_moved",
            "level": "warn",
            "fields": {"ref": "@e1", "dy": 48},
            "ts": 2.0,
            "mono": 2.0,
        },
        {
            "request_id": "someone-else",
            "module": "daemon",
            "event": "request_begin",
            "level": "info",
            "fields": {},
            "ts": 3.0,
            "mono": 3.0,
        },
    ]
    (tmp_path / "dbg.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    ingest.attach_debug_log(writer, steps, tmp_path / "dbg.jsonl")

    logs = [r for r in _written(tmp_path / "run") if isinstance(r, EbrowseLog)]
    assert [(r.step, r.event) for r in logs] == [
        (1, "phase"),
        (3, "element_moved"),
        (None, "request_begin"),
    ]
    anomalies = [r for r in _written(tmp_path / "run") if isinstance(r, Anomaly)]
    assert len(anomalies) == 1
    assert anomalies[0].kind == "element_moved" and anomalies[0].step == 3
    assert "@e1" in anomalies[0].message
    # phase timing rolled up onto the step (call-1 -> step 1)
    assert steps[0].timing == {"capture": 0.2}


def test_instrumented_shim_end_to_end(tmp_path):
    """Execute the generated shim against a stub target: counter increments,
    EBROWSE_REQUEST_ID is stamped per call, spool entries appear, NOHOOK skips."""
    stub = tmp_path / "stub-ebrowse"
    log = tmp_path / "calls.log"
    stub.write_text(f'#!/usr/bin/env bash\necho "$EBROWSE_REQUEST_ID $@" >> "{log}"\n')
    stub.chmod(0o755)
    h = PiHarness(provider="p", model="m", tool="ebrowse", capture=True, ebrowse_bin=str(stub))
    run_dir = tmp_path / "run"
    env: dict[str, str] = {"PATH": os.defpath}
    h._install_shim(run_dir, env)
    assert env["EBROWSE_DEBUG_LOG"] == str(run_dir / DEBUG_LOG_FILE)

    shim = run_dir / "bin" / "ebrowse"
    subprocess.run([str(shim), "open", "http://x"], env=env, check=True, timeout=30)
    subprocess.run([str(shim), "click", "@e1"], env=env, check=True, timeout=30)
    calls = log.read_text().splitlines()
    # setup's `daemon stop` ran with NOHOOK: no request id, no spool slot
    assert calls[0].split() == ["daemon", "stop"]
    assert calls[1].split() == ["call-1", "open", "http://x"]
    assert calls[2].split() == ["call-2", "click", "@e1"]
    spool = run_dir / SPOOL_DIR
    assert (spool / "seq").read_text().strip() == "2"
    # no daemon running -> the hook spools hook_error payloads, exit code still 0
    for n in (1, 2):
        payload = json.loads((spool / f"{n}.json").read_text())
        assert "hook_error" in payload or "browser" in payload


def _written(run_dir: Path):
    from ebrowse_evals.trace.records import record_from_dict

    lines = (run_dir / "events.jsonl").read_text().splitlines()
    return [r for r in (record_from_dict(json.loads(ln)) for ln in lines) if r is not None]


def test_run_task_ingests_shim_artifacts(tmp_path):
    """End-to-end through run_task with a fake harness that fabricates the
    shim artifacts the way an instrumented run leaves them."""
    from ebrowse_evals.harness import HarnessResult, ParsedStep
    from ebrowse_evals.runner import run_task
    from ebrowse_evals.tasks import Task

    class FakeInstrumentedHarness:
        def describe(self):
            return {"harness": "fake"}

        def run(self, prompt, workdir, env, timeout_s, run_dir):
            _spool(run_dir / SPOOL_DIR, 1, {"browser": {"url": "http://x"}})
            (run_dir / DEBUG_LOG_FILE).write_text(
                json.dumps(
                    {
                        "request_id": "call-1",
                        "module": "diff",
                        "event": "summary",
                        "level": "info",
                        "fields": {"changed": 1},
                        "ts": 1.0,
                        "mono": 1.0,
                    }
                )
                + "\n"
            )
            return HarnessResult(
                steps=[ParsedStep(command="ebrowse open http://x", output="ok")],
                final_answer="done",
            )

    task = Task(id="t", prompt="go")
    run_dir = tmp_path / "run"
    run_task(task, FakeInstrumentedHarness(), {"capture": True}, run_dir)
    reader = TraceReader(run_dir)
    assert reader.validate() == []
    step = reader.steps()[0]
    assert step.browser == {"url": "http://x"}
    logs = [r for r in reader.records() if isinstance(r, EbrowseLog)]
    assert len(logs) == 1 and logs[0].step == 1 and logs[0].module == "diff"
    meta = reader.meta()
    assert isinstance(meta, RunMeta)
