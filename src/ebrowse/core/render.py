"""Renderers: PageMem -> outline text, Section -> markdown. FROZEN formats (§4).

Format changes require updating tests/golden/ and DESIGN.md in the same commit.
Provenance markers: '≈' = LLM summary (model-paraphrased page content, untrusted),
'|' = deterministic label (verbatim page text, quoted).
"""

from __future__ import annotations

from ebrowse.config import ObserveConfig
from ebrowse.core.label import deterministic_label
from ebrowse.core.snapshot import DomNode
from ebrowse.core.split import RawSection
from ebrowse.model import Diff, Element, PageMem, Section

_HEADING_LEVEL = {"h1": 2, "h2": 3, "h3": 3, "h4": 4, "h5": 4, "h6": 4}
_BLOCK_TAGS = {"p", "li", "tr", "blockquote", "pre", "dt", "dd", "figcaption"}
_MAX_SELECT_OPTIONS_INLINE = 15


def fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"~{n / 1000:.1f}kt"
    return f"~{n}t"


# ---------------------------------------------------------------- outline ----


def render_outline(page: PageMem, summaries_note: str | None = None) -> str:
    lines = [f"PAGE {page.title} — {page.url}" if page.title else f"PAGE {page.url}"]
    for s in page.sections:
        lines.append(outline_line(s))
    if summaries_note:
        lines.append(summaries_note)
    return "\n".join(lines)


def outline_line(s: Section) -> str:
    if s.cross_origin:
        return f"{s.sid} iframe  ({s.preview})"
    counts = s.counts_desc()
    tok = fmt_tokens(s.token_estimate)
    if s.summary:
        label = f"≈ {s.summary}"
    else:
        det = deterministic_label(s.heading, s.preview)
        label = f'| "{det}"' if det else "|"
    return f"{s.sid} {s.type:<7} {counts}  {tok}  {label}"


# ----------------------------------------------------------------- expand ----


def render_section_markdown(
    section: Section,
    raw: RawSection,
    observe: ObserveConfig,
    cursor: int = 0,
    show_all: bool = False,
) -> str:
    head = f"## {section.sid} {section.type}"
    if section.heading:
        head += f" — {section.heading}"
    if section.cross_origin:
        return f"{head}\n(cross-origin iframe: {section.preview} — content not accessible; try `screenshot --section {section.sid}`)"

    if section.type in ("list", "table") and _items_of(raw):
        body = _render_items(section, raw, observe, cursor, show_all)
    else:
        body = "\n".join(_blocks(raw, skip_heading=section.heading))
    return f"{head}\n{body}".rstrip()


def _items_of(raw: RawSection) -> list[DomNode]:
    node = raw.node
    if node.is_list_group:
        return node.children
    if node.tag in ("ul", "ol", "dl"):
        return [c for c in node.children if c.tag in ("li", "dt", "dd")]
    if node.tag == "table":
        for n in node.walk():
            if n.tag == "tbody":
                return [c for c in n.children if c.tag == "tr"]
        return [n for n in node.walk() if n.tag == "tr"]
    return []


def _render_items(
    section: Section,
    raw: RawSection,
    observe: ObserveConfig,
    cursor: int,
    show_all: bool,
) -> str:
    items = _items_of(raw)
    total = len(items)
    window = items if show_all else items[cursor : cursor + observe.list_page_size]
    lines: list[str] = []

    if section.type == "table":
        header_cells = _table_header(raw.node)
        if header_cells:
            lines.append("| # | " + " | ".join(header_cells) + " |")
            lines.append("|---" * (len(header_cells) + 1) + "|")
        for i, row in enumerate(window, start=cursor + 1):
            cells = [c for c in row.children if c.tag in ("td", "th")]
            rendered = " | ".join(_inline(c) or " " for c in cells)
            lines.append(f"| {i} | {rendered} |")
    else:
        if not show_all and cursor:
            lines.append(f"(items {cursor + 1}–{min(cursor + len(window), total)} of {total})")
        for i, item in enumerate(window, start=cursor + 1):
            lines.append(f"{i}. {_inline(item)}")

    shown_end = total if show_all else cursor + len(window)
    if shown_end < total:
        lines.append(
            f"… {total - shown_end} more items — expand {section.sid} --cursor {shown_end}"
        )
    return "\n".join(lines)


