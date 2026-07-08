# experiments — driving a local Qwen agent over ebrowse vs agent-browser

Harness for running an actual coding agent (backed by local Qwen) against a
browsing task, and comparing browser tools (`ebrowse` vs `agent-browser`) on
token cost and steps. The agent is the **pi** harness driving the tool as an
ordinary CLI via its shell — no MCP, so both tools are measured on equal footing.

## Stack

| Piece | What | Where |
|---|---|---|
| Model | `unsloth/Qwen3.6-35B-A3B-MTP`, 128k ctx (reasoning) | llama-server on `http://localhost:5001` |
| Harness | [pi](https://pi.dev) coding agent (`pi`) | `npm i -g @earendil-works/pi-coding-agent` |
| Tool A | `ebrowse` | `uv tool install --editable .` in repo root |
| Tool B | `agent-browser` | `npm i -g agent-browser` |

Provider wiring lives in `~/.pi/agent/models.json` (provider `qwen-local`,
`api: openai-completions` → `:5001/v1`). We use the **Chat Completions**
transport: llama-server also exposes `/v1/responses`, but chat is the robust,
well-supported path. `compat.supportsDeveloperRole` / `supportsReasoningEffort`
are disabled because llama-server doesn't understand those OpenAI-isms.

## Prerequisites

1. llama-server up on :5001 — `curl -s localhost:5001/v1/models` should list Qwen.
2. `pi --list-models qwen` should show the `qwen-local` provider.
3. `ebrowse --help` and `agent-browser --help` both resolve on PATH.

## Run a task

```bash
# ebrowse, task from a file, capture JSON events (tokens/tool-calls)
./run-agent.sh -t ebrowse -f tasks/example.txt -j -n eb-example

# agent-browser, same task, for comparison
./run-agent.sh -t agent-browser -f tasks/example.txt -j -n ab-example

# inline task, no tool guide
./run-agent.sh "Reply with one word: pong"
```

`-t <tool>` prepends that tool's own operating guide to the prompt
(ebrowse's `SKILL.md`; `agent-browser skills get core --full`) so each tool is
driven the way its authors intend. Every run is saved under local-only
`runs/<name>/`: `prompt.txt`, `meta.txt`, `output.txt` (or `events.jsonl` with
`-j`), `stderr.log`, and an isolated `workdir/`. Put screenshots or trajectory
artifacts under that run directory; `runs/` is ignored because it can grow large.
The wrapper also saves the full **pi session** to ignored
`sessions/<timestamp>_<id>.jsonl` (tagged with `--name`), so any run can be
inspected or resumed later. See `./run-agent.sh -h` for all flags.

## Compare

```bash
./summarize-run.py runs/eb-example/events.jsonl runs/ab-example/events.jsonl
```

Prints turns / tool-calls / input / output / reasoning / total tokens per run —
the token-per-task metric ebrowse is designed to win on.

## Inspect a run

`summarize-run.py` reads `-j` event streams; `inspect-session.py` reads the saved
**pi session** (works for both wrapper runs and interactive `pi` sessions) and shows
the step-by-step command trail — the best way to see *what the agent actually did*.

```bash
# newest session across interactive pi and wrapper runs
./inspect-session.py --latest

# newest wrapper run only (sessions live under experiments/sessions)
./inspect-session.py --latest --dir sessions

# newest session under multiple explicit roots
./inspect-session.py --latest --dir ~/.pi/agent/sessions --dir sessions

# a specific session, with the full turn-by-turn transcript
./inspect-session.py sessions/2026-07-08T19-47-27-291Z_<id>.jsonl --full
```

Summary reports output tokens generated, input tokens billed, and **peak context**
(max single-turn `totalTokens`) — summing per-turn totals would overcount, since each
turn's total already includes the whole prior context.

Quick parser smoke test, no model required:

```bash
./inspect-session.py fixtures/sample-pi-session.jsonl
```

## Caveats / knobs

- **Guide asymmetry:** agent-browser's core skill is ~2500 lines; ebrowse's
  SKILL.md is ~150. That preamble gap shows up in `input` tokens on turn 1 — it
  reflects each tool's real onboarding cost, but keep it in mind when reading totals.
- **No sandbox:** pi runs shell as your user (no per-tool prompts in `-p` mode).
  Use only bot-friendly sites (see the repo's Online-Mind2Web note) and read-only tasks.
- **Daemon state:** ebrowse and agent-browser keep a browser between calls. Runs
  share that state unless you reset it; for clean measurements, close browsers
  between runs (`ebrowse close`, and kill stray agent-browser chromium) or vary sites.
- **Transport swap:** to mirror codex's Responses path instead, set
  `"api": "openai-responses"` for the `qwen-local` provider in models.json —
  :5001 supports it (verified) — and re-run. Chat is the default here.
