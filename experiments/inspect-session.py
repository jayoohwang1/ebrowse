#!/usr/bin/env python3
"""Inspect a pi session JSONL — summary, command trail, and (optionally) the
full conversation. By default, --latest searches both interactive pi sessions
(~/.pi/agent/sessions) and wrapper sessions (experiments/sessions).

Usage:
  inspect-session.py <session.jsonl>              # summary + command trail
  inspect-session.py --latest                     # newest interactive or wrapper session
  inspect-session.py --latest --dir <root>        # newest session under one root
  inspect-session.py <session.jsonl> --full       # + full turn-by-turn transcript

Token note: each assistant turn's `totalTokens` is that turn's whole context, so
summing it across turns overcounts. The honest per-run figures are: output tokens
generated (summed), input tokens billed (summed), and peak context (max in one turn).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".pi" / "agent" / "sessions"
LOCAL_ROOT = Path(__file__).resolve().parent / "sessions"
DEFAULT_ROOTS = (DEFAULT_ROOT, LOCAL_ROOT)


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def find_latest(roots: list[Path]) -> Path | None:
    files = []
    for root in roots:
        if root.exists():
            files.extend(root.rglob("*.jsonl"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            out.append(c.get("text", ""))
    return " ".join(out).strip()


def summarize(entries: list[dict]) -> None:
    header = next((e for e in entries if e.get("type") == "session"), {})
    turns = tool_calls = out_tok = in_tok = peak = 0
    last_stop = last_final = ""
    cmds: list[str] = []
    name = None
    for e in entries:
        t = e.get("type")
        if t == "session_info":
            name = e.get("name")
        if t != "message":
            continue
        m = e["message"]
        role = m.get("role")
        if role == "assistant":
            turns += 1
            u = m.get("usage") or {}
            out_tok += u.get("output", 0)
            in_tok += u.get("input", 0)
            peak = max(peak, u.get("totalTokens", 0))
            last_stop = m.get("stopReason", last_stop)
            txt = _text(m.get("content"))
            if txt:
                last_final = txt
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    tool_calls += 1
                    args = c.get("arguments") or {}
                    label = args.get("command") or json.dumps(args)
                    cmds.append(f"{c.get('name')}: {str(label)}")
        elif role == "bashExecution":
            cmds.append(f"bash: {m.get('command')}")

    print(f"session:   {header.get('id','?')}  (v{header.get('version','?')})")
    print(f"cwd:       {header.get('cwd','?')}")
    if name:
        print(f"name:      {name}")
    print(f"turns:     {turns} assistant   tool-calls: {tool_calls}")
    print(f"tokens:    output={out_tok}  input-billed={in_tok}  peak-context={peak}")
    print(f"stop:      {last_stop}")
    print("\ncommand trail:")
    for c in cmds:
        print("  $ " + c.replace("\n", " ")[:120])
    if last_final:
        print("\nfinal message:\n  " + last_final.replace("\n", "\n  ")[:1000])


def full(entries: list[dict]) -> None:
    for e in entries:
        if e.get("type") != "message":
            continue
        m = e["message"]
        role = m.get("role")
        if role == "user":
            print(f"\n=== USER ===\n{_text(m.get('content'))[:2000]}")
        elif role == "assistant":
            txt = _text(m.get("content"))
            if txt:
                print(f"\n--- assistant ---\n{txt[:2000]}")
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    a = c.get("arguments") or {}
                    print(f"  >> {c.get('name')}({a.get('command') or json.dumps(a)[:200]})")
        elif role == "toolResult":
            body = _text(m.get("content"))
            flag = " [ERROR]" if m.get("isError") else ""
            print(f"  << {m.get('toolName')}{flag}: {body[:400].replace(chr(10),' ')}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?")
    ap.add_argument("--latest", action="store_true", help="use newest session")
    ap.add_argument(
        "--dir",
        action="append",
        help="sessions root for --latest; repeatable (default: pi global + experiments/sessions)",
    )
    ap.add_argument("--full", action="store_true", help="print full transcript")
    args = ap.parse_args(argv)

    if args.latest:
        roots = [Path(d).expanduser() for d in args.dir] if args.dir else list(DEFAULT_ROOTS)
        path = find_latest(roots)
        if not path:
            roots_s = ", ".join(str(p) for p in roots)
            print(f"no sessions under: {roots_s}", file=sys.stderr)
            return 1
    elif args.session:
        path = Path(args.session).expanduser()
    else:
        print(__doc__.strip())
        return 2

    print(f"# {path}\n")
    entries = load(path)
    summarize(entries)
    if args.full:
        print("\n" + "=" * 60 + "\nFULL TRANSCRIPT")
        full(entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
