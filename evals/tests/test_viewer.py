"""Viewer tests: render the committed sample trace and degraded variants."""

from __future__ import annotations

import json
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ebrowse_evals import viewer_server
from ebrowse_evals.cli import main
from ebrowse_evals.trace import AgentMessage, PromptSnapshot, TraceWriter
from ebrowse_evals.trace.records import Summary
from ebrowse_evals.trace.store import TraceReader
from ebrowse_evals.viewer import render_run
from ebrowse_evals.viewer_server import (
    _run_label,
    discover_runs,
    make_handler,
    render_index,
    trash_runs,
)

SAMPLE = Path(__file__).parent / "fixtures" / "sample-trace"


@pytest.fixture
def html() -> str:
    return render_run(SAMPLE)


def test_all_steps_and_lanes_present(html: str) -> None:
    for n in (1, 2, 3):
        assert f'id="step-{n}"' in html
    assert html.count('class="lane lane-left"') == 3
    assert html.count('class="lane lane-right"') == 3


def test_right_lane_shows_agent_view_verbatim(html: str) -> None:
    assert "ebrowse open http://127.0.0.1:8196/list.html" in html
    assert "s2 list (24 items)" in html  # tool output
    assert "I&#x27;ll open the page and look at the outline." in html  # agent text, escaped
    assert "input 1200" in html and "1.40s" in html  # tokens / latency


def test_header_metadata_outcome_and_anomalies(html: str) -> None:
    assert "list-count" in html
    assert "success" in html
    assert "anomalies (1)" in html
    assert 'href="#step-3"' in html  # anomaly links to its step
    assert "element_moved" in html
    assert "resolved config" in html


def test_header_leads_with_site_and_instruction(html: str) -> None:
    assert "<h1>Open http://127.0.0.1:8196/list.html and count the products.</h1>" in html
    assert '<span class="site">127.0.0.1:8196</span>' in html  # from the first step's url
    # the noisy blocks open on demand, not on load
    assert '<details class="anomaly-list"><summary>anomalies (1)</summary>' in html
    assert '<details class="facts"><summary>run details</summary>' in html


def test_left_lane_internals_sections(html: str) -> None:
    assert "internals" in html
    assert "browser events" in html
    assert "third-party cookie blocked" in html  # console event
    assert "ebrowse log" in html and "refs_assigned" in html
    assert "browser state" in html
    assert "DomSnapshot" in html
    assert 'class="log-debug"' in html  # debug rows exist (hidden by CSS default)
    assert "show-debug" in html  # toggle wiring


def test_summary_marker_and_timing_bar(html: str) -> None:
    assert "summary steps 1&ndash;3" in html
    assert "Opened the product list" in html
    assert 'class="tbar"' in html
    assert "navigate 0.60s" in html


def test_fake_png_blobs_get_placeholder_not_img(html: str) -> None:
    # Sample-trace .png blobs are fake text files; must render as placeholders.
    assert "placeholder" in html
    assert "data:image/png" not in html


def test_conversation_view_shows_all_blocks_and_fixed_browser_lane(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shutil.copytree(SAMPLE, run)
    writer = TraceWriter(run)
    writer.write(PromptSnapshot(kind="start", text="EXACT start <prompt>", sha256="start"))
    writer.write(PromptSnapshot(kind="system", text="full secret system", sha256="system"))
    writer.write(
        AgentMessage(
            sequence=1,
            message_id="u1",
            role="user",
            content=[{"type": "text", "text": "EXACT start <prompt>"}],
            is_start=True,
        )
    )
    writer.write(
        AgentMessage(
            sequence=2,
            message_id="a1",
            role="assistant",
            turn=1,
            content=[
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "ordinary assistant output"},
                {
                    "type": "toolCall",
                    "id": "read-1",
                    "name": "read",
                    "arguments": {"path": "notes.txt"},
                },
            ],
        )
    )
    writer.write(
        AgentMessage(
            sequence=3,
            message_id="r1",
            role="toolResult",
            content=[{"type": "text", "text": "non-browser tool output"}],
            tool_call_id="read-1",
            tool_name="read",
        )
    )
    page = render_run(run)
    assert "EXACT start &lt;prompt&gt;" in page
    assert "full secret system" in page
    assert '<details class="system-prompt">' in page  # collapsed by default
    assert "private reasoning" in page
    assert "ordinary assistant output" in page
    assert "non-browser tool output" in page
    assert "notes.txt" in page
    assert page.count('class="browser-side browser-empty"') >= 3
    assert "grid-template-columns:minmax(0,1fr) 340px" in page


