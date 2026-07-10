"""Lossless, budget-aware section partitioning.

The splitter treats a page as owned fragments rather than assuming every
section is one intact DOM subtree.  Oversized semantic containers are sliced
at child boundaries, queryable collections are promoted, and wrapper-owned
text/click signals are retained in a shallow projection.  Every captured node
is consequently owned by exactly one RawSection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ebrowse.core.clickable import is_clickable
from ebrowse.core.collection import collection_items, collection_kind
from ebrowse.core.fingerprint import normalize_class
from ebrowse.core.snapshot import DomNode, DomSnapshot
from ebrowse.model import SectionType

GROUP_TAGS = {
    "ol", "ul", "table", "form", "fieldset", "aside", "article", "details",
    "p", "img", "embed", "code", "pre", "nav", "header", "footer", "iframe",
    "figure", "video", "audio", "blockquote", "dl", "dialog",
}  # fmt: skip

GROUP_ROLES = {
    "group", "dialog", "alertdialog", "navigation", "form", "list", "table",
    "grid", "treegrid",
}  # fmt: skip

SEMANTIC_CHILD_TAGS = {
    "article", "aside", "section", "nav", "header", "footer", "form",
    "table", "ul", "ol", "main", "dialog",
}  # fmt: skip

MIN_GROUP_RUN = 4
MERGE_MAX_HEIGHT = 320
MERGE_RUN_MAX_HEIGHT = 1100
_CHUNK_TARGET_RATIO = 0.80  # headroom prevents small updates reshaping every boundary
_NODE_MARKUP_COST = 20


@dataclass(slots=True)
class RawSection:
    """An ordered forest of exclusively-owned nodes plus semantic context."""

    node: DomNode
    parent_tags: tuple[str, ...]
    stype: SectionType = "content"
    item_count: int | None = None
    merged_nodes: list[DomNode] = field(default_factory=list)
    # Semantic owner inherited by residual fragments (a form split around a
    # table remains a form). Collections always override this hint.
    stype_hint: SectionType | None = None
    # Current-observation-only identity for safe coalescing. Unlike
    # parent_tags, it distinguishes unrelated containers with the same tag path.
    context_key: int | None = None
    estimated_chars: int = 0
    # Stable structural representative for fingerprints when ``node`` is a
    # synthetic forest container.
    fingerprint_node: DomNode | None = None

    def all_nodes(self) -> list[DomNode]:
        return self.merged_nodes if self.merged_nodes else [self.node]

    def iter_walk(self):
        for n in self.all_nodes():
            yield from n.walk()


def _oversized_pixels(node: DomNode) -> bool:
    _, _, w, h = node.rect
    return (h > 900 and w > 320) or (h > 500 and w > 800)


def _is_terminal(node: DomNode) -> bool:
    if collection_kind(node):
        return True
    if node.tag in GROUP_TAGS:
        return True
    if (node.attrs.get("role") or "") in GROUP_ROLES:
        return True
    if _oversized_pixels(node):
        return not node.children
    semantic_children = sum(1 for c in node.children if c.tag in SEMANTIC_CHILD_TAGS)
    return semantic_children < 2


def _semantic_type(node: DomNode) -> SectionType | None:
    role = node.attrs.get("role") or ""
    if role in ("dialog", "alertdialog") or node.tag == "dialog":
        return "dialog"
    if node.tag in ("form", "fieldset") or role == "form":
        return "form"
    if node.tag == "nav" or role == "navigation":
        return "nav"
    if node.tag == "header" or role == "banner":
        return "header"
    if node.tag == "footer" or role == "contentinfo":
        return "footer"
    return None


def _group_key(node: DomNode) -> tuple[str, str]:
    return (node.tag, normalize_class(node.attrs.get("cls", "")))


def _union_rect(nodes: list[DomNode]) -> tuple[int, int, int, int]:
    visible = [n for n in nodes if n.rect[2] > 0 and n.rect[3] > 0] or nodes
    xs1 = [n.rect[0] for n in visible]
    ys1 = [n.rect[1] for n in visible]
    xs2 = [n.rect[0] + n.rect[2] for n in visible]
    ys2 = [n.rect[1] + n.rect[3] for n in visible]
    x, y = min(xs1), min(ys1)
    return (x, y, max(xs2) - x, max(ys2) - y)


_ITEMISH_TAGS = {"li", "tr", "dt", "dd", "option", "article"}


def _groupable(node: DomNode, key: tuple[str, str]) -> bool:
    return (key[1] != "" or key[0] in _ITEMISH_TAGS) and node.rect[3] > 0


def _group_siblings(children: list[DomNode]) -> list[DomNode]:
    out: list[DomNode] = []
    i = 0
    while i < len(children):
        j = i + 1
        key = _group_key(children[i])
        while j < len(children) and _group_key(children[j]) == key:
            j += 1
        run = children[i:j]
        if len(run) >= MIN_GROUP_RUN and all(_groupable(n, key) for n in run):
            group = DomNode(
                tag=run[0].tag,
                rect=_union_rect(run),
                attrs={"cls": run[0].attrs.get("cls", "")},
                children=run,
                iframe_path=run[0].iframe_path,
            )
            group.is_list_group = True
            out.append(group)
        else:
            out.extend(run)
        i = j
    return out


def _has_substance(node: DomNode) -> bool:
    for n in node.walk():
        if n.text or is_clickable(n) or n.tag in ("img", "iframe", "video"):
            return True
    return False


def _has_own_substance(node: DomNode) -> bool:
    return bool(node.text or is_clickable(node) or node.tag in ("img", "iframe", "video"))


def _node_cost(node: DomNode) -> int:
    """Conservative rendered-character proxy, independent of assigned refs."""
    if node.split_cost is not None:
        return node.split_cost
    cost = _NODE_MARKUP_COST + len(node.text)
    for key in ("nm", "href", "ph", "val", "sel", "alt", "ttl"):
        value = node.attrs.get(key)
        if value:
            cost += len(str(value))
    opts = node.attrs.get("opt") or []
    cost += sum(len(str(opt)) + 3 for opt in opts)
    node.split_cost = cost + sum(_node_cost(child) for child in node.children)
    return node.split_cost


def _contains_collection(node: DomNode) -> bool:
    if node.has_collection_descendant is None:
        node.has_collection_descendant = any(
            collection_kind(child) is not None or _contains_collection(child)
            for child in _group_siblings(node.children)
        )
    return node.has_collection_descendant


def _shallow_owner(node: DomNode) -> DomNode:
    """Projection owning only a wrapper's own contribution, not descendants."""
    projected = DomNode(
        tag=node.tag,
        rect=node.rect,
        attrs=dict(node.attrs),
        text=node.text,
        signals=dict(node.signals),
        iframe_path=node.iframe_path,
        cross_origin=node.cross_origin,
    )
    if is_clickable(node):
        projected.identity_text = node.subtree_text(cap=160)
    return projected


