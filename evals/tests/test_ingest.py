"""Post-run join of browser-tool capture spool + debug log into the trace."""

import base64
import json
from pathlib import Path

from ebrowse_evals import ingest
from ebrowse_evals.harness import DEBUG_LOG_FILE, SPOOL_DIR
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


def test_ebrowse_call_steps_ignores_non_bash_tool_arguments():
    steps = [
        Step(step=1, tool_name="edit", command='{"newText": "example: ebrowse outline"}'),
        Step(step=2, tool_name="bash", command="ebrowse outline"),
    ]
    assert ingest.ebrowse_call_steps(steps) == {1: steps[1]}


def test_ebrowse_call_steps_custom_tool_skips_policy_blocks():
    steps = [
        Step(step=1, tool_name="ebrowse", command="ebrowse outline"),
        Step(
            step=2,
            tool_name="ebrowse",
            command="ebrowse eval 1+1",
            error={"class": "policy_block"},
        ),
        Step(step=3, tool_name="ebrowse", command="ebrowse click @e1"),
    ]
    assert ingest.ebrowse_call_steps(steps) == {1: steps[0], 2: steps[2]}


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

        def run(
            self,
            prompt,
            workdir,
            env,
            timeout_s,
            run_dir,
            start_url=None,
            tool_call_limit=None,
            config=None,
        ):
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
