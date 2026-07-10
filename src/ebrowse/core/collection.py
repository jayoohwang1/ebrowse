"""Shared list/table semantics for splitting, rendering, and query.

A node is collection-capable only when this module can expose stable items for
pagination/query.  Keeping that decision in one place prevents sections from
being labelled ``table`` or ``list`` without actually supporting those verbs.
"""

from __future__ import annotations

from typing import Literal

from ebrowse.core.snapshot import DomNode

CollectionKind = Literal["list", "table"]

_TABLE_ROLES = {"table", "grid", "treegrid"}
_CELL_ROLES = {"cell", "gridcell", "columnheader", "rowheader"}


def collection_kind(node: DomNode) -> CollectionKind | None:
    role = node.attrs.get("role") or ""
    if node.tag == "table" or role in _TABLE_ROLES:
        return "table"
    if node.is_list_group or node.tag in ("ul", "ol", "dl") or role == "list":
        return "list"
    return None


def collection_items(node: DomNode) -> list[DomNode]:
    """Return the pageable items owned by one collection, in document order."""
    kind = collection_kind(node)
    if kind == "list":
        if node.is_list_group:
            return node.children
        if node.tag in ("ul", "ol"):
            return [c for c in node.children if c.tag == "li"]
        if node.tag == "dl":
            return [c for c in node.children if c.tag in ("dt", "dd")]
        return _owned_role_descendants(node, "listitem", nested_container_roles={"list"})

    if kind == "table":
        if node.tag == "table":
            bodies = [c for c in node.children if c.tag in ("tbody", "tfoot")]
            if bodies:
                return [row for body in bodies for row in body.children if row.tag == "tr"]
            rows = _html_rows(node)
            # Without an explicit thead/tbody, a leading all-<th> row is the header.
            if rows and any(c.tag == "th" for c in rows[0].children):
                rows = rows[1:]
            return rows
        rows = _owned_role_descendants(node, "row", nested_container_roles=_TABLE_ROLES)
        return [
            row
            for row in rows
            if not any((cell.attrs.get("role") or "") == "columnheader" for cell in row.children)
        ]
    return []


def table_headers(node: DomNode) -> list[DomNode]:
    if node.tag == "table":
        for child in node.children:
            if child.tag == "thead":
                row = next((c for c in child.children if c.tag == "tr"), None)
                if row:
                    return [c for c in row.children if c.tag in ("th", "td")]
        for row in _html_rows(node):
            cells = [c for c in row.children if c.tag == "th"]
            if cells:
                return cells
        return []
    rows = _owned_role_descendants(node, "row", nested_container_roles=_TABLE_ROLES)
    for row in rows:
        cells = [c for c in row.children if (c.attrs.get("role") or "") == "columnheader"]
        if cells:
            return cells
    return []


def table_cells(row: DomNode) -> list[DomNode]:
    if row.tag == "tr":
        return [c for c in row.children if c.tag in ("td", "th")]
    return [c for c in row.children if (c.attrs.get("role") or "") in _CELL_ROLES]


def _html_rows(node: DomNode) -> list[DomNode]:
    rows: list[DomNode] = []
    for child in node.children:
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in ("thead", "tbody", "tfoot"):
            rows.extend(c for c in child.children if c.tag == "tr")
    return rows


def _owned_role_descendants(
    node: DomNode, role: str, *, nested_container_roles: set[str]
) -> list[DomNode]:
    """Role descendants owned by ``node``, excluding nested collections."""
    out: list[DomNode] = []

    def rec(cur: DomNode) -> None:
        cur_role = cur.attrs.get("role") or ""
        if cur is not node and (
            cur_role in nested_container_roles or collection_kind(cur) is not None
        ):
            return
        if cur is not node and cur_role == role:
            out.append(cur)
            return
        for child in cur.children:
            rec(child)

    rec(node)
    return out