def _forest_section(
    roots: list[DomNode],
    chain: tuple[str, ...],
    *,
    hint: SectionType | None,
    context_key: int,
) -> RawSection:
    if len(roots) == 1:
        node = roots[0]
        merged: list[DomNode] = []
    else:
        node = DomNode(
            tag="div",
            rect=_union_rect(roots),
            children=roots,
            iframe_path=roots[0].iframe_path,
        )
        merged = roots
    return RawSection(
        node=node,
        parent_tags=chain,
        merged_nodes=merged,
        stype_hint=hint,
        context_key=context_key,
        estimated_chars=sum(_node_cost(n) for n in roots),
        fingerprint_node=roots[0],
    )


def _emit_partitioned(
    node: DomNode,
    chain: tuple[str, ...],
    out: list[RawSection],
    *,
    max_chars: int,
    inherited_hint: SectionType | None,
) -> None:
    """Partition one oversized/collection-bearing container without loss."""
    hint = _semantic_type(node) or inherited_hint
    context_key = id(node)
    target = max(256, int(max_chars * _CHUNK_TARGET_RATIO))
    pending: list[DomNode] = []
    pending_cost = 0
    pending_chain = (*chain, node.tag)

    def flush() -> None:
        nonlocal pending_cost, pending_chain
        if pending:
            out.append(
                _forest_section(pending.copy(), pending_chain, hint=hint, context_key=context_key)
            )
            pending.clear()
            pending_cost = 0
            pending_chain = (*chain, node.tag)

    # A modal dialog needs an explicit section even when all of its owned
    # content is a promoted collection; otherwise appeared-dialog detection
    # would see only a table/list and lose the page-blocking signal.
    if _has_own_substance(node) or hint == "dialog":
        pending.append(_shallow_owner(node))
        pending_cost = _node_cost(pending[0])
        pending_chain = chain

    for child in _group_siblings(node.children):
        # Oversized-container packing must apply the same substance gate as
        # normal recursive descent. Zero-height anchors/spacers otherwise
        # inherit semantic context and become empty form/content outline rows.
        if not _has_substance(child):
            continue
        kind = collection_kind(child)
        cost = _node_cost(child)
        if kind:
            flush()
            raw = RawSection(
                node=child,
                parent_tags=(*chain, node.tag),
                context_key=context_key,
                estimated_chars=cost,
            )
            out.append(raw)
            continue
        if _contains_collection(child) or cost > max_chars:
            flush()
            if child.children:
                _emit_partitioned(
                    child,
                    (*chain, node.tag),
                    out,
                    max_chars=max_chars,
                    inherited_hint=hint,
                )
            elif _has_substance(child):
                out.append(
                    _forest_section([child], (*chain, node.tag), hint=hint, context_key=context_key)
                )
            continue
        if pending and pending_cost + cost > target:
            flush()
        pending.append(child)
        pending_cost += cost
    flush()


