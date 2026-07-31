"""Central local web application for browsing trace runs by directory."""

from __future__ import annotations

import html
import json
import subprocess
import threading
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from ebrowse_evals.trace.records import Anomaly, RunEnd, RunMeta, Step, Summary
from ebrowse_evals.trace.store import TraceReader
from ebrowse_evals.viewer import render_run, render_step_fragment, website


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


@dataclass(slots=True)
class RunSummary:
    relative: Path
    group: str
    run_id: str
    task_id: str
    prompt: str
    website: str
    model: str
    outcome: str
    verdict: str
    steps: int
    anomalies: int
    updated: float


def _summary(root: Path, run_dir: Path) -> RunSummary:
    """One pass over events.jsonl -- the index reads every run on every
    refresh, so re-scanning the file per field does not scale."""
    reader = TraceReader(run_dir)
    meta: RunMeta | None = None
    end: RunEnd | None = None
    steps = anomalies = 0
    verdict = first_url = ""
    for rec in reader.records():
        if isinstance(rec, RunMeta):
            meta = meta or rec
        elif isinstance(rec, RunEnd):
            end = end or rec
        elif isinstance(rec, Step):
            steps += 1
            first_url = first_url or str((rec.browser or {}).get("url") or "")
        elif isinstance(rec, Anomaly):
            anomalies += 1
        elif isinstance(rec, Summary) and rec.kind == "verdict" and not verdict:
            verdict = rec.text
    relative = run_dir.relative_to(root)
    agent: dict[str, Any] = meta.agent if meta is not None else {}
    return RunSummary(
        relative=relative,
        group=str(relative.parent) if relative.parent != Path(".") else "Runs",
        run_id=(meta.run_id if meta and meta.run_id else run_dir.name),
        task_id=(meta.task_id if meta and meta.task_id else "?"),
        prompt=(meta.prompt.strip() if meta and meta.prompt else ""),
        website=website(meta.config if meta else {}, first_url),
        model=str(agent.get("model", "")),
        outcome=(end.outcome if end else "in progress"),
        verdict=verdict,
        steps=(end.steps if end else steps),
        anomalies=anomalies,
        updated=(run_dir / "events.jsonl").stat().st_mtime,
    )


def discover_runs(root: Path) -> list[RunSummary]:
    """Find trace directories recursively, newest first within directory."""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"runs directory not found: {root}")
    runs: list[RunSummary] = []
    for events in root.rglob("events.jsonl"):
        try:
            runs.append(_summary(root, events.parent))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return sorted(runs, key=lambda r: (r.group.casefold(), -r.updated, r.run_id.casefold()))


_CSS = """
:root { --bg:#f5f6f8; --panel:#fff; --fg:#20242a; --muted:#6c737f; --line:#dfe2e7;
  --accent:#356fc2; --good:#217a43; --bad:#b33e32; --warn:#936c12; }
@media (prefers-color-scheme:dark) { :root { --bg:#15171b; --panel:#202329; --fg:#e7e8ea;
  --muted:#9da3ad; --line:#343941; --accent:#78a9ed; --good:#69c98b; --bad:#ef8175; --warn:#dfbd67; } }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.45 system-ui,sans-serif; } main { max-width:1800px; margin:auto; padding:2rem 1.25rem 5rem; }
header { display:flex; align-items:end; justify-content:space-between; gap:1rem; margin-bottom:1.5rem; }
h1 { margin:0; font-size:1.6rem; } h2 { margin:1.8rem 0 .55rem; font-size:1rem; }
.root,.muted,.count { color:var(--muted); } .group { background:var(--panel); border:1px solid var(--line);
  border-radius:9px; overflow:hidden; } table { width:100%; border-collapse:collapse; table-layout:fixed; }
th { color:var(--muted); font-size:.72rem; text-transform:uppercase; letter-spacing:.04em;
  text-align:left; padding:.55rem .7rem; } td { padding:.65rem .7rem; border-top:1px solid var(--line);
  vertical-align:top; }
tr:hover td { background:color-mix(in srgb,var(--accent) 5%,transparent); }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
.num { text-align:right; white-space:nowrap; }
.c-task { width:34%; } .c-outcome { width:7rem; } .c-verdict { width:32%; }
.c-model { width:11rem; } .c-num { width:5.5rem; } .c-updated { width:6.5rem; }
.run { display:block; font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sub { color:var(--muted); font-size:.78rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.site { color:var(--fg); } .verdict { font-size:.86rem; line-height:1.35;
  display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
.status { display:inline-block; border-radius:10px; padding:.08rem .48rem; background:var(--line); }
.status-success { color:var(--good); } .status-error,.status-failure,.status-timeout,.status-tool_limit { color:var(--bad); }
.status-in_progress { color:var(--warn); }
@media(max-width:1100px) { .optional { display:none; } .c-task { width:40%; } .c-verdict { width:36%; } }
.pick { width:2.2rem; text-align:center; } h2 label { cursor:pointer; }
.toolbar { position:sticky; top:0; z-index:2; display:flex; justify-content:flex-end; align-items:center;
  gap:.8rem; padding:.65rem 0; background:var(--bg); }
button { border:0; border-radius:7px; padding:.48rem .8rem; background:var(--bad); color:white;
  font-weight:650; cursor:pointer; } button:disabled { opacity:.45; cursor:not-allowed; }
.notice { border:1px solid var(--good); color:var(--good); background:var(--panel); padding:.65rem .8rem;
  border-radius:7px; margin-bottom:.7rem; } .notice.error { border-color:var(--bad); color:var(--bad); }
"""