def _table_header(table: DomNode) -> list[str]:
    for n in table.walk():
        if n.tag == "tr":
            ths = [c for c in n.children if c.tag == "th"]
            if ths:
                return [_inline(th) or " " for th in ths]
    return []


# ------------------------------------------------------------ node -> md ----


def _element_md(node: DomNode) -> str:
    """Inline markdown for a node that carries a ref (an interactive element)."""
    a = node.attrs
    ref = node.ref
    tag = node.tag
    if tag == "a" or (a.get("role") == "link"):
        text = a.get("nm") or node.subtree_text(cap=120) or a.get("href", "link")
        href = a.get("href")
        return (
            f"[{_clip(text, 80)} ({ref})](→ {_clip(href, 100)})"
            if href
            else f"[{_clip(text, 80)} ({ref})]"
        )
    if tag == "select":
        label = a.get("nm") or "select"
        opts = a.get("opt") or []
        sel = a.get("sel", "")
        if opts and len(opts) <= _MAX_SELECT_OPTIONS_INLINE:
            return f'[{label} ({ref}) ▾ "{sel}"] options: {" | ".join(opts)}'
        return f'[{label} ({ref}) ▾ "{sel}" of {len(opts)} options]'
    if tag in ("input", "textarea") or a.get("con"):
        typ = a.get("typ", "text")
        label = a.get("nm") or a.get("ph") or typ
        if typ in ("checkbox", "radio"):
            mark = "x" if a.get("chk") else " "
            return f"[{mark}] {label} ({ref})"
        if typ in ("submit", "button", "reset"):
            return f"[{a.get('val') or label} ({ref})]"
        val = a.get("val", "")
        shown = f'"{_clip(val, 60)}"' if val else "empty"
        req = ", required" if a.get("req") else ""
        return f"[{label} ({ref}: {shown}{req})]"
    # buttons and everything else clickable
    text = a.get("nm") or node.subtree_text(cap=120) or a.get("ttl") or tag
    state = ""
    if "exp" in a:
        state = " expanded" if a.get("exp") else " collapsed"
    return f"[{_clip(text, 80)} ({ref}){state}]"