def _split(
    node: DomNode,
    chain: tuple[str, ...],
    out: list[RawSection],
    *,
    max_chars: int,
    inherited_hint: SectionType | None = None,
    context_key: int | None = None,
) -> None:
    cost = _node_cost(node)
    if collection_kind(node):
        if _has_substance(node):
            out.append(RawSection(node=node, parent_tags=chain, estimated_chars=cost))
        return
    if _is_terminal(node):
        if not _has_substance(node):
            return
        if cost <= max_chars:
            out.append(
                RawSection(
                    node=node,
                    parent_tags=chain,
                    stype_hint=_semantic_type(node) or inherited_hint,
                    context_key=context_key or id(node),
                    estimated_chars=cost,
                )
            )
        else:
            _emit_partitioned(
                node,
                chain,
                out,
                max_chars=max_chars,
                inherited_hint=inherited_hint,
            )
        return

    # Descending used to discard wrapper-owned text/click signals. Preserve it
    # as a shallow fragment before visiting child subtrees.
    hint = _semantic_type(node) or inherited_hint
    if _has_own_substance(node):
        out.append(_forest_section([_shallow_owner(node)], chain, hint=hint, context_key=id(node)))
    for child in _group_siblings(node.children):
        _split(
            child,
            (*chain, node.tag),
            out,
            max_chars=max_chars,
            inherited_hint=hint,
            context_key=id(node),
        )


def _classify(raw: RawSection) -> SectionType:
    node = raw.node
    kind = collection_kind(node)
    if kind:
        return kind
    if raw.stype_hint:
        return raw.stype_hint
    semantic = _semantic_type(node)
    if semantic:
        return semantic
    if node.tag == "iframe":
        return "iframe"
    if node.tag in ("img", "figure", "video", "audio", "embed"):
        return "media"
    controls = 0
    clickables = 0
    for n in raw.iter_walk():
        if is_clickable(n):
            clickables += 1
            if n.tag in ("input", "select", "textarea"):
                controls += 1
    if controls >= 2 and controls * 2 >= max(clickables, 1):
        return "form"
    return "content"


def _finalize(raw: RawSection) -> None:
    raw.stype = _classify(raw)
    items = collection_items(raw.node)
    raw.item_count = len(items) or None
    if not raw.estimated_chars:
        raw.estimated_chars = sum(_node_cost(n) for n in raw.all_nodes())


def _merge_sections(run: list[RawSection]) -> RawSection:
    roots = [n for sec in run for n in sec.all_nodes()]
    merged = _forest_section(
        roots,
        run[0].parent_tags,
        hint=run[0].stype if run[0].stype == "form" else run[0].stype_hint,
        context_key=run[0].context_key or id(run[0]),
    )
    _finalize(merged)
    return merged


