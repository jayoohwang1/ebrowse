"""Generates the committed sample trace at evals/tests/fixtures/sample-trace/.

Edit this generator, not the output:
    python evals/tests/fixtures/generate_trace.py

The sample models a short 3-step run (outline -> expand -> click) with an
anomaly, browser events, ebrowse internal logs, and a post-hoc summary --
one of everything, so viewer/inspection work can build against it without a
real run. Timestamps are fixed for byte-stable output.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ebrowse_evals.trace import (  # noqa: E402
    Anomaly,
    BrowserEvent,
    EbrowseLog,
    RunEnd,
    RunMeta,
    Step,
    Summary,
    TraceWriter,
)

T0 = 1752800000.0  # fixed wall clock; mono starts at 0


def main() -> None:
    out = Path(__file__).parent / "sample-trace"
    shutil.rmtree(out, ignore_errors=True)
    w = TraceWriter(out)

    def snap(url: str, n_sections: int) -> str:
        payload = {"url": url, "title": "Products", "sections": n_sections}
        return w.put_blob(json.dumps(payload).encode(), ".json")

    def shot(label: str) -> str:
        return w.put_blob(f"fake-png:{label}".encode(), ".png")

    w.write(
        RunMeta(
            ts=T0,
            mono=0.0,
            run_id="sample-001",
            task_id="list-count",
            prompt="Open http://127.0.0.1:8196/list.html and count the products.",
            benchmark="fixtures",
            config={"worktree": True, "fixture_server": "127.0.0.1:8196"},
            agent={"harness": "pi", "provider": "local", "model": "qwen-test"},
            git_sha="0000000000000000000000000000000000000000",
            git_dirty=False,
            ebrowse_version="0.1.0",
            ebrowse_mode="worktree",
        )
    )

    url = "http://127.0.0.1:8196/list.html"
    w.write(
        Step(
            step=1,
            ts=T0 + 2,
            mono=2.0,
            command=f"ebrowse open {url}",
            output="Products — 4 sections\ns1 nav …\ns2 list (24 items) …",
            exit_code=0,
            agent_text="I'll open the page and look at the outline.",
            tokens={"input": 1200, "output": 45, "context": 1245},
            latency_s=1.4,
            timing={"navigate": 0.6, "settle": 0.3, "snapshot": 0.2, "render": 0.05},
            browser={"url": url, "title": "Products", "tabs": 1, "scroll_y": 0},
            screenshot=shot("step1"),
            dom_snapshot=snap(url, 4),
            request_id="req-001",
        )
    )
    w.write(
        EbrowseLog(
            step=1,
            ts=T0 + 2,
            mono=2.0,
            request_id="req-001",
            module="split",
            event="page_split",
            level="debug",
            fields={"sections": 4, "nodes": 310, "budget_merges": 1},
        )
    )
    w.write(
        BrowserEvent(
            step=1,
            ts=T0 + 2.1,
            mono=2.1,
            kind="console",
            data={"level": "warning", "text": "third-party cookie blocked"},
        )
    )

    w.write(
        Step(
            step=2,
            ts=T0 + 6,
            mono=6.0,
            command="ebrowse expand s2",
            output="s2 list (24 items)\n- Widget A @e1\n- Widget B @e2 …",
            exit_code=0,
            tokens={"input": 1400, "output": 30, "context": 1620},
            latency_s=0.9,
            timing={"snapshot": 0.2, "render": 0.1},
            browser={"url": url, "title": "Products", "tabs": 1, "scroll_y": 0},
            screenshot=shot("step1"),  # unchanged page -> same blob, dedupe path
            dom_snapshot=snap(url, 4),
            request_id="req-002",
        )
    )
    w.write(
        EbrowseLog(
            step=2,
            ts=T0 + 6,
            mono=6.0,
            request_id="req-002",
            module="fingerprint",
            event="refs_assigned",
            level="info",
            fields={"minted": 24, "reused": 0},
        )
    )

    url3 = "http://127.0.0.1:8196/detail.html"
    w.write(
        Step(
            step=3,
            ts=T0 + 11,
            mono=11.0,
            command="ebrowse click @e1",
            output="clicked @e1 -> navigated\n+ s1 detail: Widget A …",
            exit_code=0,
            tokens={"input": 1700, "output": 25, "context": 1950},
            latency_s=2.1,
            timing={"locate": 0.1, "click": 0.05, "settle": 1.2, "snapshot": 0.3},
            browser={"url": url3, "title": "Widget A", "tabs": 1, "scroll_y": 0},
            screenshot=shot("step3"),
            dom_snapshot=snap(url3, 2),
            request_id="req-003",
        )
    )
    w.write(
        BrowserEvent(
            step=3,
            ts=T0 + 10.5,
            mono=10.5,
            kind="navigation",
            data={"from": url, "to": url3},
        )
    )
    w.write(
        EbrowseLog(
            step=3,
            ts=T0 + 10.4,
            mono=10.4,
            request_id="req-003",
            module="interaction",
            event="element_moved",
            level="warn",
            fields={"ref": "@e1", "dy": 48, "rescrolled": True},
        )
    )
    w.write(
        Anomaly(
            step=3,
            ts=T0 + 10.4,
            mono=10.4,
            kind="element_moved",
            message="@e1 moved 48px between snapshot and click; re-scrolled before clicking",
            fields={"ref": "@e1", "request_id": "req-003"},
        )
    )

    w.write(
        Summary(
            ts=T0 + 60,
            mono=60.0,
            step_start=1,
            step_end=3,
            text="Opened the product list, expanded it, clicked into Widget A.",
            model="qwen-test",
        )
    )
    w.write(
        RunEnd(
            ts=T0 + 15,
            mono=15.0,
            outcome="success",
            steps=3,
            totals={"tokens_output": 100, "peak_context": 1950, "anomalies": 1},
            eval={"success": True, "score": None, "details": {}},
        )
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
