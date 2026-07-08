#!/usr/bin/env bash
# run-agent.sh — drive a coding agent (pi harness) on a browsing task, invoking
# it as a command-line subagent. Each run is saved under runs/ so transcripts
# and token/step counts can be diffed across tools (ebrowse vs agent-browser)
# and prompts.
#
# Requires: pi on PATH; a pi provider/model configured in ~/.pi/agent/models.json
# and selected via $PI_PROVIDER/$PI_MODEL or -p/-m (see experiments/README.md).
set -euo pipefail

PROVIDER="${PI_PROVIDER:-}"
MODEL="${PI_MODEL:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EBROWSE_SKILL="$(cd "$HERE/.." && pwd)/SKILL.md"

TOOL="none"
TASK=""
TASK_FILE=""
NAME=""
JSON=0
WORKDIR=""

usage() {
  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Usage:
  run-agent.sh [options] "<task text>"
  run-agent.sh [options] -f <task-file>

Options:
  -t, --tool <ebrowse|agent-browser|none>  Prepend that tool's operating guide to the
                                           prompt so the agent knows how to drive it (default: none)
  -f, --file <path>                        Read the task from a file instead of an argument
  -n, --name <name>                        Run label (default: <tool>-<UTC timestamp>)
  -d, --dir <path>                         Working dir the agent runs in (default: fresh runs/<name>/workdir)
  -j, --json                               Also capture the full JSON event stream (tokens, tool calls)
  -m, --model <id>                         Model id (default: $PI_MODEL)
  -p, --provider <name>                    pi provider name (default: $PI_PROVIDER)
  -h, --help

Examples:
  run-agent.sh -t ebrowse "Go to example.com and report the page title."
  run-agent.sh -t agent-browser -f tasks/find-product.txt -j -n ab-run1
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--tool)     TOOL="$2"; shift 2 ;;
    -f|--file)     TASK_FILE="$2"; shift 2 ;;
    -n|--name)     NAME="$2"; shift 2 ;;
    -d|--dir)      WORKDIR="$2"; shift 2 ;;
    -j|--json)     JSON=1; shift ;;
    -m|--model)    MODEL="$2"; shift 2 ;;
    -p|--provider) PROVIDER="$2"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    -*)            echo "unknown option: $1" >&2; usage; exit 2 ;;
    *)             TASK="$1"; shift ;;
  esac
done

if [[ -n "$TASK_FILE" ]]; then
  [[ -f "$TASK_FILE" ]] || { echo "task file not found: $TASK_FILE" >&2; exit 1; }
  TASK="$(cat "$TASK_FILE")"
fi
[[ -n "$TASK" ]] || { echo "no task given" >&2; usage; exit 2; }
[[ -n "$PROVIDER" && -n "$MODEL" ]] || {
  echo "no provider/model — set \$PI_PROVIDER and \$PI_MODEL or pass -p/-m" \
       "(see experiments/README.md)" >&2
  exit 2
}

# Build the tool preamble so each tool is driven from its own documented guide
# (fair, consistent instructions across runs).
PREAMBLE=""
case "$TOOL" in
  ebrowse)
    [[ -f "$EBROWSE_SKILL" ]] && PREAMBLE="You control a web browser using the 'ebrowse' CLI. Its operating guide follows.

$(cat "$EBROWSE_SKILL")
"
    ;;
  agent-browser)
    GUIDE="$(agent-browser skills get core --full 2>/dev/null || true)"
    PREAMBLE="You control a web browser using the 'agent-browser' CLI. Its operating guide follows.

$GUIDE
"
    ;;
  none) ;;
  *) echo "unknown tool: $TOOL (want ebrowse|agent-browser|none)" >&2; exit 2 ;;
esac

NAME="${NAME:-${TOOL}-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="$HERE/runs/$NAME"
mkdir -p "$RUN_DIR"
WORKDIR="${WORKDIR:-$RUN_DIR/workdir}"
mkdir -p "$WORKDIR"

PROMPT="$PREAMBLE
# Task
$TASK"

# Record what we ran for reproducibility.
{
  echo "name:      $NAME"
  echo "provider:  $PROVIDER"
  echo "model:     $MODEL"
  echo "tool:      $TOOL"
  echo "workdir:   $WORKDIR"
  echo "utc:       $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$RUN_DIR/meta.txt"
printf '%s\n' "$PROMPT" > "$RUN_DIR/prompt.txt"

echo ">> run: $NAME  (tool=$TOOL, model=$MODEL)" >&2
echo ">> dir: $RUN_DIR" >&2

cd "$WORKDIR"
# Save the session so runs can be inspected/resumed later (inspect-session.py).
# Sessions land under experiments/sessions/; --name makes them findable.
SESSION_DIR="$HERE/sessions"
COMMON=(pi --provider "$PROVIDER" --model "$MODEL" --session-dir "$SESSION_DIR" --name "$NAME")

if [[ "$JSON" -eq 1 ]]; then
  "${COMMON[@]}" --mode json -p "$PROMPT" 2>"$RUN_DIR/stderr.log" \
    | tee "$RUN_DIR/events.jsonl"
  echo ">> events: $RUN_DIR/events.jsonl" >&2
else
  "${COMMON[@]}" -p "$PROMPT" 2>"$RUN_DIR/stderr.log" \
    | tee "$RUN_DIR/output.txt"
  echo ">> output: $RUN_DIR/output.txt" >&2
fi
