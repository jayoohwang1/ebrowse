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
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ebrowse_evals.trace.store import BlobStore, TraceReader

# Cap for inlined DomSnapshot JSON so a huge page can't make the HTML unopenable.
_MAX_INLINE_JSON = 200_000

_PHASE_COLORS = ["#4c8dd6", "#57a773", "#c9a227", "#b06ab3", "#d0684f", "#5aa7a7", "#8a8fd0"]
_EBROWSE_COMMAND = re.compile(r"(?:^|[;&|(]|\$\(|`)\s*(?:[\w./\-]*/)?ebrowse\b")


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def website(config: dict[str, Any], fallback_url: str = "") -> str:
    """Host the run started on: the bootstrap target when the runner recorded
    one, else the task url, else wherever the first step ended up."""
    bootstrap = config.get("navigation_bootstrap")
    url = str(bootstrap.get("requested_url") or "") if isinstance(bootstrap, dict) else ""
    url = url or str(config.get("url") or "") or fallback_url
    return (urlparse(url).netloc or url).removeprefix("www.") if url else ""


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


def _screenshot_cell(
    ref: str | None, blobs: BlobStore, blob_url: Callable[[str], str] | None = None
) -> str:
    if not ref:
        return '<div class="shot placeholder">no screenshot captured</div>'
    if blob_url is not None:
        return (
            f'<img class="shot" src="{_e(blob_url(ref))}" '
            f'alt="screenshot {_e(_short_ref(ref))}" loading="lazy">'
        )
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


def _dom_snapshot_block(
    ref: str | None, blobs: BlobStore, blob_url: Callable[[str], str] | None = None
) -> str:
    if not ref:
        return "<p class='muted'>no DomSnapshot captured</p>"
    label = f"DomSnapshot <code>{_e(_short_ref(ref))}</code>"
    if blob_url is not None:
        return (
            f'<details class="dom-lazy" data-url="{_e(blob_url(ref))}">'
            f"<summary>{label}</summary><pre class='muted'>open to load</pre></details>"
        )
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
    blob_url: Callable[[str], str] | None = None,
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
        _screenshot_cell(step.get("screenshot"), blobs, blob_url),
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
    inner.append("<h4>blobs</h4>" + _dom_snapshot_block(step.get("dom_snapshot"), blobs, blob_url))
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


_ANNOTATION_KINDS = ("verdict", "issue", "stuck_span", "vision")


def _summary_marker(rec: dict[str, Any]) -> str:
    a, b = rec.get("step_start", "?"), rec.get("step_end", "?")
    model = f" <span class='muted'>({_e(rec['model'])})</span>" if rec.get("model") else ""
    return (
        f'<div class="summary-marker">summary steps {_e(a)}&ndash;{_e(b)}{model}: '
        f"{_e(rec.get('text', ''))}</div>"
    )


def _message_content(
    content: Any,
    content_ref: str | None,
    blobs: BlobStore,
    blob_url: Callable[[str], str] | None,
) -> str:
    if content_ref:
        if blob_url is not None:
            return (
                f'<details class="content-lazy" data-url="{_e(blob_url(content_ref))}">'
                "<summary>Large message content — open to load</summary>"
                '<pre class="muted">not loaded</pre></details>'
            )
        try:
            content = json.loads(blobs.get(content_ref).decode("utf-8"))
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
            return '<p class="muted">large message content blob unavailable</p>'
    if isinstance(content, str):
        return f'<div class="message-text">{_e(content)}</div>'
    blocks: list[str] = []
    for block in content if isinstance(content, list) else [content]:
        if not isinstance(block, dict):
            blocks.append(
                f'<pre class="raw-block">{_e(json.dumps(block, ensure_ascii=False))}</pre>'
            )
            continue
        kind = block.get("type")
        if kind == "thinking":
            blocks.append(
                '<details class="thinking"><summary>Thinking</summary>'
                f'<div class="message-text">{_e(block.get("thinking", ""))}</div></details>'
            )
        elif kind == "text":
            blocks.append(f'<div class="message-text">{_e(block.get("text", ""))}</div>')
        elif kind == "toolCall":
            args = block.get("arguments") or {}
            blocks.append(
                '<div class="tool-call"><div><strong>Tool call</strong> '
                f"<code>{_e(block.get('name', '?'))}</code> "
                f'<span class="muted">{_e(block.get("id", ""))}</span></div>'
                f"<pre>{_e(json.dumps(args, indent=2, ensure_ascii=False))}</pre></div>"
            )
        else:
            blocks.append(
                f'<pre class="raw-block">{_e(json.dumps(block, indent=2, ensure_ascii=False))}</pre>'
            )
    return "".join(blocks)


