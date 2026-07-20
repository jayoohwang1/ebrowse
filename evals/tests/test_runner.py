"""Runner: selection, config layering, trace emission, evaluator invocation.

Pure tests — a fake harness stands in for pi; no network, no browser.
"""

import json
import time
from pathlib import Path

from ebrowse_evals.harness import (
    PI_EVENTS_FILE,
    HarnessResult,
    ParsedMessage,
    ParsedStep,
    PiHarness,
    parse_pi_session,
)
from ebrowse_evals.runner import (
    HARNESS_DEFAULTS,
    resolve_config,
    run_task,
    run_tasks,
    select_tasks,
)
from ebrowse_evals.tasks import load_benchmark, load_task
from ebrowse_evals.trace.records import AgentMessage, PromptSnapshot, Step
from ebrowse_evals.trace.store import TraceReader, TraceWriter

FIXTURES = Path(__file__).parent / "fixtures"
BENCH = FIXTURES / "benchmark"
SAMPLE_SESSION = Path(__file__).parents[2] / "experiments" / "fixtures" / "sample-pi-session.jsonl"


class FakeHarness:
    """Deterministic AgentHarness: canned steps + final answer."""

    def __init__(self, result: HarnessResult | None = None):
        self.result = result or HarnessResult(
            steps=[
                ParsedStep(
                    command="ebrowse open https://example.com",
                    output="PAGE Example Domain",
                    tokens={"input": 100, "output": 20},
                    latency_s=1.0,
                ),
                ParsedStep(command="ebrowse expand s1", output="32 products", is_error=False),
            ],
            final_answer="There are 32 products.",
            totals={"output_tokens": 30, "input_tokens": 250, "peak_context": 280},
        )
        self.calls: list[dict] = []

    def describe(self):
        return {"harness": "fake", "provider": "test", "model": "test-model"}

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
        self.calls.append(
            {
                "prompt": prompt,
                "workdir": workdir,
                "timeout_s": timeout_s,
                "start_url": start_url,
                "tool_call_limit": tool_call_limit,
                "config": config,
            }
        )
        return self.result


# -- selection ---------------------------------------------------------------


def test_select_tasks_sample_is_seeded():
    bench = load_benchmark(BENCH)
    a = select_tasks(bench, sample=1, seed=7)
    b = select_tasks(bench, sample=1, seed=7)
    assert [t.id for t in a] == [t.id for t in b]
    assert len(a) == 1
    assert select_tasks(bench, sample=10) == bench.tasks  # sample > n is a no-op


def test_select_tasks_patterns_and_tags():
    bench = load_benchmark(BENCH)
    assert [t.id for t in select_tasks(bench, patterns=["list-*"])] == ["list-count"]
    assert [t.id for t in select_tasks(bench, tags=["process"])] == ["custom-eval"]


# -- config layering ---------------------------------------------------------


def test_resolve_config_layering():
    resolved = resolve_config(
        {"fixture_server": "x", "timeout_s": 100},  # benchmark
        {"timeout_s": 50, "tool": "none"},  # task
        {"tool": "ebrowse", "worktree": True, "timeout_s": None},  # CLI (None = unset)
    )
    assert resolved["timeout_s"] == 50  # task beat benchmark; CLI None didn't override
    assert resolved["tool"] == "ebrowse"  # CLI beat task
    assert resolved["worktree"] is True
    assert resolved["fixture_server"] == "x"
    assert resolve_config() == HARNESS_DEFAULTS


# -- trace emission ----------------------------------------------------------


