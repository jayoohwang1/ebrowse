"""Viewer tests: render the committed sample trace and degraded variants."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ebrowse_evals.cli import main
from ebrowse_evals.viewer import render_run

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