def _run_label(run_id: str, task_id: str) -> str:
    """Run ids are conventionally `<run-name>-<task-id>`; the task id is already
    implied by the instruction line, so show only the run-name part."""
    label = run_id.replace(task_id, "").strip("-_ ") if task_id else run_id
    return label or run_id


def _age(ts: float, now: float) -> str:
    seconds = max(0, int(now - ts))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def render_index(
    root: Path,
    runs: list[RunSummary] | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    root = root.resolve()
    runs = discover_runs(root) if runs is None else runs
    groups: dict[str, list[RunSummary]] = {}
    for run in runs:
        groups.setdefault(run.group, []).append(run)
    now = datetime.now(UTC).timestamp()
    sections: list[str] = []
    for group, items in groups.items():
        rows: list[str] = []
        group_id = f"group-{len(sections)}"
        for run in items:
            href = "/run/" + quote(run.relative.as_posix(), safe="/")
            status_class = run.outcome.replace(" ", "_")
            # Line 1 is the instruction (the only thing that distinguishes tasks
            # at a glance); line 2 carries the site plus the run id, which the
            # instruction alone cannot disambiguate across repeated runs.
            instruction = run.prompt or run.task_id
            site = f'<span class="site">{_e(run.website)}</span> &middot; ' if run.website else ""
            verdict = (
                f'<div class="verdict" title="{_e(run.verdict)}">{_e(run.verdict)}</div>'
                if run.verdict
                else '<span class="muted">&mdash;</span>'
            )
            rows.append(
                f'<tr><td class="pick"><input type="checkbox" name="run" '
                f'value="{_e(run.relative.as_posix())}" data-group="{group_id}" '
                f'aria-label="Select {_e(run.run_id)}"></td>'
                f'<td class="task"><a class="run" href="{href}" title="{_e(instruction)}">'
                f'{_e(instruction)}</a><div class="sub" title="{_e(run.run_id)}">'
                f"{site}{_e(_run_label(run.run_id, run.task_id))}</div></td>"
                f'<td><span class="status status-{_e(status_class)}">{_e(run.outcome)}</span></td>'
                f"<td>{verdict}</td>"
                f'<td class="optional sub">{_e(run.model)}</td><td class="num">{run.steps}</td>'
                f'<td class="num">{run.anomalies}</td><td class="num muted">{_e(_age(run.updated, now))}</td></tr>'
            )
        sections.append(
            f'<h2><label><input class="group-pick" type="checkbox" data-group="{group_id}"> '
            f"{_e(group)}</label></h2><div class='group'><table><thead><tr><th class='pick'></th>"
            "<th class='c-task'>Task</th><th class='c-outcome'>Outcome</th>"
            "<th class='c-verdict'>Summary</th><th class='optional c-model'>Model</th>"
            "<th class='num c-num'>Steps</th><th class='num c-num'>Anomalies</th>"
            "<th class='num c-updated'>Updated</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )
    content = (
        "".join(sections)
        or "<div class='group'><p style='padding:1rem'>No trace runs found.</p></div>"
    )
    notice_html = f'<div class="notice">{_e(notice)}</div>' if notice else ""
    error_html = f'<div class="notice error">{_e(error)}</div>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ebrowse eval runs</title>
<style>{_CSS}</style></head><body><main><header><div><h1>ebrowse eval runs</h1>
<div class="root">{_e(root)}</div></div><div class="count">{len(runs)} run{"s" if len(runs) != 1 else ""}</div>
</header>{notice_html}{error_html}<form method="post" action="/trash" id="runs-form">
<div class="toolbar"><span id="selected-count">0 selected</span>
<button type="submit" id="trash-button" disabled>Move selected to trash</button></div>
{content}</form></main><script>
const rows = [...document.querySelectorAll('input[name="run"]')];
const groups = [...document.querySelectorAll('.group-pick')];
const count = document.getElementById('selected-count');
const button = document.getElementById('trash-button');
function sync() {{
  const n = rows.filter(x => x.checked).length;
  count.textContent = `${{n}} selected`; button.disabled = n === 0;
  groups.forEach(g => {{ const xs=rows.filter(x => x.dataset.group===g.dataset.group);
    g.checked=xs.length>0 && xs.every(x => x.checked); g.indeterminate=xs.some(x => x.checked)&&!g.checked; }});
}}
rows.forEach(x => x.addEventListener('change', sync));
groups.forEach(g => g.addEventListener('change', () => {{ rows.filter(x => x.dataset.group===g.dataset.group)
  .forEach(x => x.checked=g.checked); sync(); }}));
document.getElementById('runs-form').addEventListener('submit', e => {{
  const n=rows.filter(x => x.checked).length;
  if (!confirm(`Move ${{n}} selected run${{n===1?'':'s'}} to the system trash?`)) e.preventDefault();
}});
</script></body></html>"""


def _safe_run(root: Path, encoded: str) -> Path | None:
    candidate = (root / unquote(encoded)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if (candidate / "events.jsonl").is_file() else None


def trash_runs(root: Path, selected: list[str]) -> int:
    """Move selected trace directories to the system trash after containment checks."""
    root = root.resolve()
    paths: list[Path] = []
    seen: set[Path] = set()
    for value in selected:
        run_dir = _safe_run(root, value)
        if run_dir is None or run_dir == root:
            raise ValueError(f"invalid trace run: {value}")
        if run_dir not in seen:
            paths.append(run_dir)
            seen.add(run_dir)
    if not paths:
        raise ValueError("no trace runs selected")
    try:
        proc = subprocess.run(
            ["trash-put", "--", *(str(path) for path in paths)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError("trash-put is not installed") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"trash-put failed: {detail}")
    return len(paths)


def make_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    root = root.resolve()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                query = parse_qs(parsed.query)
                self._html(
                    render_index(
                        root,
                        notice=query.get("notice", [None])[0],
                        error=query.get("error", [None])[0],
                    )
                )
            elif path.startswith("/run/"):
                run_dir = _safe_run(root, path.removeprefix("/run/"))
                if run_dir is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "trace run not found")
                else:
                    relative = run_dir.relative_to(root).as_posix()

                    def blob_url(ref: str) -> str:
                        return "/blob?" + urlencode({"run": relative, "ref": ref})

                    def fragment_url(offset: int, limit: int) -> str:
                        return "/steps?" + urlencode(
                            {"run": relative, "offset": offset, "limit": limit}
                        )

                    self._html(
                        render_run(
                            run_dir,
                            back_href="/",
                            blob_url=blob_url,
                            fragment_url=fragment_url,
                            compact_middle=True,
                        )
                    )
            elif path == "/steps":
                query = parse_qs(parsed.query)
                run_dir = _safe_run(root, query.get("run", [""])[0])
                try:
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    limit = min(10, max(1, int(query.get("limit", ["10"])[0])))
                except ValueError:
                    self.send_error(HTTPStatus.BAD_REQUEST, "invalid step range")
                    return
                if run_dir is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "trace run not found")
                    return
                relative = run_dir.relative_to(root).as_posix()
                self._html(
                    render_step_fragment(
                        run_dir,
                        offset,
                        limit,
                        blob_url=lambda ref: "/blob?" + urlencode({"run": relative, "ref": ref}),
                    )
                )
            elif path == "/blob":
                query = parse_qs(parsed.query)
                run_dir = _safe_run(root, query.get("run", [""])[0])
                ref = query.get("ref", [""])[0]
                if run_dir is None or not ref.startswith("sha256:"):
                    self.send_error(HTTPStatus.NOT_FOUND, "blob not found")
                    return
                try:
                    data = TraceReader(run_dir).blobs.get(ref)
                except (FileNotFoundError, OSError, ValueError):
                    self.send_error(HTTPStatus.NOT_FOUND, "blob not found")
                    return
                if data.startswith(b"\x89PNG\r\n"):
                    content_type = "image/png"
                elif data.startswith(b"\xff\xd8\xff"):
                    content_type = "image/jpeg"
                else:
                    content_type = "application/json; charset=utf-8"
                self._send(data, content_type)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/trash":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1_000_000:
                    raise ValueError("request body too large")
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                count = trash_runs(root, form.get("run", []))
            except (UnicodeDecodeError, ValueError, RuntimeError) as e:
                self._redirect("/?" + urlencode({"error": str(e)}))
            else:
                noun = "run" if count == 1 else "runs"
                self._redirect("/?" + urlencode({"notice": f"Moved {count} {noun} to trash"}))

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

        def _html(self, body: str) -> None:
            self._send(body.encode(), "text/html; charset=utf-8")

        def _send(self, data: bytes, content_type: str) -> None:
            # Browsers routinely cancel an obsolete navigation or lazy asset
            # request. BaseHTTPRequestHandler otherwise prints a full worker
            # traceback for this harmless disconnect.
            with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return Handler


def serve_runs(root: Path, host: str, port: int, open_browser: bool = False) -> None:
    root = root.resolve()
    discover_runs(root)
    server = ThreadingHTTPServer((host, port), make_handler(root))
    url = f"http://{host}:{server.server_port}/"
    print(f"serving {root} at {url}", flush=True)
    if open_browser:
        threading.Timer(0.15, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