def _step_details(
    step: dict[str, Any],
    attached: dict[str, list[dict[str, Any]]],
    blobs: BlobStore,
    blob_url: Callable[[str], str] | None,
    labelled: bool,
) -> str:
    """One step's internals, for the browser panel's expander."""
    browser = step.get("browser", {}) if isinstance(step.get("browser"), dict) else {}
    detail: list[str] = []
    if labelled:
        detail.append(f'<h4 class="step-detail">step {_e(step.get("step", "?"))}</h4>')
    detail.append(_timing_bar(step.get("timing", {}) or {}))
    if browser:
        detail.append("<h4>browser state</h4>" + _kv_table(browser))
    detail.append("<h4>blobs</h4>" + _dom_snapshot_block(step.get("dom_snapshot"), blobs, blob_url))
    events = attached.get("browser_event", [])
    if events:
        detail.append(
            "<h4>browser events</h4><ul class='events'>"
            + "".join(_browser_event_html(event, None) for event in events)
            + "</ul>"
        )
    logs = attached.get("ebrowse_log", [])
    if logs:
        detail.append("<h4>ebrowse log</h4>" + _log_rows(logs))
    error = step.get("error")
    if isinstance(error, dict):
        detail.append("<h4>error</h4><div class='error-box'>" + _kv_table(error) + "</div>")
    unknown = attached.get("_unknown", [])
    if unknown:
        detail.append(
            "<h4>other records</h4><pre>"
            + _e("\n".join(json.dumps(record, ensure_ascii=False) for record in unknown))
            + "</pre>"
        )
    return "".join(detail)


def _browser_panel(
    group: list[tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]],
    blobs: BlobStore,
    blob_url: Callable[[str], str] | None,
) -> str:
    """One screenshot for a chain of steps that left the page unchanged, plus
    every step's internals behind a single expander."""
    if not group:
        return (
            '<aside class="browser-side browser-empty" aria-label="No browser interaction"></aside>'
        )
    shot = next((s for s, _ in reversed(group) if s.get("screenshot")), None)
    state = next((s for s, _ in reversed(group) if s.get("browser")), None)
    browser = (
        (state or {}).get("browser", {}) if isinstance((state or {}).get("browser"), dict) else {}
    )
    numbers = [s.get("step") for s, _ in group]
    label = (
        f"step {_e(numbers[0])}"
        if len(numbers) == 1
        else f"steps {_e(numbers[0])}&ndash;{_e(numbers[-1])} <span class='muted'>unchanged</span>"
    )
    anomalies = [a for _, attached in group for a in attached.get("anomaly", [])]
    return (
        f'<aside class="browser-side"><div class="browser-label">Browser after action · {label}</div>'
        + _screenshot_cell((shot or {}).get("screenshot"), blobs, blob_url)
        + f'<div class="pageid"><strong>{_e(browser.get("title", ""))}</strong><br>'
        f'<span class="url">{_e(browser.get("url", ""))}</span></div>'
        + (f'<div class="badges">{_anomaly_badges(anomalies)}</div>' if anomalies else "")
        + "<details class='internals'><summary>browser details"
        + (f" · {len(group)} steps" if len(group) > 1 else "")
        + "</summary>"
        + "".join(
            _step_details(step, attached, blobs, blob_url, labelled=len(group) > 1)
            for step, attached in group
        )
        + "</details></aside>"
    )


def _has_browser(step: dict[str, Any] | None) -> bool:
    """Whether a step deserves a place in the browser lane at all."""
    return step is not None and bool(
        step.get("browser")
        or step.get("screenshot")
        or step.get("dom_snapshot")
        or (
            step.get("tool_name") in (None, "bash")
            and _EBROWSE_COMMAND.search(str(step.get("command", "")))
        )
    )