def _clip(s: str | None, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _img_md(node: DomNode) -> str:
    label = node.attrs.get("cap") or node.attrs.get("alt")  # cap: VLM caption (≈)
    mark = "≈" if node.attrs.get("cap") else ""
    body = f"{mark}{_clip(label, 60)}" if label else "image"
    return f"![{body}]({node.ref})" if node.ref else (f"![{body}]" if label else "")


def _inline(node: DomNode) -> str:
    """Aggregate a subtree into one inline string."""
    if node.tag == "img":
        md = _img_md(node)
        return md
    if node.ref:
        return _element_md(node)
    parts: list[str] = []
    # <label> text is already carried as the associated control's accessible
    # name; rendering it again would double every form line. Children (the
    # controls themselves) still render. Labels with neither `for` nor a
    # control inside lose their text — acceptable, they label nothing.
    skip_text = node.tag == "label"
    if node.text and not skip_text:
        parts.append(node.text)
    for c in node.children:
        sub = _inline(c)
        if sub:
            parts.append(sub)
    return " ".join(p for p in parts if p).strip()


def _blocks(raw: RawSection, skip_heading: str | None = None) -> list[str]:
    """Block-level markdown for content/form/nav sections."""
    lines: list[str] = []
    heading_skipped = False

    def rec(node: DomNode) -> None:
        nonlocal heading_skipped
        if node.tag == "img":  # before the ref check: @i refs are not elements
            lines.append(_img_md(node) or "![image]")
            return
        if node.ref:
            _append_inline(lines, _element_md(node))
            return
        if node.tag == "label":  # see _inline: label text lives on its control
            for c in node.children:
                rec(c)
            return
        level = _HEADING_LEVEL.get(node.tag)
        if level:
            text = node.subtree_text(cap=200)
            # the section header already shows this heading; don't repeat it
            if text and not heading_skipped and skip_heading and text.startswith(skip_heading):
                heading_skipped = True
                return
            if text:
                lines.append("")
                lines.append(f"{'#' * level} {text}")
                lines.append("")
            return
        is_block = node.tag in _BLOCK_TAGS
        if is_block:
            text = _inline(node)
            if text:
                lines.append(text)
            return
        if node.text:
            _append_inline(lines, node.text)
        for c in node.children:
            rec(c)

    for n in raw.all_nodes():
        rec(n)
    # collapse runs of blank lines
    out: list[str] = []
    for line in lines:
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    return out


def _append_inline(lines: list[str], text: str) -> None:
    """Join short inline fragments onto the current line (nav links, labels)."""
    if lines and lines[-1] != "" and not lines[-1].startswith("#") and len(lines[-1]) < 100:
        lines[-1] = f"{lines[-1]} {text}"
    else:
        lines.append(text)


# ------------------------------------------------------------------- diff ----


def element_inline(e: Element) -> str:
    """Inline rendering of a model Element (used by diffs, where no DomNode
    subtree is at hand). Mirrors _element_md's shapes from descriptor+state."""
    d, s = e.desc, e.state
    label = d.name or d.text_head or d.placeholder or d.tag
    label = _clip(label, 60)
    if d.tag == "a" or d.role == "link":
        return f"[{label} ({e.ref})](→ {_clip(d.href, 80)})" if d.href else f"[{label} ({e.ref})]"
    if d.input_type in ("checkbox", "radio"):
        mark = "x" if s.checked else " "
        return f"[{mark}] {label} ({e.ref})"
    if d.tag == "select":
        return f'[{label} ({e.ref}) ▾ "{s.value or ""}"]'
    if d.tag in ("input", "textarea"):
        shown = f'"{_clip(s.value, 40)}"' if s.value else "empty"
        return f"[{label} ({e.ref}: {shown})]"
    return f"[{label} ({e.ref})]"


_MAX_DIFF_ELEMENTS = 12


def render_diff(action_line: str, diff: Diff, observe: ObserveConfig | None = None) -> str:
    """§4.3 action-result rendering. `action_line` is 'CLICK @e42 (button "…")'.
    Compound verbs pass multi-line action_lines (header + step lines); the
    outcome arrow always goes on the FIRST line."""
    del observe  # reserved for future pagination of huge diffs
    head, _, steps = action_line.partition("\n")
    out: list[str] = []

    if diff.kind == "navigation":
        out.append(f"{head} → navigation")
        if steps:
            out.append(steps)
        page = diff.new_page
        assert page is not None
        unchanged = set(diff.unchanged_sids)
        out.append(f"PAGE {page.title} — {page.url}" if page.title else f"PAGE {page.url}")
        for s in page.sections:
            if s.sid in unchanged:
                out.append(f"{s.sid} {s.type:<7} (unchanged)")
            else:
                out.append(outline_line(s))
    elif diff.kind == "no_change":
        out.append(f"{head} → no change detected")
        if steps:
            out.append(steps)
        out.append(
            "(page DOM and URL unchanged after settle — the action may have been a "
            "no-op, or its effect is outside the DOM. Check `ebrowse outline` or screenshot.)"
        )
    else:
        label = "partial change" if diff.kind == "partial" else "dialog"
        out.append(f"{head} → {label}")
        if steps:
            out.append(steps)
        for sd in diff.sections:
            if sd.kind == "appeared" and sd.section:
                out.append(f"{outline_line(sd.section)}  [appeared]")
            elif sd.kind == "disappeared":
                desc = ""
                if sd.section:
                    desc = f" ({sd.section.type}, {sd.section.counts_desc()})"
                out.append(f"- {sd.sid}{desc} disappeared")
            else:
                if sd.added:
                    shown = ", ".join(element_inline(e) for e in sd.added[:_MAX_DIFF_ELEMENTS])
                    more = (
                        f" … +{len(sd.added) - _MAX_DIFF_ELEMENTS} more"
                        if len(sd.added) > _MAX_DIFF_ELEMENTS
                        else ""
                    )
                    out.append(f"+ {sd.sid}: {shown}{more}")
                if sd.removed:
                    names = ", ".join(d.short_desc() for d in sd.removed[:6])
                    out.append(f"- {sd.sid}: {len(sd.removed)} element(s) removed ({names})")
                for ref, field_name, old, new in sd.state_changes:
                    out.append(f'~ {ref} {field_name}: "{_clip(old, 40)}" → "{_clip(new, 40)}"')
                if sd.text_added:
                    out.append(f'~ {sd.sid}: new text: "{sd.text_added}"')
                elif not sd.added and not sd.removed and not sd.state_changes:
                    out.append(f"~ {sd.sid}: text content changed")

    for note in diff.notes:
        out.append(f"note: {note}")
    return "\n".join(out)


# ------------------------------------------------------------------ query ----


def render_query(
    section: Section,
    raw: RawSection,
    observe: ObserveConfig,
    filter_expr: str | None = None,
    cols: list[str] | None = None,
    cursor: int = 0,
    limit: int | None = None,
) -> str:
    """R2 query over a list/table section: filter rows, project columns.

    filter_expr is a regex (falling back to a literal substring on bad regex),
    matched case-insensitively against each item's full text.
    """
    import re as _re

    items = _items_of(raw)
    if not items:
        return f"{section.sid} has no queryable items (type={section.type})"
    limit = limit or observe.list_page_size

    pattern = None
    if filter_expr:
        try:
            pattern = _re.compile(filter_expr, _re.IGNORECASE)
        except _re.error:
            pattern = _re.compile(_re.escape(filter_expr), _re.IGNORECASE)

    is_table = section.type == "table"
    headers = _table_header(raw.node) if is_table else []
    col_idx: list[int] | None = None
    if cols and headers:
        col_idx = []
        plain = [_re.sub(r"\(@e\d+\)|[\[\]]", "", h).strip().casefold() for h in headers]
        missing = []
        for want in cols:
            hit = next((i for i, h in enumerate(plain) if want.casefold() in h), None)
            if hit is None:
                missing.append(want)
            else:
                col_idx.append(hit)
        if missing:
            return (
                f"error-cols: no column matching {', '.join(missing)} — columns: {', '.join(plain)}"
            )

    # filter matches PLAIN item text (subtree_text), not the rendered markdown —
    # anchors like ^Ab must not collide with "[...](@ref)" markup (drugs.com A–Z)
    matched: list[tuple[int, str]] = []
    for i, item in enumerate(items, start=1):
        if pattern and not pattern.search(item.subtree_text(cap=4000)):
            continue
        if is_table:
            cells = [c for c in item.children if c.tag in ("td", "th")]
            texts = [_inline(c) or " " for c in cells]
            shown = [texts[j] for j in col_idx if j < len(texts)] if col_idx else texts
            matched.append((i, "| " + " | ".join(shown) + " |"))
        else:
            matched.append((i, _inline(item)))

    total_matched = len(matched)
    window = matched[cursor : cursor + limit]
    lines = [
        f"QUERY {section.sid}"
        + (f' filter="{filter_expr}"' if filter_expr else "")
        + f" — matched {total_matched} of {len(items)} items"
    ]
    if is_table and window:
        shown_headers = [headers[j] for j in col_idx] if col_idx else headers
        if shown_headers:
            lines.append("| # | " + " | ".join(shown_headers) + " |")
    for i, text in window:
        lines.append(f"| {i} {text}" if is_table else f"{i}. {text}")
    end = cursor + len(window)
    if end < total_matched:
        lines.append(f"… {total_matched - end} more — add --cursor {end}")
    return "\n".join(lines)