def test_run_task_emits_valid_trace(tmp_path):
    task = load_task(BENCH / "list-count")
    harness = FakeHarness()
    run_dir = tmp_path / "run1"
    result = run_task(
        task, harness, resolve_config({"worktree": True}), run_dir, benchmark="fixtures"
    )
    reader = TraceReader(run_dir)
    assert reader.validate() == []
    meta = reader.meta()
    assert meta is not None
    assert meta.task_id == "list-count"
    assert meta.benchmark == "fixtures"
    assert meta.config["worktree"] is True
    assert meta.ebrowse_mode == "worktree"
    assert meta.agent["harness"] == "fake"
    assert meta.git_sha  # runs inside the repo
    steps = reader.steps()
    assert [s.step for s in steps] == [1, 2]
    assert steps[0].command == "ebrowse open https://example.com"
    assert steps[0].output == "PAGE Example Domain"
    assert steps[0].tokens == {"input": 100, "output": 20}
    assert steps[0].screenshot is None and steps[0].dom_snapshot is None  # capture layer's job
    end = reader.end()
    assert end is not None
    assert end.outcome == "success"  # default_eval: "32" in final answer
    assert end.steps == 2
    assert end.totals["output_tokens"] == 30
    assert end.eval == {"success": True, "score": None, "details": {}}
    assert result.outcome == "success"
    # task-level timeout (120) beat the harness default
    assert harness.calls[0]["timeout_s"] == 120
    assert harness.calls[0]["start_url"] == "http://127.0.0.1:8196/list.html"
    assert harness.calls[0]["tool_call_limit"] == 200


def test_pi_ebrowse_preamble_adapts_cli_skill_to_custom_tool(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        "`ebrowse` is a CLI. Run it via shell. One background daemon owns the browser;\n"
        "state (page, refs, logins) persists between commands.\n"
        "Run `ebrowse outline` next.\n"
    )
    preamble = PiHarness(
        provider="p", model="m", tool="ebrowse", repo_root=tmp_path
    ).tool_preamble()
    assert "using the `ebrowse` tool" in preamble
    assert "available through the dedicated `ebrowse` tool" in preamble
    assert "without the leading `ebrowse` prefix" in preamble
    assert "Run it via shell" not in preamble
    assert "Run `ebrowse outline` next" in preamble  # examples remain recognizable


def test_run_meta_is_written_after_prepare_but_before_agent_run(tmp_path):
    class PreparingHarness(FakeHarness):
        def prepare_run(self, env, run_dir, start_url, config):
            config["resolved_navigation_domains"] = ["example.ca"]

        def run(self, prompt, workdir, env, timeout_s, run_dir, **kwargs):
            raw = list(TraceReader(run_dir).raw())
            assert len(raw) == 1
            assert raw[0]["type"] == "run_meta"
            assert raw[0]["config"]["resolved_navigation_domains"] == ["example.ca"]
            return super().run(prompt, workdir, env, timeout_s, run_dir, **kwargs)

    task = load_task(BENCH / "list-count")
    run_dir = tmp_path / "prepared"
    run_task(task, PreparingHarness(), resolve_config(), run_dir)
    assert TraceReader(run_dir).validate() == []


def test_run_task_outcomes(tmp_path):
    task = load_task(BENCH / "list-count")
    wrong = FakeHarness(HarnessResult(final_answer="no idea"))
    assert run_task(task, wrong, {}, tmp_path / "a").outcome == "failure"
    crashed = FakeHarness(HarnessResult(exit_code=1))
    assert run_task(task, crashed, {}, tmp_path / "b").outcome == "error"
    hung = FakeHarness(HarnessResult(timed_out=True, exit_code=-1))
    assert run_task(task, hung, {}, tmp_path / "c").outcome == "timeout"
    capped = FakeHarness(HarnessResult(tool_limit_hit=True, exit_code=-15))
    assert run_task(task, capped, {}, tmp_path / "d").outcome == "tool_limit"


def test_run_task_persists_structured_policy_block(tmp_path):
    task = load_task(BENCH / "list-count")
    harness = FakeHarness(
        HarnessResult(
            steps=[
                ParsedStep(
                    command="ebrowse eval document.title",
                    output="blocked",
                    is_error=True,
                    tool_name="ebrowse",
                    details={
                        "error_class": "policy_block",
                        "verb": "eval",
                        "reason": "not enabled",
                    },
                )
            ]
        )
    )
    run_task(task, harness, {}, tmp_path / "policy")
    step = TraceReader(tmp_path / "policy").steps()[0]
    assert step.error == {
        "class": "policy_block",
        "verb": "eval",
        "reason": "not enabled",
    }