def _conversation_row(
    message: dict[str, Any],
    step: dict[str, Any] | None,
    blobs: BlobStore,
    blob_url: Callable[[str], str] | None,
) -> str:
    role = str(message.get("role", "message"))
    label = "Starting prompt" if message.get("is_start") else role
    turn = f" · turn {_e(message['turn'])}" if message.get("turn") is not None else ""
    meta: list[str] = []
    usage = message.get("usage")
    if isinstance(usage, dict) and usage:
        meta.append(" / ".join(f"{_e(k)} {_e(v)}" for k, v in usage.items() if k != "cost"))
    if message.get("stop_reason"):
        meta.append(f"stop {_e(message['stop_reason'])}")
    if message.get("is_error"):
        meta.append("error")
    metadata = message.get("metadata")
    anchor = f' id="step-{_e(step.get("step"))}"' if step else ""
    start = " is-start" if message.get("is_start") else ""
    return (
        f'<section class="conversation-row role-{_e(role)}{start}"{anchor}>'
        '<article class="conversation-main">'
        f'<div class="message-head"><strong>{_e(label)}</strong>{turn} '
        f'<span class="muted">{_e(message.get("message_id", ""))}</span></div>'
        + _message_content(
            message.get("content"),
            message.get("content_ref"),
            blobs,
            None if message.get("is_start") else blob_url,
        )
        + (f'<div class="stats">{" &middot; ".join(meta)}</div>' if meta else "")
        + (
            '<details class="message-metadata"><summary>message metadata</summary><pre>'
            + _e(json.dumps(metadata, indent=2, ensure_ascii=False))
            + "</pre></details>"
            if isinstance(metadata, dict) and metadata
            else ""
        )
        + "</article></section>"
    )


def _prompt_text(
    prompt: dict[str, Any], blobs: BlobStore, blob_url: Callable[[str], str] | None
) -> str:
    ref = prompt.get("text_ref")
    if not ref:
        return f"<pre>{_e(prompt.get('text', ''))}</pre>"
    if blob_url is not None:
        return f'<pre class="prompt-lazy muted" data-url="{_e(blob_url(ref))}">open to load</pre>'
    try:
        return f"<pre>{_e(blobs.get(ref).decode('utf-8'))}</pre>"
    except (FileNotFoundError, UnicodeDecodeError):
        return '<pre class="muted">prompt blob unavailable</pre>'


def _prompt_panels(
    prompts: list[dict[str, Any]],
    blobs: BlobStore,
    blob_url: Callable[[str], str] | None,
) -> str:
    systems = [prompt for prompt in prompts if prompt.get("kind") == "system"]
    if not systems:
        return ""
    return (
        '<div class="system-prompts">'
        + "".join(
            '<details class="system-prompt"><summary>Effective system prompt'
            + (f" · revision {_e(prompt.get('sequence'))}" if len(systems) > 1 else "")
            + f' <span class="muted">sha256:{_e(str(prompt.get("sha256", ""))[:12])}</span></summary>'
            + _prompt_text(prompt, blobs, blob_url)
            + "</details>"
            for prompt in systems
        )
        + "</div>"
    )


