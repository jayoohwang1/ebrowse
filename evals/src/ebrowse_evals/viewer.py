"""Human trace viewer: render a run directory into one self-contained HTML file.

Design (see evals/docs/viewer.md): a vertical log of the trajectory, one row
per step, two lanes. The RIGHT lane is what the agent saw (command, verbatim
output, assistant text, tokens/latency). The LEFT lane is ground truth + tool
internals: always the step screenshot (a filmstrip for skimming, rendered even
when the agent never looked at it), URL/title, a per-phase timing bar, anomaly
badges — and, behind a per-step expander, browser events, grouped ebrowse_log
records, browser state, blob refs with the inlined DomSnapshot JSON, and the
structured error. Philosophy: track everything, hide verbosity behind
expansion; the collapsed view must stay skimmable.

Pure renderer: run dir in, HTML string out. No framework, no CDN — all CSS/JS
inline, screenshots embedded as data URIs. Unknown record types/fields render
as raw JSON instead of breaking (schema rule 1).
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from ebrowse_evals.trace.store import BlobStore, TraceReader

# Cap for inlined DomSnapshot JSON so a huge page can't make the HTML unopenable.
_MAX_INLINE_JSON = 200_000

_PHASE_COLORS = ["#4c8dd6", "#57a773", "#c9a227", "#b06ab3", "#d0684f", "#5aa7a7", "#8a8fd0"]


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _image_data_uri(data: bytes) -> str | None:
    """Data URI for real image bytes; None for anything else (fixture blobs
    are fake text .pngs — those get a placeholder, not a broken <img>)."""
    if data.startswith(b"\x89PNG\r\n"):
        return "data:image/png;base64," + _b64(data)
    if data.startswith(b"\xff\xd8\xff"):
        return "data:image/jpeg;base64," + _b64(data)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "data:image/webp;base64," + _b64(data)
    return None


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _short_ref(ref: str) -> str:
    return ref.removeprefix("sha256:")[:12]


def _fmt_ts_offset(rec: dict[str, Any], t0: float | None) -> str:
    ts = rec.get("ts")
    if not isinstance(ts, (int, float)) or t0 is None:
        return ""
    return f"t+{ts - t0:.1f}s"


def _kv_table(d: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><th>{_e(k)}</th><td>{_e(json.dumps(v) if isinstance(v, (dict, list)) else v)}</td></tr>"
        for k, v in d.items()
    )
    return f'<table class="kv">{rows}</table>' if rows else ""


def _timing_bar(timing: dict[str, Any]) -> str:
    phases = [(k, v) for k, v in timing.items() if isinstance(v, (int, float)) and v > 0]
    total = sum(v for _, v in phases)
    if not phases or total <= 0:
        return ""
    segs = "".join(
        f'<span class="tseg" style="width:{100 * v / total:.1f}%;'
        f'background:{_PHASE_COLORS[i % len(_PHASE_COLORS)]}" title="{_e(k)} {v:.2f}s"></span>'
        for i, (k, v) in enumerate(phases)
    )
    legend = " &middot; ".join(f"{_e(k)} {v:.2f}s" for k, v in phases)
    return f'<div class="tbar">{segs}</div><div class="tlegend">{legend}</div>'


def _screenshot_cell(ref: str | None, blobs: BlobStore) -> str:
    if not ref:
        return '<div class="shot placeholder">no screenshot captured</div>'
    try:
        data = blobs.get(ref)
    except (FileNotFoundError, OSError):
        return f'<div class="shot placeholder">missing blob<br><code>{_e(_short_ref(ref))}</code></div>'
    uri = _image_data_uri(data)
    if uri is None:
        return (
            f'<div class="shot placeholder">screenshot blob (not an image)<br>'
            f"<code>{_e(_short_ref(ref))}</code></div>"
        )
    return f'<img class="shot" src="{uri}" alt="screenshot {_e(_short_ref(ref))}" loading="lazy">'


def _dom_snapshot_block(ref: str | None, blobs: BlobStore) -> str:
    if not ref:
        return "<p class='muted'>no DomSnapshot captured</p>"
    label = f"DomSnapshot <code>{_e(_short_ref(ref))}</code>"
    try:
        data = blobs.get(ref)
    except (FileNotFoundError, OSError):
        return f"<p class='muted'>{label} — blob missing</p>"
    try:
        text = json.dumps(json.loads(data.decode("utf-8")), indent=2, ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        return f"<p class='muted'>{label} — not valid JSON ({len(data)} bytes)</p>"
    truncated = ""
    if len(text) > _MAX_INLINE_JSON:
        text = text[:_MAX_INLINE_JSON]
        truncated = f"<p class='muted'>truncated at {_MAX_INLINE_JSON} chars</p>"
    return f"<details><summary>{label} ({len(data)} bytes)</summary><pre>{_e(text)}</pre>{truncated}</details>"


def _browser_event_html(rec: dict[str, Any], t0: float | None) -> str:
    kind = rec.get("kind", "?")
    data = rec.get("data", {})
    detail = (
        ", ".join(f"{_e(k)}={_e(v)}" for k, v in data.items())
        if isinstance(data, dict)
        else _e(data)
    )
    return (
        f'<li><span class="tag tag-{_e(kind)}">{_e(kind)}</span> {detail} '
        f'<span class="muted">{_fmt_ts_offset(rec, t0)}</span></li>'
    )


def _log_rows(logs: list[dict[str, Any]]) -> str:
    """ebrowse_log records grouped by module; debug rows hidden unless the
    global 'show debug' toggle is on (body.show-debug)."""
    by_module: dict[str, list[dict[str, Any]]] = {}
    for rec in logs:
        by_module.setdefault(str(rec.get("module", "?")), []).append(rec)
    out: list[str] = []
    for module, recs in sorted(by_module.items()):
        rows = "".join(
            f'<li class="log-{_e(r.get("level", "info"))}">'
            f'<span class="lvl">{_e(r.get("level", "info"))}</span> '
            f"<strong>{_e(r.get('event', ''))}</strong> "
            f"<code>{_e(json.dumps(r.get('fields', {}), ensure_ascii=False))}</code></li>"
            for r in recs
        )
        out.append(f"<div class='log-module'><h5>{_e(module)}</h5><ul>{rows}</ul></div>")
    return "".join(out)


def _anomaly_badges(anomalies: list[dict[str, Any]]) -> str:
    return "".join(
        f'<span class="badge" title="{_e(a.get("message", ""))}">{_e(a.get("kind", "anomaly"))}</span>'
        for a in anomalies
    )


def _step_row(
    step: dict[str, Any],
    attached: dict[str, list[dict[str, Any]]],
    blobs: BlobStore,
    t0: float | None,
) -> str:
    n = step.get("step", "?")
    browser = step.get("browser", {}) if isinstance(step.get("browser"), dict) else {}
    url, title = browser.get("url", ""), browser.get("title", "")
    anomalies = attached.get("anomaly", [])
    events = attached.get("browser_event", [])
    logs = attached.get("ebrowse_log", [])
    unknown = attached.get("_unknown", [])

    # -- left lane (ground truth + internals) ------------------------------
    left: list[str] = [
        _screenshot_cell(step.get("screenshot"), blobs),
        f'<div class="pageid"><strong>{_e(title)}</strong><br><span class="url">{_e(url)}</span></div>',
        _timing_bar(step.get("timing", {}) or {}),
    ]
    if anomalies:
        left.append(f'<div class="badges">{_anomaly_badges(anomalies)}</div>')

    inner: list[str] = []
    if events:
        inner.append(
            "<h4>browser events</h4><ul class='events'>"
            + "".join(_browser_event_html(ev, t0) for ev in events)
            + "</ul>"
        )
    if logs:
        inner.append("<h4>ebrowse log</h4>" + _log_rows(logs))
    if browser:
        inner.append("<h4>browser state</h4>" + _kv_table(browser))
    inner.append("<h4>blobs</h4>" + _dom_snapshot_block(step.get("dom_snapshot"), blobs))
    if step.get("screenshot"):
        inner.append(
            f"<p class='muted'>screenshot <code>{_e(_short_ref(step['screenshot']))}</code></p>"
        )
    if anomalies:
        inner.append(
            "<h4>anomalies</h4><ul>"
            + "".join(
                f"<li><strong>{_e(a.get('kind', ''))}</strong>: {_e(a.get('message', ''))}</li>"
                for a in anomalies
            )
            + "</ul>"
        )
    error = step.get("error")
    if isinstance(error, dict):
        inner.append("<h4>error</h4><div class='error-box'>" + _kv_table(error) + "</div>")
    if unknown:
        inner.append(
            "<h4>other records</h4><pre>"
            + _e("\n".join(json.dumps(u, ensure_ascii=False) for u in unknown))
            + "</pre>"
        )
    left.append(
        "<details class='internals'><summary>internals</summary>" + "".join(inner) + "</details>"
    )

    # -- right lane (what the agent saw) -----------------------------------
    right: list[str] = []
    if step.get("agent_text"):
        right.append(f'<div class="agent-text">{_e(step["agent_text"])}</div>')
    right.append(f'<div class="command"><code>{_e(step.get("command", ""))}</code></div>')
    right.append(f'<pre class="output">{_e(step.get("output", ""))}</pre>')
    stats: list[str] = []
    tokens = step.get("tokens", {})
    if isinstance(tokens, dict) and tokens:
        stats.append(" / ".join(f"{_e(k)} {_e(v)}" for k, v in tokens.items()) + " tok")
    if isinstance(step.get("latency_s"), (int, float)):
        stats.append(f"{step['latency_s']:.2f}s")
    if step.get("exit_code") not in (None, 0):
        stats.append(f"exit {_e(step['exit_code'])}")
    if stats:
        right.append(f'<div class="stats">{" &middot; ".join(stats)}</div>')

    return (
        f'<section class="step" id="step-{_e(n)}">'
        f'<div class="lane lane-left"><div class="stepno">step {_e(n)} '
        f'<span class="muted">{_fmt_ts_offset(step, t0)}</span></div>{"".join(left)}</div>'
        f'<div class="lane lane-right">{"".join(right)}</div>'
        f"</section>"
    )


def _summary_marker(rec: dict[str, Any]) -> str:
    a, b = rec.get("step_start", "?"), rec.get("step_end", "?")
    model = f" <span class='muted'>({_e(rec['model'])})</span>" if rec.get("model") else ""
    return (
        f'<div class="summary-marker">summary steps {_e(a)}&ndash;{_e(b)}{model}: '
        f"{_e(rec.get('text', ''))}</div>"
    )


def _header(
    meta: dict[str, Any] | None,
    end: dict[str, Any] | None,
    anomalies: list[dict[str, Any]],
    n_steps: int,
) -> str:
    meta = meta or {}
    end = end or {}
    agent = meta.get("agent", {}) if isinstance(meta.get("agent"), dict) else {}
    facts = {
        "task": meta.get("task_id", "?"),
        "run": meta.get("run_id", "?"),
        "benchmark": meta.get("benchmark"),
        "agent": " / ".join(str(v) for v in agent.values()) or None,
        "git": (str(meta.get("git_sha", ""))[:10] or None),
        "ebrowse": f"{meta.get('ebrowse_version', '?')} ({meta.get('ebrowse_mode', '?')})",
        "schema": f"v{meta.get('schema_version', '?')}",
    }
    fact_rows = "".join(
        f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in facts.items() if v is not None
    )
    outcome = end.get("outcome", "in progress / no run_end")
    totals = end.get("totals", {})
    totals_line = " &middot; ".join(f"{_e(k)} {_e(v)}" for k, v in totals.items())
    eval_line = ""
    if isinstance(end.get("eval"), dict):
        eval_line = (
            f"<div>eval: <code>{_e(json.dumps(end['eval'], ensure_ascii=False))}</code></div>"
        )
    anomaly_items = (
        "".join(
            f'<li><a href="#step-{_e(a.get("step", ""))}">step {_e(a.get("step", "?"))}</a> '
            f"<strong>{_e(a.get('kind', ''))}</strong> — {_e(a.get('message', ''))}</li>"
            for a in anomalies
        )
        or "<li class='muted'>none</li>"
    )
    config = meta.get("config", {})
    config_block = (
        f"<details><summary>resolved config</summary><pre>"
        f"{_e(json.dumps(config, indent=2, ensure_ascii=False))}</pre></details>"
        if config
        else ""
    )
    return f"""<header>