def test_run_task_invokes_custom_evaluator_on_trace(tmp_path):
    task = load_task(BENCH / "custom-eval")
    harness = FakeHarness(
        HarnessResult(
            steps=[ParsedStep(command="ebrowse open detail.html", output="ok")],
            final_answer="opened",
        )
    )

    # custom-eval's eval.py checks step.browser urls; enrich via the capture hook
    class Capture:
        def on_step(self, writer: TraceWriter, step: Step) -> None:
            step.browser = {"url": "http://127.0.0.1:8196/detail.html"}

    result = run_task(task, harness, {}, tmp_path / "r", capture=Capture())
    assert result.outcome == "success"
    assert result.eval.details == {"steps": 1, "errors": 0}
    reader = TraceReader(tmp_path / "r")
    assert reader.validate() == []
    assert reader.steps()[0].browser["url"].endswith("detail.html")


def test_run_task_survives_broken_evaluator(tmp_path):
    task = load_task(BENCH / "custom-eval")
    task.path = tmp_path / "task"
    task.path.mkdir()
    (task.path / "eval.py").write_text("def evaluate(trace):\n    raise RuntimeError('boom')\n")
    result = run_task(task, FakeHarness(), {}, tmp_path / "r")
    assert result.outcome == "unknown"
    assert "boom" in result.eval.details["evaluator_error"]
    assert TraceReader(tmp_path / "r").validate() == []


def test_run_tasks_layers_benchmark_and_task_config(tmp_path):
    bench = load_benchmark(BENCH)
    harness = FakeHarness()
    results = run_tasks(bench.tasks, harness, bench, {"tool": "none"}, tmp_path)
    assert len(results) == 2
    for r in results:
        meta = TraceReader(r.run_dir).meta()
        assert meta is not None
        assert meta.config["fixture_server"] == "127.0.0.1:8196"  # from benchmark [config]
        assert meta.config["tool"] == "none"  # CLI layer
        assert meta.benchmark == "fixtures"


def test_run_tasks_can_run_in_parallel_and_preserves_order(tmp_path):
    bench = load_benchmark(BENCH)

    class SlowHarness(FakeHarness):
        def run(self, *args, **kwargs):
            time.sleep(0.2)
            return super().run(*args, **kwargs)

    begun = time.monotonic()
    results = run_tasks(bench.tasks, SlowHarness(), bench, {"jobs": 2}, tmp_path)
    elapsed = time.monotonic() - begun
    assert elapsed < 0.38
    assert [r.task_id for r in results] == [t.id for t in bench.tasks]
    assert len({r.run_dir for r in results}) == 2


def test_pi_harness_stops_at_tool_call_limit(tmp_path):
    pi = tmp_path / "fake-pi"
    pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        "for i in range(10):\n"
        " print(json.dumps({'type':'message_end','message':{'role':'assistant',"
        "'content':[{'type':'toolCall','id':str(i),'arguments':{'command':'echo x'}}]}}),"
        " flush=True)\n"
        " print(json.dumps({'type':'message_end','message':{'role':'toolResult',"
        "'toolCallId':str(i),'content':[{'type':'text','text':'x'}]}}), flush=True)\n"
        " time.sleep(.05)\n"
    )
    pi.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = PiHarness(provider="p", model="m", pi_bin=str(pi)).run(
        "go", run_dir / "work", {}, 10, run_dir, tool_call_limit=2
    )
    assert result.tool_limit_hit is True
    assert result.timed_out is False
    assert len(result.steps) == 2


