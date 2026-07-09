#!/usr/bin/env bash
# serve-fixtures.sh — serve the repo's test fixture pages for local agent runs,
# so worktree testing has a deterministic, offline target (no real-site flakiness).
# Usage: experiments/serve-fixtures.sh [port]   (default 8196)
#   then: ./run-agent.sh -t ebrowse -w "open http://127.0.0.1:8196/list.html and …"
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8196}"
DIR="$(cd "$HERE/.." && pwd)/tests/fixtures/pages"
[[ -d "$DIR" ]] || { echo "no fixtures at $DIR" >&2; exit 1; }
echo "serving $DIR" >&2
echo "  http://127.0.0.1:$PORT/list.html   (32-item product grid)" >&2
echo "  http://127.0.0.1:$PORT/form.html   (signup form)" >&2
echo "  http://127.0.0.1:$PORT/dialogs.html  http://127.0.0.1:$PORT/native_modal.html  (dialogs/modals)" >&2
echo "Ctrl-C to stop." >&2
exec python3 -m http.server "$PORT" --directory "$DIR"