<h1>{_e(meta.get("task_id", "trace"))} <span class="outcome outcome-{_e(outcome)}">{_e(outcome)}</span></h1>
<p class="prompt">{_e(meta.get("prompt", ""))}</p>
<table class="kv">{fact_rows}</table>
{config_block}
<div class="totals">{n_steps} steps &middot; {totals_line}</div>
{eval_line}
<div class="anomaly-list"><h3>anomalies ({len(anomalies)})</h3><ul>{anomaly_items}</ul></div>
<label class="debug-toggle"><input type="checkbox" id="show-debug"> show debug log events</label>
</header>"""


_CSS = """
:root { --bg:#ffffff; --fg:#1a1a1a; --muted:#777; --line:#d8d8d8; --panel:#f5f5f7;
  --code-bg:#eef0f3; --accent:#4c8dd6; --bad:#c04a3a; --warn:#b58a1f; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181c; --fg:#e4e4e2; --muted:#9a9a96; --line:#33363d; --panel:#1e2127;
    --code-bg:#262a31; --accent:#6aa5e0; --bad:#e07a68; --warn:#d4b04a; } }
body { margin:0 auto; max-width:1200px; padding:1rem 1.5rem 4rem; background:var(--bg);
  color:var(--fg); font:14px/1.5 system-ui,sans-serif; }
h1 { font-size:1.3rem; } h4,h5 { margin:.8em 0 .3em; font-size:.85rem; text-transform:uppercase;
  letter-spacing:.04em; color:var(--muted); }
code,pre { font:12px/1.45 ui-monospace,Menlo,Consolas,monospace; }
pre { background:var(--code-bg); padding:.6rem .7rem; border-radius:6px; overflow-x:auto;
  white-space:pre-wrap; word-break:break-word; margin:.4rem 0; }
header { border-bottom:2px solid var(--line); padding-bottom:1rem; margin-bottom:1rem; }
.prompt { font-style:italic; }
.kv { border-collapse:collapse; margin:.4rem 0; }
.kv th { text-align:left; padding:.1rem .8rem .1rem 0; color:var(--muted); font-weight:500;
  vertical-align:top; white-space:nowrap; }
.kv td { padding:.1rem 0; word-break:break-all; }
.outcome { font-size:.8rem; padding:.15rem .5rem; border-radius:9px; background:var(--panel);
  vertical-align:middle; }
.outcome-success { background:#2e7d3222; color:#2e7d32; }
.outcome-failure,.outcome-error,.outcome-timeout { background:#c04a3a22; color:var(--bad); }
.anomaly-list ul { margin:.2rem 0; padding-left:1.2rem; }
.debug-toggle { color:var(--muted); font-size:.85rem; }
.step { display:grid; grid-template-columns:340px 1fr; gap:1.2rem; padding:1rem 0;
  border-bottom:1px solid var(--line); }
@media (max-width:800px) { .step { grid-template-columns:1fr; } }
.stepno { font-weight:600; margin-bottom:.4rem; }
.shot { max-width:100%; border:1px solid var(--line); border-radius:6px; display:block; }
.shot.placeholder { display:flex; flex-direction:column; align-items:center; justify-content:center;
  height:120px; background:var(--panel); color:var(--muted); font-size:.8rem; text-align:center; }
.pageid { margin:.4rem 0; font-size:.85rem; } .url { color:var(--muted); word-break:break-all; }
.tbar { display:flex; height:8px; border-radius:4px; overflow:hidden; margin-top:.4rem; }
.tseg { display:block; height:100%; }
.tlegend { font-size:.75rem; color:var(--muted); margin:.15rem 0 .4rem; }
.badges { margin:.3rem 0; }
.badge { display:inline-block; background:#c04a3a22; color:var(--bad); border:1px solid var(--bad);
  border-radius:9px; font-size:.72rem; padding:.05rem .5rem; margin-right:.3rem; }
.internals { background:var(--panel); border-radius:6px; padding:.3rem .6rem; margin-top:.4rem; }
.internals summary { cursor:pointer; color:var(--muted); }
.events { padding-left:1.1rem; margin:.2rem 0; }
.tag { font-size:.72rem; border:1px solid var(--line); border-radius:9px; padding:.02rem .4rem; }
.log-module ul { list-style:none; padding-left:.3rem; margin:.2rem 0; }
.lvl { display:inline-block; width:3.2em; color:var(--muted); font-size:.75rem; }
.log-warn .lvl { color:var(--warn); }
body:not(.show-debug) .log-debug { display:none; }
.agent-text { color:var(--muted); font-style:italic; margin-bottom:.4rem; }
.command code { background:var(--code-bg); padding:.25rem .5rem; border-radius:6px;
  border-left:3px solid var(--accent); display:inline-block; }
.output { max-height:30rem; overflow-y:auto; }
.stats { color:var(--muted); font-size:.8rem; }
.summary-marker { border-left:3px solid var(--accent); background:var(--panel); padding:.5rem .8rem;
  margin:.8rem 0; border-radius:0 6px 6px 0; font-size:.9rem; }
.error-box { border:1px solid var(--bad); border-radius:6px; padding:.3rem .6rem; }
.muted { color:var(--muted); }
"""

_JS = """
document.getElementById('show-debug').addEventListener('change', function () {
  document.body.classList.toggle('show-debug', this.checked);
});
"""


def render_run(run_dir: Path) -> str:
    """Render a run directory to a single self-contained HTML page."""
    reader = TraceReader(run_dir)  # raises FileNotFoundError -> CLI reports it
    records = list(reader.raw())

    meta = next((r for r in records if r.get("type") == "run_meta"), None)
    end = next((r for r in records if r.get("type") == "run_end"), None)
    steps = [r for r in records if r.get("type") == "step"]
    anomalies = [r for r in records if r.get("type") == "anomaly"]
    summaries = [r for r in records if r.get("type") == "summary"]
    t0 = meta.get("ts") if meta and isinstance(meta.get("ts"), (int, float)) else None

    # Attach per-step records; unknown types with a step id show up under
    # "other records" instead of vanishing.
    known = {"run_meta", "run_end", "step", "summary"}
    attached: dict[Any, dict[str, list[dict[str, Any]]]] = {}
    for r in records:
        rtype = r.get("type")
        if rtype in known or r.get("step") is None:
            continue
        bucket = attached.setdefault(r["step"], {})
        key = rtype if rtype in ("browser_event", "ebrowse_log", "anomaly") else "_unknown"
        bucket.setdefault(key, []).append(r)

    # Summaries become range markers placed after their step_end row.
    markers_after: dict[Any, list[str]] = {}
    for s in summaries:
        markers_after.setdefault(s.get("step_end"), []).append(_summary_marker(s))

    body: list[str] = [_header(meta, end, anomalies, len(steps))]
    for step in steps:
        n = step.get("step")
        body.append(_step_row(step, attached.get(n, {}), reader.blobs, t0))
        body.extend(markers_after.pop(n, []))
    for leftovers in markers_after.values():  # summaries pointing past the last step
        body.extend(leftovers)

    title = f"ebrowse trace — {meta.get('task_id', run_dir.name) if meta else run_dir.name}"
    return (
        "<!doctype html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
        f"<title>{_e(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        + "\n".join(body)
        + f"\n<script>{_JS}</script>\n</body>\n</html>\n"
    )