def test_pi_browser_harness_loads_only_custom_ebrowse_tool(tmp_path):
    argv_file = tmp_path / "argv.json"
    pi = tmp_path / "fake-pi"
    pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(argv_file)!r}).write_text(json.dumps(sys.argv))\n"
    )
    pi.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ebrowse = tmp_path / "fake-ebrowse"
    ebrowse.write_text("#!/bin/sh\nexit 0\n")
    ebrowse.chmod(0o755)
    PiHarness(
        provider="p", model="m", tool="ebrowse", pi_bin=str(pi), ebrowse_bin=str(ebrowse)
    ).run(
        "go",
        run_dir / "work",
        {},
        10,
        run_dir,
        config={"navigation_policy": "unrestricted"},
    )
    argv = json.loads(argv_file.read_text())
    tools_at = argv.index("--tools")
    assert argv[tools_at + 1] == "ebrowse"
    assert "--no-builtin-tools" in argv
    assert "--no-extensions" in argv
    assert "--no-skills" in argv
    assert "--no-prompt-templates" in argv
    assert "--no-context-files" in argv
    assert "bash" not in argv and "edit" not in argv and "write" not in argv


def test_pi_harness_filters_cumulative_updates_and_keeps_fallback(tmp_path):
    pi = tmp_path / "fake-pi"
    pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "for i in range(500):\n"
        " print(json.dumps({'type':'message_update','message':{'role':'assistant',"
        "'content':'x'*(i+1)}}), flush=True)\n"
        "print(json.dumps({'type':'message_end','message':{'role':'assistant','content':["
        "{'type':'toolCall','id':'1','arguments':{'command':'echo bounded'}}],"
        "'usage':{'input':2,'output':3,'totalTokens':5}}}), flush=True)\n"
        "print(json.dumps({'type':'message_end','message':{'role':'toolResult',"
        "'toolCallId':'1','content':[{'type':'text','text':'bounded'}]}}), flush=True)\n"
    )
    pi.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = PiHarness(provider="p", model="m", pi_bin=str(pi)).run(
        "go", run_dir / "work", {}, 10, run_dir
    )
    saved = (run_dir / PI_EVENTS_FILE).read_text()
    assert "message_update" not in saved
    assert saved.count('"type": "message_end"') == 2
    assert len(saved) < 1_000
    assert [step.command for step in result.steps] == ["echo bounded"]
    assert result.steps[0].output == "bounded"


def test_pi_harness_caps_event_file_but_preserves_in_memory_recovery(tmp_path):
    pi = tmp_path / "fake-pi"
    pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "for i in range(20): print(json.dumps({'type':'diagnostic','data':'x'*100}), flush=True)\n"
        "print(json.dumps({'type':'message_end','message':{'role':'assistant','content':["
        "{'type':'toolCall','id':'1','arguments':{'command':'echo recovered'}}]}}), flush=True)\n"
        "print(json.dumps({'type':'message_end','message':{'role':'toolResult',"
        "'toolCallId':'1','content':[{'type':'text','text':'recovered'}]}}), flush=True)\n"
    )
    pi.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = PiHarness(provider="p", model="m", pi_bin=str(pi), pi_events_max_bytes=512).run(
        "go", run_dir / "work", {}, 10, run_dir
    )
    saved = (run_dir / PI_EVENTS_FILE).read_text()
    assert (run_dir / PI_EVENTS_FILE).stat().st_size <= 512
    assert saved.count('"type":"events_truncated"') == 1
    assert [step.command for step in result.steps] == ["echo recovered"]
    assert result.steps[0].output == "recovered"


def test_pi_harness_timeout_uses_in_memory_message_end_fallback(tmp_path):
    pi = tmp_path / "fake-pi"
    pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        "print(json.dumps({'type':'message_update','message':{'content':'partial'}}), flush=True)\n"
        "print(json.dumps({'type':'message_end','message':{'role':'assistant','content':["
        "{'type':'toolCall','id':'1','arguments':{'command':'echo before-timeout'}}]}}), flush=True)\n"
        "print(json.dumps({'type':'message_end','message':{'role':'toolResult',"
        "'toolCallId':'1','content':[{'type':'text','text':'done'}]}}), flush=True)\n"
        "time.sleep(5)\n"
    )
    pi.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = PiHarness(provider="p", model="m", pi_bin=str(pi)).run(
        "go", run_dir / "work", {}, 0.2, run_dir
    )
    assert result.timed_out is True
    assert [step.command for step in result.steps] == ["echo before-timeout"]
    assert result.steps[0].output == "done"
    assert "message_update" not in (run_dir / PI_EVENTS_FILE).read_text()


