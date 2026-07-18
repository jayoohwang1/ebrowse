"""Pure orchestration: DomSnapshot -> PageMem.

build_page() coordinates split -> extract -> refs -> labels -> fingerprints and
is shared by the dev harness and the daemon session.
No Playwright, no I/O.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from ebrowse import debug
from ebrowse.config import ObserveConfig
from ebrowse.core import render
from ebrowse.core.clickable import (
    candidate_evidence,
    implicit_role,
    is_clickable,
    is_container_widget,
    is_form_control,
)
from ebrowse.core.fingerprint import (
    RefRegistry,
    content_hash,
    normalize_href,
    section_fingerprint,
)
from ebrowse.core.label import deterministic_label, media_label, section_heading, section_preview
from ebrowse.core.snapshot import DomNode, DomSnapshot
from ebrowse.core.split import RawSection, split_page
from ebrowse.model import (
    BBox,
    Element,
    ElementDesc,
    ElementState,
    PageMem,
    Section,
    estimate_tokens,
)

_TEXT_HEAD_LEN = 80
_MIN_IMG_PX = 80  # images smaller than this (either side) get no @i ref


def _desc_for(node: DomNode, page_url: str) -> ElementDesc:
    a = node.attrs
    return ElementDesc(
        tag=node.tag,
        role=implicit_role(node.tag, a),
        id=a.get("id"),
        testid=a.get("tid"),
        name=a.get("nm"),
        placeholder=a.get("ph"),
        href=normalize_href(a.get("href", ""), page_url),
        input_type=a.get("typ"),
        text_head=(node.identity_text or node.subtree_text(cap=_TEXT_HEAD_LEN * 2))[
            :_TEXT_HEAD_LEN
        ],
        iframe_path=node.iframe_path,
    )


def _state_for(node: DomNode) -> ElementState:
    a = node.attrs
    value = a.get("val")
    if value is None and a.get("sel") is not None:
        value = a.get("sel")
    checked = None
    if "chk" in a:
        checked = bool(a["chk"])
    expanded = None
    if "exp" in a:
        expanded = bool(a["exp"])
    pressed = bool(a["prs"]) if "prs" in a else None
    selected = bool(a["asel"]) if "asel" in a else None
    return ElementState(
        bbox=BBox(*node.rect),
        visible=True,
        value=value,
        checked=checked,
        disabled=bool(a.get("dis")),
        expanded=expanded,
        options=a.get("opt"),
        pressed=pressed,
        selected=selected,
        candidate=node.candidate,
    )


def _contains_clickable(node: DomNode) -> bool:
    return any(is_clickable(d) for d in node.walk() if d is not node)


def extract_element_nodes(raw: RawSection) -> list[DomNode]:
    """Clickable nodes in document order, suppressing nested clickables.

    A descendant of an <a>/<button> is not its own element unless it is a form
    control (adapted from WebChallenger contained_in logic).

    Weak-evidence CANDIDATES (clickable.candidate_evidence: real listener,
    tabindex, role-less ARIA state) are also extracted, annotated via
    node.candidate, but suppressed when they contain or sit inside a strong
    clickable — the strong element is the real target and duplicate refs are
    the main cost of recall (e.g. a tabindex wrapper around native buttons).
    """
    out: list[DomNode] = []

    def rec(node: DomNode, inside_widget: bool) -> None:
        clickable = is_clickable(node)
        if clickable and (not inside_widget or is_form_control(node)):
            out.append(node)
            # container widgets (listbox/menu/…) don't own their descendants:
            # the items inside are the actual click targets
            if not is_container_widget(node):
                inside_widget = True
        elif node.tag in ("a", "button") and not inside_widget:
            inside_widget = clickable
        elif not inside_widget:
            evidence = candidate_evidence(node)
            if evidence and not _contains_clickable(node):
                node.candidate = evidence
                out.append(node)
        for c in node.children:
            rec(c, inside_widget)

    for n in raw.all_nodes():
        rec(n, False)
    return out


def build_page(
    snapshot: DomSnapshot,
    registry: RefRegistry,
    observe: ObserveConfig,
    nav_id: int = 0,
    captured_at: float | None = None,
) -> tuple[PageMem, dict[str, RawSection]]:
    """Build a PageMem. Also returns sid -> RawSection so callers (expand,
    screenshots) can reach the underlying DOM subtree of each section."""
    with debug.timed("split", "split"):
        raws = split_page(
            snapshot,
            max_sections=observe.max_sections,
            max_section_tokens=observe.max_section_tokens,
        )
    debug.emit("split", "sections", count=len(raws))

    # 1) extract all element nodes page-wide (document order matters for refs)
    section_nodes: list[list[DomNode]] = [extract_element_nodes(r) for r in raws]
    all_nodes = [n for nodes in section_nodes for n in nodes]
    descs = [_desc_for(n, snapshot.url) for n in all_nodes]
    minted_before = len(registry)
    refs = registry.assign(descs)
    debug.emit(
        "fingerprint",
        "refs_assigned",
        total=len(refs),
        minted=len(registry) - minted_before,
        reused=len(refs) - (len(registry) - minted_before),
    )
    for node, ref in zip(all_nodes, refs, strict=True):
        node.ref = ref  # annotate for the markdown renderer

    # 1b) large images get page-scoped @i refs (NOT durable across observes,
    # unlike @e refs — they exist for screenshots + captions, not actions)
    img_n = 0
    for raw in raws:
        for n in raw.iter_walk():
            if n.tag == "img" and n.rect[2] >= _MIN_IMG_PX and n.rect[3] >= _MIN_IMG_PX:
                img_n += 1
                n.ref = f"@i{img_n}"

    # 2) build sections
    sections: list[Section] = []
    raw_by_sid: dict[str, RawSection] = {}
    i = 0
    for idx, raw in enumerate(raws):
        identity_node = raw.fingerprint_node or raw.node
        nodes = section_nodes[idx]
        elements = []
        for node in nodes:
            desc = descs[i]
            elements.append(Element(ref=refs[i], desc=desc, state=_state_for(node)))
            i += 1

        heading = section_heading(raw)
        preview = section_preview(raw, heading, observe.preview_chars)
        if not preview and not heading:
            # image-only sections (hero banners, decorative strips) have no
            # text; alt text is the only deterministic label available
            preview = media_label(raw) or ""
        sid = f"s{idx + 1}"
        cross = raw.node.tag == "iframe" and raw.node.cross_origin
        section = Section(
            sid=sid,
            fingerprint=section_fingerprint(
                tag=identity_node.tag,
                cls=identity_node.attrs.get("cls", ""),
                role=identity_node.attrs.get("role", ""),
                heading=heading or "",
                iframe_path=identity_node.iframe_path,
                parent_tags=raw.parent_tags,
            ),
            type=raw.stype,
            heading=heading,
            preview=preview,
            elements=elements,
            content_hash=content_hash(
                " ".join(n.subtree_text(cap=2000) for n in raw.all_nodes()),
                [d.match_key() for d in (e.desc for e in elements)],
            ),
            token_estimate=0,  # filled below from the real rendering
            bbox=BBox(*raw.node.rect),
            item_count=raw.item_count,
            iframe_path=raw.node.iframe_path,
            cross_origin=cross,
        )
        if cross:
            src = raw.node.attrs.get("src", "")
            host = urlsplit(src).netloc if "//" in src else src
            section.preview = f"cross-origin: {host or 'unknown'}"
        section.token_estimate = estimate_tokens(
            render.render_section_markdown(section, raw, observe)
        )
        sections.append(section)
        raw_by_sid[sid] = raw

    page = PageMem(
        url=snapshot.url,
        title=snapshot.title,
        sections=sections,
        captured_at=captured_at if captured_at is not None else time.time(),
        nav_id=nav_id,
        truncated=snapshot.truncated,
    )
    return page, raw_by_sid


def outline_label(section: Section) -> str:
    """Deterministic outline label (used when no summary is cached)."""
    return deterministic_label(section.heading, section.preview)
