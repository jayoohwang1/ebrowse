"""Interactable-element predicate: canonical signal sets + Python-side decision.

The sets live here (single source of truth) and are string-templated into
discover.js, which computes per-node signals in-page. The final predicate over
those signals is `is_clickable()` below.

Adapted from WebChallenger `is_clickable` (agent.py ~L1109) and the paper's
appendix predicate: visibility/accessibility gate AND at least one positive
signal (tag, role, listener attr, or top-of-chain cursor:pointer).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ebrowse.core.snapshot import DomNode

CLICKABLE_TAGS = [
    "a",
    "button",
    "input",
    "select",
    "textarea",
    "summary",
    "embed",
    "audio",
    "video",
]

CLICKABLE_ROLES = [
    "button",
    "link",
    "checkbox",
    "radio",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "tab",
    "switch",
    "option",
    "combobox",
    "listbox",
    "searchbox",
    "slider",
    "spinbutton",
    "textbox",
    "treeitem",
]

LISTENER_ATTRS = ["onclick", "onmousedown", "onmouseup", "onkeydown", "onkeyup", "jsaction"]

SKIP_TAGS = [
    "script",
    "style",
    "noscript",
    "template",
    "link",
    "meta",
    "base",
    "title",
    "head",
    "br",
    "wbr",
    "source",
    "track",
    "param",
    "map",
    "area",
    "datalist",
    "slot",
]

# Implicit ARIA roles by tag (subset that matters for descriptions/counting)
_IMPLICIT_ROLES = {
    "a": "link",  # only with href; handled in implicit_role()
    "button": "button",
    "select": "combobox",
    "textarea": "textbox",
    "summary": "button",
    "nav": "navigation",
    "header": "banner",
    "footer": "contentinfo",
    "form": "form",
    "table": "table",
    "dialog": "dialog",
}

_INPUT_ROLES = {
    "checkbox": "checkbox",
    "radio": "radio",
    "search": "searchbox",
    "range": "slider",
    "number": "spinbutton",
    "button": "button",
    "submit": "button",
    "reset": "button",
    "file": "button",
    "image": "button",
}


def implicit_role(tag: str, attrs: dict) -> str | None:
    """Effective role: explicit role attr wins, else implicit-by-tag."""
    explicit = attrs.get("role")
    if explicit:
        return explicit
    if tag == "a":
        return "link" if attrs.get("href") else None
    if tag == "input":
        return _INPUT_ROLES.get(attrs.get("typ", "text"), "textbox")
    return _IMPLICIT_ROLES.get(tag)


def render_js_template(js_source: str) -> str:
    """Inject the canonical sets into discover.js."""
    return (
        js_source.replace("__CLICKABLE_TAGS__", json.dumps(CLICKABLE_TAGS))
        .replace("__CLICKABLE_ROLES__", json.dumps(CLICKABLE_ROLES))
        .replace("__LISTENER_ATTRS__", json.dumps(LISTENER_ATTRS))
        .replace("__SKIP_TAGS__", json.dumps(SKIP_TAGS))
    )


def is_clickable(node: DomNode) -> bool:
    """Final predicate over in-page signals. Gate: rendered + enabled."""
    if not node.signals:
        return False
    if node.attrs.get("dis"):
        return False
    return node.bbox_area() > 0


def is_form_control(node: DomNode) -> bool:
    return node.tag in ("input", "select", "textarea") or bool(node.attrs.get("con"))


# Clickable roles that are CONTAINERS: their descendants are the real targets
# (a role=listbox is clickable, but its role=option children must still become
# elements — recreation.gov suggestions bug). These never suppress descendants.
CONTAINER_ROLES = {"listbox", "combobox", "menu", "list", "radiogroup", "tree", "grid"}


def is_container_widget(node: DomNode) -> bool:
    return (node.attrs.get("role") or "") in CONTAINER_ROLES