# -- pi session parsing ------------------------------------------------------


def test_parse_pi_session_sample_fixture():
    entries = [json.loads(line) for line in SAMPLE_SESSION.read_text().splitlines() if line.strip()]
    result = parse_pi_session(entries)
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.command == "ebrowse open https://example.com"
    assert step.output.startswith("PAGE Example Domain")
    assert step.is_error is False
    assert step.agent_text == "I will open the page."
    assert step.tokens == {"input": 100, "output": 20, "totalTokens": 120}
    assert step.latency_s == 1.0  # 00:00:02 -> 00:00:03
    assert result.final_answer == "The page title is Example Domain."
    assert result.totals == {
        "turns": 2,
        "tool_calls": 1,
        "output_tokens": 30,
        "input_tokens": 250,
        "peak_context": 160,
    }
    assert [message.role for message in result.messages] == [
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ]
    assert result.messages[0].is_start is True
    assert result.messages[1].content[0]["type"] == "thinking"
    assert step.tool_call_id == "tool-1"
    assert step.tool_name == "bash"
    assert result.messages[2].tool_call_id == "tool-1"


def test_parse_custom_ebrowse_call_and_policy_details():
    entries = [
        {
            "type": "message",
            "id": "a1",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "c1",
                        "name": "ebrowse",
                        "arguments": {"command": "eval document.title"},
                    }
                ],
            },
        },
        {
            "type": "message",
            "id": "r1",
            "message": {
                "role": "toolResult",
                "toolCallId": "c1",
                "toolName": "ebrowse",
                "content": [{"type": "text", "text": "blocked"}],
                "isError": True,
                "details": {
                    "error_class": "policy_block",
                    "verb": "eval",
                    "reason": "not allowed",
                },
            },
        },
    ]
    result = parse_pi_session(entries)
    assert result.steps[0].command == "ebrowse eval document.title"
    assert result.steps[0].details["error_class"] == "policy_block"


def test_run_task_persists_prompts_messages_and_step_links(tmp_path):
    task = load_task(BENCH / "list-count")
    harness = FakeHarness(
        HarnessResult(
            start_prompt="exact starting prompt",
            system_prompts=["effective system prompt"],
            messages=[
                ParsedMessage(
                    1,
                    "u1",
                    None,
                    "user",
                    [{"type": "text", "text": "exact starting prompt"}],
                    is_start=True,
                ),
                ParsedMessage(
                    2,
                    "a1",
                    "u1",
                    "assistant",
                    [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {"command": "ebrowse outline"},
                        }
                    ],
                    turn=1,
                ),
                ParsedMessage(
                    3,
                    "r1",
                    "a1",
                    "toolResult",
                    [{"type": "text", "text": "PAGE Example"}],
                    turn=1,
                    tool_call_id="call-1",
                    tool_name="bash",
                ),
            ],
            steps=[
                ParsedStep(
                    command="ebrowse outline",
                    output="PAGE Example",
                    message_id="a1",
                    tool_call_id="call-1",
                    tool_name="bash",
                    call_index=1,
                )
            ],
            final_answer="32 products",
        )
    )
    run_task(task, harness, {}, tmp_path / "run")
    records = list(TraceReader(tmp_path / "run").records())
    prompts = [record for record in records if isinstance(record, PromptSnapshot)]
    messages = [record for record in records if isinstance(record, AgentMessage)]
    step = next(record for record in records if isinstance(record, Step))
    assert [(prompt.kind, prompt.text) for prompt in prompts] == [
        ("start", "exact starting prompt"),
        ("system", "effective system prompt"),
    ]
    assert [message.message_id for message in messages] == ["u1", "a1", "r1"]
    assert step.message_id == "a1"
    assert step.tool_call_id == "call-1"