_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _is_heading_only(raw: RawSection) -> bool:
    roots = raw.all_nodes()
    return (
        raw.stype == "content"
        and len(roots) == 1
        and roots[0].tag in _HEADING_TAGS
        and not any(is_clickable(node) for node in roots[0].walk())
    )


def _attach_heading_sections(sections: list[RawSection], max_chars: int) -> list[RawSection]:
    """Attach a standalone heading to its compatible following content."""
    out: list[RawSection] = []
    i = 0
    while i < len(sections):
        heading = sections[i]
        following = sections[i + 1] if i + 1 < len(sections) else None
        compatible = bool(
            following
            and _is_heading_only(heading)
            and following.stype == "content"
            and heading.context_key == following.context_key
            and heading.parent_tags == following.parent_tags
            and heading.node.iframe_path == following.node.iframe_path
            and heading.node.rect[3] + following.node.rect[3] <= MERGE_RUN_MAX_HEIGHT
            and heading.estimated_chars + following.estimated_chars <= max_chars
        )
        if compatible:
            assert following is not None
            out.append(_merge_sections([heading, following]))
            i += 2
        else:
            out.append(heading)
            i += 1
    return out


def _keep_final_section(raw: RawSection) -> bool:
    """Defensive final gate against empty projections and layout fragments."""
    if raw.stype in ("dialog", "iframe", "list", "table"):
        return True
    return any(_has_substance(root) for root in raw.all_nodes())


def _coalesce_small(sections: list[RawSection], max_chars: int) -> list[RawSection]:
    out: list[RawSection] = []
    run: list[RawSection] = []
    run_height = 0
    run_cost = 0

    def flush() -> None:
        nonlocal run_height, run_cost
        if run:
            out.append(run[0] if len(run) == 1 else _merge_sections(run))
        run.clear()
        run_height = 0
        run_cost = 0

    for sec in sections:
        mergeable = (
            sec.stype in ("content", "form")
            and sec.node.rect[3] < MERGE_MAX_HEIGHT
            and not collection_kind(sec.node)
        )
        fits = (
            run
            and run[-1].stype == sec.stype
            and run[-1].parent_tags == sec.parent_tags
            and run[-1].context_key == sec.context_key
            and run_height + sec.node.rect[3] <= MERGE_RUN_MAX_HEIGHT
            and run_cost + sec.estimated_chars <= max_chars
        )
        if mergeable and (not run or fits):
            run.append(sec)
            run_height += sec.node.rect[3]
            run_cost += sec.estimated_chars
        else:
            flush()
            if mergeable:
                run.append(sec)
                run_height = sec.node.rect[3]
                run_cost = sec.estimated_chars
            else:
                out.append(sec)
    flush()
    return out


def _reduce_to_target(
    sections: list[RawSection], max_sections: int, max_chars: int
) -> list[RawSection]:
    """Best-effort outline reduction; correctness budgets outrank the target."""
    sections = list(sections)
    while len(sections) > max_sections:
        candidates: list[tuple[int, int]] = []
        for i, (left, right) in enumerate(zip(sections, sections[1:], strict=False)):
            compatible = (
                left.stype == right.stype == "content"
                and left.context_key == right.context_key
                and left.parent_tags == right.parent_tags
                and left.node.iframe_path == right.node.iframe_path
            )
            combined = left.estimated_chars + right.estimated_chars
            if compatible and combined <= max_chars:
                candidates.append((combined, i))
        if not candidates:
            break
        _, i = min(candidates)
        sections[i : i + 2] = [_merge_sections(sections[i : i + 2])]
    return sections


def split_page(
    snapshot: DomSnapshot,
    max_sections: int = 60,
    max_section_tokens: int = 16_384,
) -> list[RawSection]:
    """DomSnapshot -> lossless, ordered, budgeted RawSections.

    ``max_sections`` is a soft outline-size target. An ordinary section's
    expansion budget and collection/action boundaries always take precedence.
    """
    max_chars = max(1, max_section_tokens) * 4
    sections: list[RawSection] = []
    _split(snapshot.root, (), sections, max_chars=max_chars)
    for sec in sections:
        _finalize(sec)
    sections = [sec for sec in sections if _keep_final_section(sec)]
    sections = _attach_heading_sections(sections, max_chars)
    sections = _coalesce_small(sections, max_chars)
    return _reduce_to_target(sections, max_sections, max_chars)