def test_tolerates_torn_tail_missing_blobs_unknown_types(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shutil.copytree(SAMPLE, run)
    shutil.rmtree(run / "blobs")
    with (run / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "hologram", "step": 2, "weird": [1, 2]}) + "\n")
        f.write('{"type": "step", "step": 4, "trunc')  # torn tail
    html = render_run(run)
    assert "missing blob" in html
    assert "hologram" in html  # unknown record surfaced under "other records"
    assert 'id="step-3"' in html


def test_cli_view_writes_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out.html"
    assert main(["view", str(SAMPLE), "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert str(out) in capsys.readouterr().out


def test_cli_view_missing_dir(tmp_path: Path) -> None:
    assert main(["view", str(tmp_path / "nope")]) == 2


def test_unannotated_trace_is_not_segmented(html: str) -> None:
    assert 'class="segment"' not in html and 'class="overview"' not in html
    assert "summary steps 1&ndash;3" in html  # plain range summary keeps its marker


def test_annotation_spans_segment_the_trajectory(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shutil.copytree(SAMPLE, run)
    writer = TraceWriter(run)
    writer.write(Summary(step_start=1, step_end=3, kind="verdict", text="Counted the products."))
    writer.write(
        Summary(
            step_start=2,
            step_end=2,
            kind="issue",
            category="tool_bug",
            severity="high",
            text="expand returned a stale section",
        )
    )
    writer.write(Summary(step_start=2, step_end=3, kind="vision", text="the filter chip is hidden"))
    page = render_run(run)

    assert "Counted the products." in page  # verdict heads the overview
    assert "Expand all" in page and "Collapse all" in page
    # edges at 2 and 3 -> [1,1] [2,2] [3,3]; the vision span reaches into two
    assert [int(n) for n in re.findall(r'id="segment-(\d+)"', page)] == [1, 2, 3]
    assert page.count("no findings") == 1  # only step 1 is unannotated
    assert "tool_bug" in page and "expand returned a stale section" in page
    # a span states itself once, then thins to a marker where it continues
    assert page.count("the filter chip is hidden") == 1
    assert page.count(">continues</span>") == 1

    chunks = re.split(r'<details class="segment" id="segment-\d+">', page)[1:]
    assert [re.findall(r'id="step-(\d+)"', chunk) for chunk in chunks] == [["1"], ["2"], ["3"]]
    # plain (kind-less) summaries still render inline, inside their segment
    assert "summary steps 1&ndash;3" in page


def test_central_index_discovers_and_groups_nested_runs(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    shutil.copytree(SAMPLE, root / "batch-a" / "run-one")
    shutil.copytree(SAMPLE, root / "batch-b" / "nested" / "run-two")
    runs = discover_runs(root)
    assert [r.group for r in runs] == ["batch-a", "batch-b/nested"]
    page = render_index(root, runs)
    assert "2 runs" in page
    assert "batch-a" in page and "batch-b/nested" in page
    assert "/run/batch-a/run-one" in page
    assert "success" in page
    assert 'action="/trash"' in page
    assert page.count('class="group-pick"') == 2
    assert "Move selected to trash" in page


def test_index_task_cell_shows_instruction_site_and_verdict(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = root / "batch" / "run-one"
    shutil.copytree(SAMPLE, run)
    TraceWriter(run).write(
        Summary(step_start=1, step_end=3, kind="verdict", text="Counted 24 products and answered.")
    )
    (summary,) = discover_runs(root)
    assert summary.prompt.startswith("Open http://127.0.0.1:8196/list.html")
    assert summary.website == "127.0.0.1:8196"  # no bootstrap url: taken from step 1
    assert summary.verdict == "Counted 24 products and answered."
    assert summary.steps == 3 and summary.anomalies == 1

    page = render_index(root, [summary])
    assert "count the products." in page  # instruction line, not the task id
    assert "127.0.0.1:8196" in page and "sample-001" in page  # site &middot; run label
    assert _run_label("qwen-hard-abc123", "abc123") == "qwen-hard"  # task id not repeated
    assert 'class="verdict"' in page and "Counted 24 products" in page
    assert ">Task</th>" in page and ">Summary</th>" in page
    assert ">Run</th>" not in page


def test_central_server_routes_index_trace_and_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    shutil.copytree(SAMPLE, root / "batch" / "run")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert "ebrowse eval runs" in urllib.request.urlopen(base + "/").read().decode()
        trace = urllib.request.urlopen(base + "/run/batch/run").read().decode()
        assert "all runs" in trace and 'id="step-1"' in trace
        assert "data:image/png" not in trace
        step = TraceReader(root / "batch" / "run").steps()[0]
        assert step.dom_snapshot is not None
        blob_query = urllib.parse.urlencode({"run": "batch/run", "ref": step.dom_snapshot})
        assert urllib.request.urlopen(base + "/blob?" + blob_query).read().startswith(b"{")
        fragment = (
            urllib.request.urlopen(base + "/steps?run=batch%2Frun&offset=0&limit=2").read().decode()
        )
        assert fragment.count('class="step"') == 2
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/run/%2E%2E/nope")
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_server_mode_compacts_middle_and_lazy_loads_chunks(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shutil.copytree(SAMPLE, run)
    records = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    template = next(record for record in records if record.get("type") == "step")
    kept = [record for record in records if record.get("type") not in ("step", "run_end")]
    for n in range(1, 51):
        step = dict(template)
        step["step"] = n
        step["command"] = f"ebrowse action {n}"
        step["agent_text"] = f"Thought for step {n}"
        kept.append(step)
    (run / "events.jsonl").write_text("\n".join(json.dumps(record) for record in kept) + "\n")

    page = render_run(
        run,
        blob_url=lambda ref: f"/blob/{ref}",
        fragment_url=lambda offset, limit: f"/steps/{offset}/{limit}",
        compact_middle=True,
    )
    assert page.count('class="step"') == 35
    assert page.count('class="compact-step"') == 15
    assert "Expand steps 26–35" in page and "Expand steps 36–40" in page
    assert 'id="step-25"' in page and 'id="step-41"' in page
    assert 'id="step-26"' not in page
    assert "data:image/png" not in page
    assert 'class="dom-lazy"' in page

    fragment = viewer_server.render_step_fragment(run, 25, 10, blob_url=lambda ref: f"/blob/{ref}")
    assert fragment.count('class="step"') == 10
    assert 'id="step-26"' in fragment and 'id="step-35"' in fragment


def test_trash_runs_validates_and_invokes_trash_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    shutil.copytree(SAMPLE, root / "batch" / "run")
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        return Result()

    monkeypatch.setattr(viewer_server.subprocess, "run", fake_run)
    assert trash_runs(root, ["batch/run", "batch/run"]) == 1
    assert calls == [["trash-put", "--", str((root / "batch" / "run").resolve())]]
    with pytest.raises(ValueError, match="invalid trace run"):
        trash_runs(root, ["../outside"])


def test_central_server_trash_post_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    shutil.copytree(SAMPLE, root / "batch" / "run")
    selected: list[str] = []

    def fake_trash(root_arg: Path, values: list[str]) -> int:
        assert root_arg == root.resolve()
        selected.extend(values)
        return len(values)

    monkeypatch.setattr(viewer_server, "trash_runs", fake_trash)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data = urllib.parse.urlencode({"run": "batch/run"}).encode()
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        page = (
            opener.open(
                urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/trash", data=data, method="POST"
                )
            )
            .read()
            .decode()
        )
        assert selected == ["batch/run"]
        assert "Moved 1 run to trash" in page
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
def test_central_server_ignores_client_disconnects(tmp_path: Path, error: type[OSError]) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    handler_type = make_handler(root)
    handler = object.__new__(handler_type)
    handler.send_response = lambda status: None  # type: ignore[method-assign]
    handler.send_header = lambda key, value: None  # type: ignore[method-assign]
    handler.end_headers = lambda: (_ for _ in ()).throw(error())  # type: ignore[method-assign]
    handler._send(b"page", "text/html")