def _header(
    meta: dict[str, Any] | None,
    end: dict[str, Any] | None,
    anomalies: list[dict[str, Any]],
    n_steps: int,
    site: str = "",
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
    # The instruction is what identifies a run to a human; the task id is a
    # hash, so it drops to the facts table.
    instruction = str(meta.get("prompt") or "").strip() or str(meta.get("task_id", "trace"))
    site_line = f'<span class="site">{_e(site)}</span>' if site else ""
    return f"""<header>
<div class="run-line">{site_line}<span class="outcome outcome-{_e(outcome)}">{_e(outcome)}</span></div>
<h1>{_e(instruction)}</h1>
<details class="facts"><summary>run details</summary><table class="kv">{fact_rows}</table>
{config_block}</details>
<div class="totals">{n_steps} steps &middot; {totals_line}</div>
{eval_line}
<details class="anomaly-list"><summary>anomalies ({len(anomalies)})</summary>
<ul>{anomaly_items}</ul></details>
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
header h1 { margin:.2rem 0 .5rem; line-height:1.3; }
.run-line { display:flex; align-items:center; gap:.6rem; }
.run-line .site { font-weight:600; color:var(--accent); }
.facts summary,.anomaly-list summary { cursor:pointer; color:var(--muted); font-size:.85rem; }
.facts { margin:.2rem 0; } .anomaly-list { margin-top:.5rem; }
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
.overview { border:1px solid var(--line); border-radius:8px; background:var(--panel);
  padding:.7rem 1rem; margin:1rem 0; }
.overview h3 { margin:0 0 .4rem; font-size:.8rem; text-transform:uppercase; letter-spacing:.04em;
  color:var(--muted); }
.overview .verdict { margin:.2rem 0; font-size:1.02rem; }
.seg-controls { display:flex; align-items:center; gap:.6rem; margin-top:.6rem; }
.seg-toggle { border:1px solid var(--line); background:transparent; color:var(--fg);
  border-radius:6px; padding:.2rem .6rem; font-size:.8rem; cursor:pointer; }
.segment { border:1px solid var(--line); border-radius:8px; margin:.5rem 0; background:var(--bg); }
.segment[open] { border-color:var(--accent); }
.seg-head { cursor:pointer; padding:.55rem .8rem; list-style:none; }
.seg-head::-webkit-details-marker { display:none; }
.seg-head::before { content:"▸"; color:var(--muted); margin-right:.5rem; }
.segment[open] > .seg-head::before { content:"▾"; }
.seg-range { font-weight:600; margin-right:.5rem; }
.seg-finding { margin:.3rem 0 0 1.2rem; font-size:.87rem; }
.seg-badge { display:inline-block; border:1px solid var(--line); border-radius:9px;
  padding:.02rem .45rem; font-size:.72rem; margin-right:.35rem; color:var(--muted); }
.seg-badge.seg-high { border-color:var(--bad); color:var(--bad); background:#c04a3a14; }
.seg-stuck_span { border-color:var(--warn); color:var(--warn); }
.seg-vision { border-color:var(--accent); color:var(--accent); }
.seg-body { padding:0 .8rem; border-top:1px solid var(--line); }
.error-box { border:1px solid var(--bad); border-radius:6px; padding:.3rem .6rem; }
.muted { color:var(--muted); }
.back { margin:.2rem 0 1rem; } .back a { color:var(--accent); text-decoration:none; }
.back a:hover { text-decoration:underline; }
.compact-chunk { border-bottom:1px solid var(--line); padding:.7rem 0; }
.compact-head { margin-bottom:.35rem; }
.load-chunk { border:1px solid var(--accent); color:var(--accent); background:transparent;
  border-radius:6px; padding:.25rem .55rem; cursor:pointer; }
.compact-step { display:grid; grid-template-columns:5.5rem minmax(8rem,18rem) minmax(8rem,1fr) minmax(10rem,1.3fr);
  gap:.65rem; align-items:baseline; padding:.2rem .45rem; font-size:.8rem; }
.compact-step:nth-child(even) { background:var(--panel); }
.compact-page,.compact-thought { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.compact-page { color:var(--muted); } .compact-step code { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
@media (max-width:800px) { .compact-step { grid-template-columns:4.5rem 1fr; }
  .compact-thought,.compact-step code { grid-column:2; } }
.system-prompts { margin:.8rem 0 1rem; }
.system-prompt { background:var(--panel); border:1px solid var(--line); border-radius:6px;
  padding:.4rem .65rem; }
.system-prompt summary { cursor:pointer; font-weight:600; }
/* One page = one group: rows on the left, a single screenshot on the right
   that sticks while you scroll the chain of calls made against that page. */
.page-group { display:grid; grid-template-columns:minmax(0,1fr) 340px; gap:1.2rem;
  align-items:start; border-bottom:2px solid var(--line); }
.page-group.no-browser { grid-template-columns:minmax(0,1fr); }
.group-main { min-width:0; }
.conversation-row { padding:.7rem 0; }
.conversation-row + .conversation-row { border-top:1px dashed var(--line); }
.conversation-main { min-width:0; }
.browser-side { min-width:0; position:sticky; top:.5rem; padding:1rem 0 1rem 1.2rem;
  border-left:1px solid var(--line); }
.step-detail { color:var(--fg); border-top:1px solid var(--line); padding-top:.4rem; }
.message-head,.browser-label { color:var(--muted); font-size:.8rem; margin-bottom:.45rem; }
.message-head strong { color:var(--fg); text-transform:capitalize; }
.message-text { white-space:pre-wrap; overflow-wrap:anywhere; margin:.35rem 0; }
.thinking { background:var(--panel); border-radius:6px; padding:.35rem .6rem; margin:.4rem 0; }
.thinking summary { cursor:pointer; color:var(--muted); }
.tool-call { border-left:3px solid var(--accent); padding:.4rem .65rem; margin:.5rem 0;
  background:var(--panel); border-radius:0 6px 6px 0; }
.tool-call pre,.raw-block { max-height:30rem; }
.role-toolResult .conversation-main { background:color-mix(in srgb,var(--panel) 45%,transparent); padding:.8rem; }
/* The starting prompt embeds the whole operating guide; keep it in place and
   in full, but scrolling, so it can't push the trajectory off the screen. */
.is-start .message-text { max-height:16rem; overflow-y:auto; background:var(--panel);
  border-radius:6px; padding:.5rem .7rem; }
@media (max-width:800px) { .page-group { grid-template-columns:minmax(0,1fr) 180px; gap:.6rem; }
  .browser-side { padding-left:.6rem; } }
"""

_JS = """
document.getElementById('show-debug').addEventListener('change', function () {
  document.body.classList.toggle('show-debug', this.checked);
});
document.addEventListener('toggle', async function (event) {
  const box = event.target.closest?.('.dom-lazy,.content-lazy');
  if (box && box.open && !box.dataset.loaded) {
    box.dataset.loaded = '1'; const pre = box.querySelector('pre');
    try { const response = await fetch(box.dataset.url); pre.textContent = await response.text(); }
    catch (error) { pre.textContent = 'failed to load content: ' + error; }
  }
  const system = event.target.closest?.('.system-prompt');
  const prompt = system?.querySelector('.prompt-lazy');
  if (system?.open && prompt && !prompt.dataset.loaded) {
    prompt.dataset.loaded = '1';
    try { const response = await fetch(prompt.dataset.url); prompt.textContent = await response.text();
      prompt.classList.remove('muted'); }
    catch (error) { prompt.textContent = 'failed to load system prompt: ' + error; }
  }
}, true);
document.addEventListener('click', function (event) {
  const toggle = event.target.closest?.('.seg-toggle');
  if (!toggle) return;
  const open = toggle.dataset.open === '1';
  document.querySelectorAll('.segment').forEach(function (segment) { segment.open = open; });
});
// An anchor (anomaly list, "#step-N") inside a collapsed segment cannot be
// scrolled to, so open its ancestors first.
function revealHash() {
  const target = location.hash && document.getElementById(location.hash.slice(1));
  if (!target) return;
  for (let node = target.parentElement; node; node = node.parentElement) {
    if (node.tagName === 'DETAILS') node.open = true;
  }
  target.scrollIntoView();
}
window.addEventListener('hashchange', revealHash);
revealHash();
document.addEventListener('click', async function (event) {
  const button = event.target.closest?.('.load-chunk');
  if (!button) return;
  button.disabled = true; button.textContent = 'Loading…';
  try {
    const response = await fetch(button.dataset.url);
    if (!response.ok) throw new Error('HTTP ' + response.status);
    button.closest('.compact-chunk').outerHTML = await response.text();
  } catch (error) {
    button.disabled = false; button.textContent = 'Retry expanding steps';
  }
});
"""


def _attachments(records: list[dict[str, Any]]) -> dict[Any, dict[str, list[dict[str, Any]]]]:
    known = {"run_meta", "run_end", "step", "summary"}
    attached: dict[Any, dict[str, list[dict[str, Any]]]] = {}
    for record in records:
        rtype = record.get("type")
        if rtype in known or record.get("step") is None:
            continue
        bucket = attached.setdefault(record["step"], {})
        key = rtype if rtype in ("browser_event", "ebrowse_log", "anomaly") else "_unknown"
        bucket.setdefault(key, []).append(record)
    return attached


def _short_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact_step(step: dict[str, Any]) -> str:
    browser_value = step.get("browser")
    browser: dict[str, Any] = browser_value if isinstance(browser_value, dict) else {}
    page = browser.get("title") or browser.get("url") or "unknown page"
    thought = _short_text(step.get("agent_text"), 180)
    action = _short_text(step.get("command"), 220)
    thought_html = f'<span class="compact-thought">{_e(thought)}</span>' if thought else ""
    return (
        f'<div class="compact-step"><strong>step {_e(step.get("step", "?"))}</strong>'
        f'<span class="compact-page">{_e(_short_text(page, 100))}</span>'
        f"{thought_html}<code>{_e(action)}</code></div>"
    )


# -- page groups ------------------------------------------------------------
#
# A chain of tool calls that leaves the page untouched (expand, a failed
# action, a non-browser tool) produced one identical screenshot per step, and
# each of those rows reserved a full screenshot's worth of height. Consecutive
# steps whose capture is byte-identical -- the blob store is content-addressed,
# so an equal ref IS an unchanged page -- now share a single sticky panel, and
# the next page-changing action starts a visibly separate group.


@dataclass(slots=True)
class _Row:
    step: int | None
    html: str
    record: dict[str, Any] | None = None
    attached: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _page_key(record: dict[str, Any] | None) -> str | None:
    """Identity of the page a step left behind: its screenshot blob ref.
    None means "nothing to show" -- the row joins whatever group it is in."""
    if record is None or not _has_browser(record):
        return None
    ref = record.get("screenshot")
    return str(ref) if ref else None


def _page_groups(rows: list[_Row], blobs: BlobStore, blob_url: Callable[[str], str] | None) -> str:
    """Wrap consecutive rows sharing a page into one two-column group."""
    out: list[str] = []
    pending: list[_Row] = []
    key: str | None = None

    def flush() -> None:
        nonlocal pending, key
        if not pending:
            return
        panel = [(row.record, row.attached) for row in pending if row.record is not None]
        aside = _browser_panel(panel, blobs, blob_url) if key is not None else ""
        css = "page-group" if key is not None else "page-group no-browser"
        out.append(
            f'<section class="{css}"><div class="group-main">'
            + "".join(row.html for row in pending)
            + f"</div>{aside}</section>"
        )
        pending, key = [], None

    for row in rows:
        this = _page_key(row.record)
        if this is not None and key is not None and this != key:
            flush()
        pending.append(row)
        key = key or this
    flush()
    return "".join(out)


# -- annotation-driven segmentation ----------------------------------------
#
# The model's span annotations (issue / stuck_span / vision) cut the trajectory
# into a handful of stretches. Every cut point becomes a segment boundary, so
# overlapping spans (a vision finding straddling two issues) split rather than
# merge, and every segment lists all annotations overlapping it. The stretches
# nobody annotated stay as unlabelled segments -- the run is still covered
# end to end, just quietly.


def _span(rec: dict[str, Any]) -> tuple[int, int] | None:
    lo, hi = rec.get("step_start"), rec.get("step_end")
    if not isinstance(lo, int) or not isinstance(hi, int) or hi < lo:
        return None
    return lo, hi


def _segments(
    spans: list[dict[str, Any]], lo: int, hi: int
) -> list[tuple[int, int, list[dict[str, Any]]]]:
    """Partition [lo, hi] on every annotation edge; attach overlapping spans."""
    cuts = {lo, hi + 1}
    for rec in spans:
        bounds = _span(rec)
        if bounds is None:
            continue
        a, b = bounds
        if lo <= a <= hi:
            cuts.add(a)
        if lo <= b < hi:
            cuts.add(b + 1)
    edges = sorted(cuts)
    out: list[tuple[int, int, list[dict[str, Any]]]] = []
    for start, nxt in zip(edges, edges[1:], strict=False):
        end = nxt - 1
        overlapping = [
            rec
            for rec in spans
            if (bounds := _span(rec)) is not None and bounds[0] <= end and bounds[1] >= start
        ]
        out.append((start, end, overlapping))
    return out


_SEG_BADGES = {"stuck_span": "stuck", "vision": "vision"}


def _annotation_line(rec: dict[str, Any], segment: tuple[int, int]) -> str:
    kind = str(rec.get("kind", ""))
    label = rec.get("category") or _SEG_BADGES.get(kind, kind)
    severity = str(rec.get("severity") or "")
    classes = f"seg-badge seg-{_e(kind)}" + (" seg-high" if severity == "high" else "")
    badge = f'<span class="{classes}">{_e(label)}</span>'
    bounds = _span(rec)
    # Only worth repeating when the finding spans more than this segment; a
    # wide span states itself once, then thins to a marker on the segments it
    # continues through (a run-wide issue would otherwise shout on every one).
    steps = (
        f'<span class="muted">steps {bounds[0]}&ndash;{bounds[1]}</span> '
        if bounds is not None and bounds != segment
        else ""
    )
    body = (
        '<span class="muted">continues</span>'
        if bounds is not None and bounds[0] < segment[0]
        else _e(_short_text(rec.get("text"), 400))
    )
    return f'<div class="seg-finding">{badge} {steps}{body}</div>'


def _segment_head(start: int, end: int, findings: list[dict[str, Any]]) -> str:
    count = end - start + 1
    label = f"steps {start}&ndash;{end}" if end > start else f"step {start}"
    if not findings:
        return (
            f'<summary class="seg-head"><span class="seg-range">{label}</span> '
            f'<span class="muted">{count} step{"s" if count != 1 else ""} · no findings</span>'
            "</summary>"
        )
    return (
        f'<summary class="seg-head"><span class="seg-range">{label}</span> '
        f'<span class="muted">{count} step{"s" if count != 1 else ""}</span>'
        + "".join(_annotation_line(rec, (start, end)) for rec in findings)
        + "</summary>"
    )


def _overview(verdicts: list[dict[str, Any]], n_segments: int, n_steps: int) -> str:
    lines = "".join(
        f'<p class="verdict">{_e(rec.get("text", ""))}</p>'
        + (f'<p class="muted">annotated by {_e(rec["model"])}</p>' if rec.get("model") else "")
        for rec in verdicts
    )
    return (
        '<section class="overview"><h3>overview</h3>'
        + (lines or '<p class="muted">no verdict recorded</p>')
        + f'<div class="seg-controls"><span class="muted">{n_segments} segments · '
        f"{n_steps} steps</span>"
        '<button type="button" class="seg-toggle" data-open="1">Expand all</button>'
        '<button type="button" class="seg-toggle" data-open="">Collapse all</button></div>'
        "</section>"
    )


def _segmented_body(
    rows: list[_Row],
    segments: list[tuple[int, int, list[dict[str, Any]]]],
    render: Callable[[list[_Row]], str],
) -> list[str]:
    """Wrap step-bound rows in their segment's <details>; rows before the first
    and after the last step (starting prompt, final answer) stay visible."""
    numbered = [i for i, row in enumerate(rows) if row.step is not None]
    if not numbered:
        return [render(rows)]
    first, last = numbered[0], numbered[-1]
    # A row with no step of its own (assistant thinking, a tool call) belongs
    # with the step it leads into, not the one it followed.
    owners: list[int] = []
    nxt = rows[last].step
    for row in reversed(rows[first : last + 1]):
        nxt = row.step if row.step is not None else nxt
        owners.append(nxt if nxt is not None else 0)
    owners.reverse()

    out = [render(rows[:first])] if first else []
    middle = rows[first : last + 1]
    cursor = 0
    for index, (start, end, findings) in enumerate(segments):
        taken: list[_Row] = []
        while cursor < len(middle) and (
            owners[cursor] <= end or index == len(segments) - 1  # never drop a row
        ):
            taken.append(middle[cursor])
            cursor += 1
        out.append(
            f'<details class="segment" id="segment-{start}">'
            + _segment_head(start, end, findings)
            + f'<div class="seg-body">{render(taken)}</div></details>'
        )
    if rows[last + 1 :]:
        out.append(render(rows[last + 1 :]))
    return out


def render_step_fragment(
    run_dir: Path,
    offset: int,
    limit: int,
    blob_url: Callable[[str], str] | None = None,
) -> str:
    """Render a bounded full-detail step slice for server-side lazy expansion."""
    reader = TraceReader(run_dir)
    records = list(reader.raw())
    meta = next((r for r in records if r.get("type") == "run_meta"), None)
    t0 = meta.get("ts") if meta and isinstance(meta.get("ts"), (int, float)) else None
    steps = [r for r in records if r.get("type") == "step"]
    attached = _attachments(records)
    return "".join(
        _step_row(step, attached.get(step.get("step"), {}), reader.blobs, t0, blob_url)
        for step in steps[offset : offset + limit]
    )


def render_run(
    run_dir: Path,
    back_href: str | None = None,
    blob_url: Callable[[str], str] | None = None,
    fragment_url: Callable[[int, int], str] | None = None,
    compact_middle: bool = False,
) -> str:
    """Render a run directory to a single self-contained HTML page."""
    reader = TraceReader(run_dir)  # raises FileNotFoundError -> CLI reports it
    records = list(reader.raw())

    meta = next((r for r in records if r.get("type") == "run_meta"), None)
    end = next((r for r in records if r.get("type") == "run_end"), None)
    steps = [r for r in records if r.get("type") == "step"]
    messages = sorted(
        (r for r in records if r.get("type") == "agent_message"),
        key=lambda record: record.get("sequence", 0),
    )
    prompts = [r for r in records if r.get("type") == "prompt_snapshot"]
    anomalies = [r for r in records if r.get("type") == "anomaly"]
    summaries = [r for r in records if r.get("type") == "summary"]
    t0 = meta.get("ts") if meta and isinstance(meta.get("ts"), (int, float)) else None

    # Attach per-step records; unknown types with a step id show up under
    # "other records" instead of vanishing.
    attached = _attachments(records)

    # Model annotations drive the segmented overview; plain step-range
    # summaries keep their inline markers.
    annotations = [s for s in summaries if s.get("kind") in _ANNOTATION_KINDS]
    verdicts = [s for s in annotations if s.get("kind") == "verdict"]
    spans = [s for s in annotations if s.get("kind") != "verdict"]
    markers_after: dict[Any, list[str]] = {}
    for s in summaries:
        if s not in annotations:
            markers_after.setdefault(s.get("step_end"), []).append(_summary_marker(s))

    step_numbers = [s["step"] for s in steps if isinstance(s.get("step"), int)]
    segments = (
        _segments(spans, min(step_numbers), max(step_numbers)) if spans and step_numbers else []
    )

    head: list[str] = []
    if back_href:
        head.append(f'<nav class="back"><a href="{_e(back_href)}">&larr; all runs</a></nav>')
    first_url = next(
        (str((s.get("browser") or {}).get("url") or "") for s in steps if s.get("browser")), ""
    )
    site = website(meta.get("config", {}) if meta else {}, first_url)
    head.append(_header(meta, end, anomalies, len(steps), site))
    head.append(_prompt_panels(prompts, reader.blobs, blob_url))
    if annotations:
        head.append(_overview(verdicts, len(segments), len(steps)))

    # The segmenter groups rows by step; rows belonging to no step (assistant
    # turns, chunk placeholders) ride along with the step they lead into.
    rows: list[_Row] = []

    def emit(html: str, step: Any = None, record: dict[str, Any] | None = None) -> None:
        number = step if isinstance(step, int) else None
        rows.append(_Row(number, html, record, attached.get(number, {}) if record else {}))

    def full_step(step: dict[str, Any]) -> None:
        n = step.get("step")
        emit(_step_row(step, attached.get(n, {}), reader.blobs, t0, blob_url), n)
        for marker in markers_after.pop(n, []):
            emit(marker, n)

    if messages:
        start_prompt = next((p for p in prompts if p.get("kind") == "start"), None)
        if start_prompt and not any(message.get("is_start") for message in messages):
            start_text = str(start_prompt.get("text", ""))
            if start_prompt.get("text_ref"):
                try:
                    start_text = reader.blobs.get(str(start_prompt["text_ref"])).decode("utf-8")
                except (FileNotFoundError, UnicodeDecodeError):
                    start_text = "starting prompt blob unavailable"
            messages.insert(
                0,
                {
                    "type": "agent_message",
                    "sequence": 0,
                    "message_id": "harness-start-prompt",
                    "role": "user",
                    "content": [{"type": "text", "text": start_text}],
                    "is_start": True,
                },
            )
        by_call = {
            str(step.get("tool_call_id")): step
            for step in steps
            if step.get("tool_call_id") is not None
        }
        rendered_steps: set[Any] = set()
        for message in messages:
            step = (
                by_call.get(str(message.get("tool_call_id")))
                if message.get("tool_call_id")
                else None
            )
            if step is not None:
                rendered_steps.add(step.get("step"))
            emit(
                _conversation_row(message, step, reader.blobs, blob_url),
                step.get("step") if step else None,
                step,
            )
            if step is not None:
                for marker in markers_after.pop(step.get("step"), []):
                    emit(marker, step.get("step"))
        for step in steps:
            if step.get("step") in rendered_steps:
                continue
            fallback = {
                "role": "toolResult",
                "message_id": step.get("tool_call_id") or f"legacy-step-{step.get('step')}",
                "content": [{"type": "text", "text": step.get("output", "")}],
                "tool_call_id": step.get("tool_call_id"),
                "tool_name": step.get("tool_name"),
                "is_error": bool(step.get("error")),
            }
            emit(
                _conversation_row(fallback, step, reader.blobs, blob_url),
                step.get("step"),
                step,
            )
            for marker in markers_after.pop(step.get("step"), []):
                emit(marker, step.get("step"))
    elif compact_middle and fragment_url is not None and len(steps) > 35:
        for step in steps[:25]:
            full_step(step)
        middle_end = len(steps) - 10
        for offset in range(25, middle_end, 10):
            chunk = steps[offset : min(offset + 10, middle_end)]
            first, last = chunk[0].get("step", "?"), chunk[-1].get("step", "?")
            emit(
                f'<section class="compact-chunk"><div class="compact-head">'
                f'<button class="load-chunk" data-url="{_e(fragment_url(offset, len(chunk)))}">'
                f"Expand steps {_e(first)}–{_e(last)}</button></div>"
                + "".join(_compact_step(step) for step in chunk)
                + "</section>",
                chunk[0].get("step"),
            )
        for step in steps[-10:]:
            full_step(step)
    else:
        for step in steps:
            full_step(step)
    for leftovers in markers_after.values():  # summaries pointing past the last step
        for marker in leftovers:
            emit(marker)

    # Page grouping is a property of the conversation layout; legacy step rows
    # carry their own screenshot lane.
    def render(chunk: list[_Row]) -> str:
        if messages:
            return _page_groups(chunk, reader.blobs, blob_url)
        return "".join(row.html for row in chunk)

    body = head + (_segmented_body(rows, segments, render) if segments else [render(rows)])
    title = f"ebrowse trace — {meta.get('task_id', run_dir.name) if meta else run_dir.name}"
    return (
        "<!doctype html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
        f"<title>{_e(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        + "\n".join(body)
        + f"\n<script>{_JS}</script>\n</body>\n</html>\n"
    )
