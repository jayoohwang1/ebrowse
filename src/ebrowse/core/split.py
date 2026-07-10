"""Section splitter: DomSnapshot -> ordered list of section subtrees.

Adaptation of WebChallenger's DividePage (paper appendix Alg. 1, agent.py
split_section/merge_sections): recursive descent that terminates at semantic
grouping tags, non-oversized nodes, or synthetic list groups (>=4 consecutive
siblings sharing tag+normalized class). Two ebrowse-specific post-passes keep
outlines short: tiny adjacent content sections are coalesced, and a
max_sections overflow valve merges the tail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ebrowse.core.clickable import is_clickable
from ebrowse.core.fingerprint import normalize_class
from ebrowse.core.snapshot import DomNode, DomSnapshot
from ebrowse.model import SectionType

# Tags that form a semantic grouping: never descend into them.
GROUP_TAGS = {
    "ol", "ul", "table", "form", "fieldset", "aside", "article", "details",
    "p", "img", "embed", "code", "pre", "nav", "header", "footer", "iframe",
    "figure", "video", "audio", "blockquote", "dl",
}  # fmt: skip

GROUP_ROLES = {"group", "dialog", "alertdialog", "navigation", "form", "list", "table"}

# Children that mark a container as worth descending into even when it is not
# visually oversized (e.g. a compact <main> holding <article> + <aside>). Pixel
# thresholds alone are brittle: pages with unloaded images or dense layouts can
# fall just under them (seen on article.html where main was 768x899).
SEMANTIC_CHILD_TAGS = {
    "article", "aside", "section", "nav", "header", "footer", "form",
    "table", "ul", "ol", "main", "dialog",
}  # fmt: skip

MIN_GROUP_RUN = 4  # consecutive same tag+class siblings that form a list group
MERGE_MAX_HEIGHT = 240  # adjacent content sections shorter than this get coalesced
# Cap on a coalesced run's total height. Without it, css-in-js sites whose whole
# page is small same-parent divs (healthline.com: emotion classes, no semantic
# tags) collapse into ONE giant section — 85 elements behind a single outline row.
MERGE_RUN_MAX_HEIGHT = 700
MIN_SECTION_AREA = 1200  # px^2; smaller sections with no text/elements are dropped


@dataclass(slots=True)
class RawSection:
    """A section subtree plus the ancestry context the splitter saw."""

    node: DomNode
    parent_tags: tuple[str, ...]  # ancestor tag chain from body (exclusive) downward
    stype: SectionType = "content"
    item_count: int | None = None
    merged_nodes: list[DomNode] = field(default_factory=list)  # when coalesced

    def all_nodes(self) -> list[DomNode]:
        return self.merged_nodes if self.merged_nodes else [self.node]

    def iter_walk(self):
        for n in self.all_nodes():
            yield from n.walk()


def _oversized(node: DomNode) -> bool:
    _, _, w, h = node.rect
    return (h > 900 and w > 320) or (h > 500 and w > 800)


def _is_terminal(node: DomNode) -> bool:
    if node.is_list_group:
        return True
    if node.tag in GROUP_TAGS:
        return True
    if (node.attrs.get("role") or "") in GROUP_ROLES:
        return True
    if _oversized(node):
        # A childless oversized node has nothing to descend into; descending
        # would discard it along with its own text and clickable signal
        # (full-viewport cookie veils / interstitial covers wired via onclick).
        # Terminal + the _has_substance gate keeps real overlays and still
        # drops bare decorative backdrops.
        return not node.children
    semantic_children = sum(1 for c in node.children if c.tag in SEMANTIC_CHILD_TAGS)
    return semantic_children < 2


def _group_key(node: DomNode) -> tuple[str, str]:
    return (node.tag, normalize_class(node.attrs.get("cls", "")))


def _union_rect(nodes: list[DomNode]) -> tuple[int, int, int, int]:
    xs1 = [n.rect[0] for n in nodes]
    ys1 = [n.rect[1] for n in nodes]
    xs2 = [n.rect[0] + n.rect[2] for n in nodes]
    ys2 = [n.rect[1] + n.rect[3] for n in nodes]
    x, y = min(xs1), min(ys1)
    return (x, y, max(xs2) - x, max(ys2) - y)


# Tags whose same-tag runs are list items even without a shared class.
_ITEMISH_TAGS = {"li", "tr", "dt", "dd", "option", "article"}


def _groupable(node: DomNode, key: tuple[str, str]) -> bool:
    """Classless <div> runs are page structure, not list items (healthline.com's
    body is a run of classless wrapper divs — grouping them swallowed the whole
    page into one 'list'). Require a shared class or an item-ish tag, and a
    rendered box."""
    return (key[1] != "" or key[0] in _ITEMISH_TAGS) and node.rect[3] > 0


def _group_siblings(children: list[DomNode]) -> list[DomNode]:
    """Replace runs of >=MIN_GROUP_RUN same-key siblings with one list-group node."""
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
    if node.bbox_area() >= MIN_SECTION_AREA:
        pass  # size alone is not substance; check content below
    for n in node.walk():
        if n.text or is_clickable(n) or n.tag in ("img", "iframe", "video"):
            return True
    return False


def _split(node: DomNode, chain: tuple[str, ...], out: list[RawSection]) -> None:
    if _is_terminal(node):
        if _has_substance(node):
            out.append(RawSection(node=node, parent_tags=chain))
        return
    for child in _group_siblings(node.children):
        _split(child, (*chain, node.tag), out)


def _list_item_count(node: DomNode) -> int | None:
    if node.is_list_group:
        return len(node.children)
    if node.tag in ("ul", "ol", "dl"):
        items = [c for c in node.children if c.tag in ("li", "dt", "dd")]
        return len(items) or None
    if node.tag == "table":
        for n in node.walk():
            if n.tag == "tbody":
                return len([c for c in n.children if c.tag == "tr"]) or None
        rows = [n for n in node.walk() if n.tag == "tr"]
        return (len(rows) - 1) if len(rows) > 1 else (len(rows) or None)
    return None


def _classify(raw: RawSection, doc_height: int) -> SectionType:
    node = raw.node
    role = node.attrs.get("role") or ""
    tag = node.tag
    if tag == "nav" or role == "navigation":
        return "nav"
    if role in ("dialog", "alertdialog") or tag == "dialog":
        return "dialog"
    if tag == "header" or role == "banner":
        return "header"
    if tag == "footer" or role == "contentinfo":
        return "footer"
    if tag in ("form", "fieldset") or role == "form":
        return "form"
    if tag == "table" or role == "table":
        return "table"
    if node.is_list_group or tag in ("ul", "ol", "dl") or role == "list":
        return "list"
    if tag == "iframe":
        return "iframe"
    if tag in ("img", "figure", "video", "audio", "embed"):
        return "media"
    # composition heuristics
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


def _coalesce_small(sections: list[RawSection]) -> list[RawSection]:
    """Merge runs of consecutive short content sections that share a parent chain."""
    out: list[RawSection] = []
    run: list[RawSection] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            out.append(run[0])
        else:
            nodes = [n for r in run for n in r.all_nodes()]
            container = DomNode(
                tag="div",
                rect=_union_rect(nodes),
                children=nodes,
                iframe_path=nodes[0].iframe_path,
            )
            out.append(
                RawSection(node=container, parent_tags=run[0].parent_tags, merged_nodes=nodes)
            )
        run.clear()

    run_height = 0
    for sec in sections:
        mergeable = (
            sec.stype == "content"
            and sec.node.rect[3] < MERGE_MAX_HEIGHT
            and not sec.node.is_list_group
        )
        fits_run = (
            run
            and run[-1].parent_tags == sec.parent_tags
            and run_height + sec.node.rect[3] <= MERGE_RUN_MAX_HEIGHT
        )
        if mergeable and (not run or fits_run):
            run.append(sec)
            run_height += sec.node.rect[3]
        else:
            flush()
            run_height = 0
            if mergeable:
                run.append(sec)
                run_height = sec.node.rect[3]
            else:
                out.append(sec)
    flush()
    return out


def _cap_sections(sections: list[RawSection], max_sections: int) -> list[RawSection]:
    if len(sections) <= max_sections:
        return sections
    head = sections[: max_sections - 1]
    tail = sections[max_sections - 1 :]
    nodes = [n for r in tail for n in r.all_nodes()]
    overflow = RawSection(
        node=DomNode(tag="div", rect=_union_rect(nodes), children=nodes),
        parent_tags=tail[0].parent_tags,
        merged_nodes=nodes,
        stype="content",
    )
    return [*head, overflow]


def split_page(snapshot: DomSnapshot, max_sections: int = 60) -> list[RawSection]:
    """Main entry: snapshot -> ordered RawSections with types and item counts."""
    sections: list[RawSection] = []
    _split(snapshot.root, (), sections)

    for sec in sections:
        sec.stype = _classify(sec, snapshot.doc_height)
        sec.item_count = _list_item_count(sec.node)
        if sec.node.tag == "iframe":
            sec.stype = "iframe"

    sections = _coalesce_small(sections)
    # re-classify merged containers (composition may have changed)
    for sec in sections:
        if sec.merged_nodes:
            sec.stype = _classify(sec, snapshot.doc_height)
    sections = _cap_sections(sections, max_sections)
    return sections
