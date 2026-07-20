"""Browser-only Pi tool policy and shell-free launcher."""

import json
import sys
from pathlib import Path

import pytest

from ebrowse_evals.harness import _navigation_urls, resolve_navigation_domains
from ebrowse_evals.pi_tool import (
    DEFAULT_ALLOWED_VERBS,
    PolicyBlock,
    ToolPolicy,
    execute,
    parse_command,
    validate_args,
)


def _policy(tmp_path: Path, **overrides) -> ToolPolicy:
    run_dir = tmp_path / "run"
    (run_dir / "workdir").mkdir(parents=True)
    values = {
        "executable": sys.executable,
        "argv_prefix": [],
        "run_dir": run_dir,
        "allowed_verbs": frozenset(DEFAULT_ALLOWED_VERBS),
        "allowed_domains": ("example.com",),
        "timeout_s": 5.0,
        "max_args_bytes": 16_384,
        "max_output_bytes": 262_144,
        "capture": False,
    }
    values.update(overrides)
    return ToolPolicy(**values)


@pytest.mark.parametrize("verb", ["eval", "upload", "connect", "daemon", "doctor", "mcp"])
def test_standard_policy_blocks_non_browser_or_escape_verbs(tmp_path, verb):
    with pytest.raises(PolicyBlock, match="not enabled"):
        validate_args([verb, "anything"], _policy(tmp_path))


def test_policy_validates_navigation_and_output_paths(tmp_path):
    policy = _policy(tmp_path)
    validate_args(["open", "https://shop.example.com/path"], policy)
    with pytest.raises(PolicyBlock, match="outside the task"):
        validate_args(["open", "https://evil.test/"], policy)
    with pytest.raises(PolicyBlock, match="only http"):
        validate_args(["open", "file:///etc/passwd"], policy)
    with pytest.raises(PolicyBlock, match="credentials"):
        validate_args(["open", "https://user:pass@example.com/"], policy)
    with pytest.raises(PolicyBlock, match="output paths"):
        validate_args(["screenshot", "--output", "/tmp/shot.png"], policy)
    with pytest.raises(PolicyBlock, match="global"):
        validate_args(["--session", "other", "outline"], policy)


def test_launcher_passes_literal_argv_without_shell_and_numbers_calls(tmp_path, monkeypatch):
    log = tmp_path / "argv.jsonl"
    target = tmp_path / "target.py"
    target.write_text(
        "import json, os, pathlib, sys\n"
        "p = pathlib.Path(os.environ['TEST_ARGV_LOG'])\n"
        "with p.open('a') as f: f.write(json.dumps([os.environ.get('EBROWSE_REQUEST_ID'), *sys.argv[1:]]) + '\\n')\n"
        "print('ok')\n"
    )
    monkeypatch.setenv("TEST_ARGV_LOG", str(log))
    policy = _policy(
        tmp_path,
        executable=sys.executable,
        argv_prefix=[str(target)],
        allowed_domains=(),
    )
    first = execute(["fill", "@e1", "; touch /tmp/should-not-run"], policy)
    second = execute(["outline"], policy)
    assert first["ok"] is True and second["ok"] is True
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [
        ["call-1", "fill", "@e1", "; touch /tmp/should-not-run"],
        ["call-2", "outline"],
    ]


def test_command_string_is_only_tokenized_not_executed():
    assert parse_command('fill @e1 "; touch /tmp/should-not-run"', 1024) == [
        "fill",
        "@e1",
        "; touch /tmp/should-not-run",
    ]


def test_policy_block_does_not_consume_capture_number(tmp_path):
    policy = _policy(tmp_path)
    blocked = execute(["eval", "1+1"], policy)
    assert blocked["details"] == {
        "error_class": "policy_block",
        "verb": "eval",
        "reason": "verb 'eval' is not enabled by the browser-only policy",
    }
    assert not (policy.run_dir / "capture" / "seq").exists()


def test_navigation_domain_resolution():
    assert resolve_navigation_domains(
        "https://www.example.com/start", "task-host", ["login.example.net"]
    ) == ["www.example.com", "login.example.net"]
    assert resolve_navigation_domains(None, "allowlist", ["example.com"]) == ["example.com"]
    assert resolve_navigation_domains(None, "unrestricted", []) == []
    assert resolve_navigation_domains(
        "https://www.example.com/start", "task-redirects", ["login.example.net"]
    ) == ["www.example.com", "login.example.net"]
    with pytest.raises(ValueError, match="requires task.url"):
        resolve_navigation_domains(None, "task-host", [])


def test_navigation_bootstrap_chain_includes_events_and_final_url():
    payload = {
        "events": [
            {"kind": "navigation", "data": {"to": "https://example.ca/landing"}},
            {"kind": "console", "data": {"url": "https://ignored.test"}},
        ],
        "browser": {"url": "https://shop.example.ca/final"},
    }
    assert _navigation_urls(payload, "https://example.com/") == (
        [
            "https://example.com/",
            "https://example.ca/landing",
            "https://shop.example.ca/final",
        ],
        "https://shop.example.ca/final",
    )
