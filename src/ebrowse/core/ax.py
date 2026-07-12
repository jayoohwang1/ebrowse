"""Deterministic accessibility-tree rendering derived from a ``DomSnapshot``.

This is deliberately separate from :mod:`ebrowse.core.render`: the markdown
formats are frozen, while this opt-in view can expose the captured semantic
tree without losing ebrowse's actionable refs.
"""

from __future__ import annotations

from collections.abc import Iterable

from ebrowse.config import ObserveConfig
from ebrowse.core.collection import collection_items
from ebrowse.core.render import _clip
from ebrowse.core.snapshot import DomNode
from ebrowse.core.split import RawSection
from ebrowse.model import Section

# HTML-AAM's common, useful implicit roles.  Keep this data driven so additions
# are auditable and do not turn into page-specific rendering rules.
IMPLICIT_ROLES: dict[str, str] = {
    "a[href]": "link",
    "button": "button",
    "input": "textbox",
    "input:search": "searchbox",
    "input:checkbox": "checkbox",
    "input:radio": "radio",
    "input:range": "slider",
    "input:number": "spinbutton",
    "input:submit": "button",
    "input:button": "button",
    "input:reset": "button",
    "input:image": "button",
    "input:file": "button",
    "textarea": "textbox",
    "select": "combobox",
    "select:multiple": "listbox",
    "option": "option",
    "h1": "heading",
    "h2": "heading",
    "h3": "heading",
    "h4": "heading",
    "h5": "heading",
    "h6": "heading",
    "ul": "list",
    "ol": "list",
    "li": "listitem",
    "table": "table",
    "tr": "row",
    "td": "cell",
    "th": "columnheader",
    "img": "img",
    "nav": "navigation",
    "main": "main",
    "header": "banner",
    "footer": "contentinfo",
    "aside": "complementary",
    "form": "form",
    "article": "article",
    "fieldset": "group",
    "details": "group",
    "summary": "button",
    "dialog": "dialog",
    "hr": "separator",
    "progress": "progressbar",
    "figure": "figure",
    "p": "paragraph",
    "blockquote": "blockquote",
}


def implicit_role(node: DomNode) -> str | None:
    """Return the supported implicit role for a node (explicit roles win elsewhere)."""
    tag, a = node.tag, node.attrs
    if node.is_list_group:
        return "list"
    if tag == "a":
        return IMPLICIT_ROLES["a[href]"] if a.get("href") else None
    if tag == "input":
        typ = str(a.get("typ") or "text").lower()
        return IMPLICIT_ROLES.get(f"input:{typ}", IMPLICIT_ROLES["input"])
    if tag == "select":
        size = a.get("siz") or a.get("size") or 0
        return (
            IMPLICIT_ROLES["select:multiple"]
            if a.get("mul") or int(size or 0) > 1
            else IMPLICIT_ROLES["select"]
        )
    if tag == "section":
        return "region" if a.get("nm") else None
    return IMPLICIT_ROLES.get(tag)


def render_section_ax(
    section: Section,
    raw: RawSection,
    observe: ObserveConfig,
    cursor: int = 0,
    show_all: bool = False,
) -> str:
    """Render one section as a compact accessibility-tree outline."""
    head = f"## {section.sid} {section.type}"
    if section.heading:
        head += f" — {section.heading}"
    head += " (ax)"
    if section.cross_origin:
        # The markdown notice is intentionally byte-for-byte the familiar
        # recovery guidance; only its AX header differs.
        return f"{head}\n(cross-origin iframe: {section.preview} — content not accessible; try `screenshot --section {section.sid}`)"

    lines = [head]
    scr_best: tuple[int, list] | None = None
    for node in raw.iter_walk():
        value = node.attrs.get("scr")
        if value and (scr_best is None or node.bbox_area() > scr_best[0]):
            scr_best = (node.bbox_area(), value)
    if scr_best:
        top, maximum = scr_best[1][0], scr_best[1][1]
        lines.append(
            f"(inner scrollable panel: y={top} of {maximum}px — 'ebrowse scroll {section.sid} down' scrolls it)"
        )

    items = collection_items(raw.node) if section.type in ("list", "table") else []
    excluded: set[int] | None = None
    more = 0
    next_cursor = cursor
    if items:
        window = items[cursor:] if show_all else items[cursor : cursor + observe.list_page_size]
        excluded = {id(item) for item in items} - {id(item) for item in window}
        next_cursor = cursor + len(window)
        more = len(items) - next_cursor

    body: list[str] = []
    for root in raw.all_nodes():
        _render_node(root, 0, body, observe, excluded)
    if more:
        body.append(f"… {more} more items — expand {section.sid} --ax --cursor {next_cursor}")

    # The budget is based on output text, as the markdown renderer does.  Never
    # emit half a node: lines are the atomic units of this format.
    return _bounded(lines, body, observe.max_section_tokens)


def _render_node(
    node: DomNode,
    depth: int,
    lines: list[str],
    observe: ObserveConfig,
    excluded_items: set[int] | None,
    forced_role: str | None = None,
) -> None:
    # A paged collection owns only its selected direct item roots.  Descendants
    # are rendered normally once their selected root is entered.
    if excluded_items is not None and id(node) in excluded_items:
        return

    role = node.attrs.get("role") or forced_role or implicit_role(node)
    # role=presentation/none is an explicit "no semantics" claim — prune like a
    # generic wrapper (decorative svg wrappers are the common case).
    if role in ("presentation", "none") and not node.ref:
        role = None
    name = _name(node)
    # Plain wrappers do not acquire an accessible name merely by containing
    # direct text.  Prune them and retain that text as an explicit text child.
    # <label> text is suppressed outright: it already lives on the associated
    # control's accessible name (mirrors the markdown renderer).
    generic = role is None and not node.ref and not node.attrs.get("nm")
    if generic:
        if node.tag != "label":
            _append_text(lines, depth, node.text, observe.preview_chars)
        for child in node.children:
            _render_node(child, depth, lines, observe, excluded_items)
        return

    # Name-from-content (HTML-AAM): a nameless link/button/heading/… whose
    # subtree is pure text takes that text as its name and renders as one line
    # (the consumed descendants are not repeated as text: children).
    consumed_subtree = False
    if not name and role in _NAME_FROM_CONTENT and _text_only_subtree(node):
        name = node.subtree_text(cap=80)
        consumed_subtree = bool(name)

    if role is None:
        role = "generic"
    indent = "  " * depth
    parts = [f"{indent}- {role}"]
    if name:
        parts.append(f' "{_quote(_clip(name, 80))}"')
    ref = _ref(node)
    if ref:
        parts.append(f" ({ref})")
    states = _states(node, role)
    if states:
        parts.append(" [" + ", ".join(states) + "]")
    lines.append("".join(parts))

    if consumed_subtree:
        return
    # Text used as the accessible name is not duplicated as a child.  nm is
    # authoritative, so different own text remains visible.
    if node.text and (not name or node.attrs.get("nm") or _clip(node.text, 80) != name):
        _append_text(lines, depth + 1, node.text, observe.preview_chars)
    for child in node.children:
        _render_node(
            child,
            depth + 1,
            lines,
            observe,
            excluded_items,
            "listitem" if node.is_list_group else None,
        )


# Roles whose accessible name may come from their contents (HTML-AAM subset).
_NAME_FROM_CONTENT = frozenset(
    {"link", "button", "heading", "option", "menuitem", "tab", "cell", "columnheader", "rowheader"}
)


def _text_only_subtree(node: DomNode) -> bool:
    """True when every descendant is a text-bearing generic/presentational
    wrapper — nothing with its own ref, name, image, or non-presentational role."""
    for child in node.children:
        a = child.attrs
        if child.ref or child.tag == "img" or a.get("nm"):
            return False
        role = a.get("role")
        if role and role not in ("presentation", "none"):
            return False
        if role is None and implicit_role(child) is not None:
            return False
        if not _text_only_subtree(child):
            return False
    return True


def _name(node: DomNode) -> str:
    name = str(node.attrs.get("nm") or node.text or "").strip()
    # An <input type=submit|button|reset> is named by its value attribute.
    if not name and node.tag == "input":
        typ = str(node.attrs.get("typ", "")).lower()
        if typ in ("submit", "button", "reset"):
            name = str(node.attrs.get("val") or "").strip() or typ.capitalize()
    return name


def _ref(node: DomNode) -> str | None:
    if not node.ref:
        return None
    return f"{node.ref} ?" if node.candidate else node.ref


def _states(node: DomNode, role: str) -> list[str]:
    a = node.attrs
    out: list[str] = []
    if role in ("checkbox", "radio", "switch") and "chk" in a:
        out.append("checked" if a.get("chk") else "unchecked")
    if a.get("dis"):
        out.append("disabled")
    if "exp" in a:
        out.append("expanded" if a.get("exp") else "collapsed")
    if a.get("prs"):
        out.append("pressed")
    if a.get("asel"):
        out.append("selected")
    if a.get("req"):
        out.append("required")
    if a.get("inr"):
        out.append("inert")
    if node.tag in ("input", "textarea") and role not in ("checkbox", "radio", "button"):
        value = (
            "•••"
            if str(a.get("typ", "")).lower() == "password"
            else _clip(str(a.get("val", "")), 60)
        )
        out.append(f'value="{_quote(value)}"')
    if node.tag == "select":
        value = _clip(str(a.get("sel", "")), 60)
        total = a.get("optn") or len(a.get("opt") or [])
        select = f'value="{_quote(value)}" of {total} options'
        if a.get("mul"):
            select += ", multiple"
        out.append(select)
    if node.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        out.append(f"level={node.tag[1]}")
    return out


def _append_text(lines: list[str], depth: int, text: str, cap: int) -> None:
    text = " ".join(text.split())
    if not text:
        return
    prefix = "  " * depth + '- text: "'
    # Consecutive own-text fragments at this depth are one logical text node.
    if lines and lines[-1].startswith(prefix) and lines[-1].endswith('"'):
        prior = lines[-1][len(prefix) : -1]
        lines[-1] = prefix + _quote(_clip(prior + " " + text, cap)) + '"'
    else:
        lines.append(prefix + _quote(_clip(text, cap)) + '"')


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _bounded(head: list[str], body: Iterable[str], max_tokens: int) -> str:
    budget = max_tokens * 4
    lines = list(head)
    truncated = False
    for line in body:
        candidate = "\n".join([*lines, line])
        if len(candidate) > budget:
            truncated = True
            break
        lines.append(line)
    if truncated:
        tail = "… (truncated at token budget — use --cursor or --all)"
        while len(lines) > len(head) and len("\n".join([*lines, tail])) > budget:
            lines.pop()
        lines.append(tail)
    return "\n".join(lines).rstrip()
