"""Pure core tests: DomSnapshot JSON fixtures -> build_page -> invariants + goldens.

No browser needed. Regenerate fixtures with:
    for p in ...; python -m ebrowse.dev http://127.0.0.1:8901/$p.html capture tests/fixtures/domsnapshots/$p.json
Regenerate goldens by running pytest with EBROWSE_UPDATE_GOLDENS=1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ebrowse.config import ObserveConfig
from ebrowse.core import render
from ebrowse.core.clickable import is_clickable
from ebrowse.core.fingerprint import RefRegistry, normalize_class, normalize_href
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import DomSnapshot

SNAPSHOT_DIR = Path(__file__).parent / "fixtures" / "domsnapshots"
GOLDEN_DIR = Path(__file__).parent / "golden"
PAGES = [
    "article", "form", "list", "table", "dropdown", "spa", "dialogs", "iframe", "huge",
    "custom_widgets",
]  # fmt: skip


def load_snapshot(name: str) -> DomSnapshot:
    return DomSnapshot.from_dict(json.loads((SNAPSHOT_DIR / f"{name}.json").read_text()))


def build(name: str, registry: RefRegistry | None = None):
    snap = load_snapshot(name)
    return build_page(snap, registry or RefRegistry(), ObserveConfig(), captured_at=0.0)


def check_golden(name: str, actual: str) -> None:
    path = GOLDEN_DIR / f"{name}.txt"
    if os.environ.get("EBROWSE_UPDATE_GOLDENS"):
        path.write_text(actual + "\n")
        return
    assert path.is_file(), f"golden missing: {path} (run with EBROWSE_UPDATE_GOLDENS=1)"
    assert actual + "\n" == path.read_text(), (
        f"golden mismatch for {name}; if intentional, rerun with EBROWSE_UPDATE_GOLDENS=1"
    )


# ------------------------------------------------------------- invariants ----


@pytest.mark.parametrize("name", PAGES)
def test_build_invariants(name: str):
    page, raw_by_sid = build(name)
    assert page.sections, name
    assert len(page.sections) <= ObserveConfig().max_sections

    seen_refs: set[str] = set()
    for s in page.sections:
        assert s.sid in raw_by_sid
        assert s.fingerprint
        assert s.content_hash
        assert s.token_estimate >= 1
        for e in s.elements:
            assert e.ref not in seen_refs, f"duplicate ref {e.ref}"
            seen_refs.add(e.ref)

    # every clickable/candidate node with area ends up as exactly one element
    total_clickable = 0
    for sid, raw in raw_by_sid.items():
        del sid
        for n in raw.iter_walk():
            if (is_clickable(n) or n.candidate) and n.ref:
                total_clickable += 1
    assert total_clickable == len(seen_refs)


@pytest.mark.parametrize("name", PAGES)
def test_ref_stability_across_rebuilds(name: str):
    registry = RefRegistry()
    page1, _ = build(name, registry)
    page2, _ = build(name, registry)
    refs1 = [(e.ref, e.desc.match_key()) for s in page1.sections for e in s.elements]
    refs2 = [(e.ref, e.desc.match_key()) for s in page2.sections for e in s.elements]
    assert refs1 == refs2


def test_refs_shared_across_pages_for_common_chrome():
    """form.html and list.html share the Fixture Shop header; nav links must
    keep the same refs across the two pages (cross-page ref durability)."""
    registry = RefRegistry()
    form_page, _ = build("form", registry)
    list_page, _ = build("list", registry)

    def href_refs(page):
        return {
            e.desc.href: e.ref
            for s in page.sections
            for e in s.elements
            if e.desc.tag == "a" and e.desc.href
        }

    form_refs, list_refs = href_refs(form_page), href_refs(list_page)
    shared = set(form_refs) & set(list_refs)
    assert shared, "expected shared nav hrefs between fixtures"
    for href in shared:
        assert form_refs[href] == list_refs[href], href


def test_oversized_childless_overlay_kept_backdrop_dropped():
    # A full-viewport veil with only a text node and a click signal must become
    # a section (it is the thing blocking every click); a bare decorative
    # backdrop with no text/signals must still be dropped.
    from ebrowse.core.snapshot import DomNode
    from ebrowse.core.split import split_page

    veil = DomNode(
        tag="div",
        rect=(0, 0, 1280, 2000),
        attrs={"id": "veil"},
        text="We value your privacy — click to dismiss",
        signals={"ls": 1},
    )
    backdrop = DomNode(tag="div", rect=(0, 0, 1280, 2000))
    main = DomNode(
        tag="main",
        rect=(0, 0, 1280, 600),
        children=[DomNode(tag="p", rect=(0, 0, 600, 40), text="Hello")],
    )
    root = DomNode(tag="body", rect=(0, 0, 1280, 2000), children=[main, veil, backdrop])
    snap = DomSnapshot(
        url="https://x.test/",
        title="t",
        viewport=(1280, 1280),
        scroll_y=0,
        doc_height=2000,
        truncated=False,
        root=root,
    )
    secs = split_page(snap)
    assert any("value your privacy" in s.node.subtree_text() for s in secs)
    assert all(n is not backdrop for s in secs for n in s.iter_walk())


def test_huge_page_collapses_to_list():
    page, _ = build("huge")
    assert len(page.sections) <= 6
    list_sections = [s for s in page.sections if s.type == "list"]
    assert list_sections and list_sections[0].item_count == 120


def test_iframe_content_stitched():
    page, _ = build("iframe")
    framed = [e for s in page.sections for e in s.elements if e.desc.iframe_path]
    assert framed, "payment form inputs inside the iframe should be elements"
    assert any(e.desc.placeholder == "MM/YY" for e in framed)


def test_form_section_detected():
    page, _ = build("form")
    forms = [s for s in page.sections if s.type == "form"]
    assert forms
    f = forms[0]
    kinds = {e.desc.input_type for e in f.elements if e.desc.input_type}
    assert {"text", "email", "password", "checkbox", "radio"} <= kinds
    selects = [e for e in f.elements if e.desc.tag == "select"]
    assert selects and "Canada" in (selects[0].state.options or [])


def test_table_items_counted():
    page, _ = build("table")
    tables = [s for s in page.sections if s.type == "table"]
    assert tables and tables[0].item_count == 25


def test_oversized_form_promotes_nested_table_without_losing_controls():
    """A semantic form is not an unconditional terminal once it exceeds budget."""
    from ebrowse.core.snapshot import DomNode

    def cell(tag: str, text: str):
        return DomNode(tag=tag, rect=(0, 0, 300, 30), text=text)

    rows = [
        DomNode(
            tag="tr",
            rect=(0, 100 + i * 30, 600, 30),
            children=[cell("td", f"row {i} " + "detail " * 30), cell("td", f"${i}.00")],
        )
        for i in range(12)
    ]
    table = DomNode(
        tag="table",
        rect=(0, 100, 600, 500),
        children=[
            DomNode(
                tag="thead",
                rect=(0, 100, 600, 30),
                children=[
                    DomNode(
                        tag="tr",
                        rect=(0, 100, 600, 30),
                        children=[cell("th", "Product"), cell("th", "Price")],
                    )
                ],
            ),
            DomNode(tag="tbody", rect=(0, 130, 600, 470), children=rows),
        ],
    )
    before = DomNode(
        tag="input",
        rect=(0, 40, 200, 30),
        attrs={"nm": "Filter", "typ": "search"},
        signals={"tg": 1},
    )
    after = DomNode(tag="button", rect=(0, 620, 100, 30), text="Export", signals={"tg": 1})
    spacer = DomNode(tag="div", rect=(0, 80, 700, 0), attrs={"id": "layout-anchor"})
    form = DomNode(tag="form", rect=(0, 0, 700, 700), children=[before, spacer, table, after])
    snap = DomSnapshot(
        url="https://x.test/",
        title="Orders",
        viewport=(1280, 800),
        scroll_y=0,
        doc_height=700,
        truncated=False,
        root=DomNode(tag="body", rect=(0, 0, 1280, 700), children=[form]),
    )
    cfg = ObserveConfig(max_section_tokens=200)
    page, raws = build_page(snap, RefRegistry(), cfg, captured_at=0)

    assert [s.type for s in page.sections] == ["form", "table", "form"]
    assert page.sections[1].item_count == 12
    assert {e.desc.name for e in page.sections[0].elements} == {"Filter"}
    assert {e.desc.text_head for e in page.sections[2].elements} == {"Export"}
    assert len({e.ref for s in page.sections for e in s.elements}) == 2
    query = render.render_query(page.sections[1], raws["s2"], cfg, filter_expr="row 7")
    assert "matched 1 of 12 items" in query and "row 7" in query


def test_descending_preserves_clickable_wrapper_identity_and_direct_text():
    from ebrowse.core.snapshot import DomNode

    wrapper = DomNode(
        tag="div",
        rect=(0, 0, 700, 1200),
        text="Open report",
        children=[
            DomNode(tag="section", rect=(0, 50, 600, 500), text="Quarterly results"),
            DomNode(tag="section", rect=(0, 600, 600, 500), text="Regional results"),
        ],
        signals={"ls": 1},
    )
    snap = DomSnapshot(
        url="https://x.test/",
        title="Report",
        viewport=(1280, 800),
        scroll_y=0,
        doc_height=1200,
        truncated=False,
        root=DomNode(tag="body", rect=(0, 0, 1280, 1200), children=[wrapper]),
    )
    page, raws = build_page(snap, RefRegistry(), ObserveConfig(), captured_at=0)
    elements = [e for s in page.sections for e in s.elements]
    assert len(elements) == 1
    assert elements[0].desc.text_head.startswith("Open report Quarterly results")
    expanded = "\n".join(
        render.render_section_markdown(s, raws[s.sid], ObserveConfig()) for s in page.sections
    )
    assert "Open report" in expanded
    assert "Quarterly results" in expanded
    assert "Regional results" in expanded


def test_collection_adapter_supports_multiple_tbody_and_aria_grid():
    from ebrowse.core.collection import collection_items, collection_kind, table_cells
    from ebrowse.core.snapshot import DomNode

    def row(text: str):
        return DomNode(
            tag="tr",
            rect=(0, 0, 100, 20),
            children=[DomNode(tag="td", rect=(0, 0, 100, 20), text=text)],
        )

    table = DomNode(
        tag="table",
        rect=(0, 0, 100, 100),
        children=[
            DomNode(tag="tbody", rect=(0, 0, 100, 40), children=[row("one"), row("two")]),
            DomNode(tag="tbody", rect=(0, 40, 100, 20), children=[row("three")]),
        ],
    )
    assert [r.subtree_text() for r in collection_items(table)] == ["one", "two", "three"]

    grid_row = DomNode(
        tag="div",
        rect=(0, 0, 100, 20),
        attrs={"role": "row"},
        children=[DomNode(tag="div", rect=(0, 0, 100, 20), attrs={"role": "gridcell"}, text="A")],
    )
    grid = DomNode(tag="div", rect=(0, 0, 100, 100), attrs={"role": "grid"}, children=[grid_row])
    assert collection_kind(grid) == "table"
    assert collection_items(grid) == [grid_row]
    assert table_cells(grid_row)[0].text == "A"


def test_max_sections_is_soft_when_only_collections_remain():
    from ebrowse.core.snapshot import DomNode
    from ebrowse.core.split import split_page

    lists = [
        DomNode(
            tag="ul",
            rect=(0, i * 100, 500, 80),
            children=[DomNode(tag="li", rect=(0, i * 100, 500, 20), text=f"item {i}")],
        )
        for i in range(4)
    ]
    snap = DomSnapshot(
        url="https://x.test/",
        title="Lists",
        viewport=(1280, 800),
        scroll_y=0,
        doc_height=500,
        truncated=False,
        root=DomNode(tag="body", rect=(0, 0, 1280, 500), children=lists),
    )
    sections = split_page(snap, max_sections=1)
    assert len(sections) == 4
    assert all(section.stype == "list" for section in sections)


def test_ordinary_sections_respect_expansion_budget_at_child_boundaries():
    from ebrowse.core.snapshot import DomNode

    children = [
        DomNode(tag="p", rect=(0, i * 50, 600, 40), text=f"paragraph {i} " + "word " * 45)
        for i in range(10)
    ]
    article = DomNode(tag="article", rect=(0, 0, 700, 600), children=children)
    snap = DomSnapshot(
        url="https://x.test/",
        title="Article",
        viewport=(1280, 800),
        scroll_y=0,
        doc_height=600,
        truncated=False,
        root=DomNode(tag="body", rect=(0, 0, 1280, 600), children=[article]),
    )
    cfg = ObserveConfig(max_section_tokens=100)
    page, _ = build_page(snap, RefRegistry(), cfg, captured_at=0)
    assert len(page.sections) > 1
    assert all(s.type != "list" and s.type != "table" for s in page.sections)
    assert all(s.token_estimate <= cfg.max_section_tokens for s in page.sections)


def test_outline_warns_when_snapshot_capture_was_truncated():
    page, _ = build("article")
    page.truncated = True
    outline = render.render_outline(page)
    assert "NOTE snapshot truncated" in outline
    assert "ebrowse screenshot --full" in outline


def test_collection_default_page_is_token_budgeted_but_all_is_explicit_escape_hatch():
    snap = load_snapshot("huge")
    cfg = ObserveConfig(max_section_tokens=200)
    page, raws = build_page(snap, RefRegistry(), cfg, captured_at=0)
    section = next(s for s in page.sections if s.type == "list")
    default = render.render_section_markdown(section, raws[section.sid], cfg)
    full = render.render_section_markdown(section, raws[section.sid], cfg, show_all=True)
    assert section.token_estimate <= cfg.max_section_tokens
    assert "more items" in default
    assert len(full) > len(default) * 5


def test_collection_only_oversized_dialog_keeps_modal_section():
    from ebrowse.core.snapshot import DomNode

    items = [
        DomNode(tag="li", rect=(0, i * 20, 500, 20), text="choice " + "detail " * 30)
        for i in range(20)
    ]
    dialog = DomNode(
        tag="dialog",
        rect=(0, 0, 600, 600),
        attrs={"nm": "Choose records"},
        children=[DomNode(tag="ul", rect=(0, 0, 500, 500), children=items)],
    )
    snap = DomSnapshot(
        url="https://x.test/",
        title="Dialog",
        viewport=(1280, 800),
        scroll_y=0,
        doc_height=800,
        truncated=False,
        root=DomNode(tag="body", rect=(0, 0, 1280, 800), children=[dialog]),
    )
    page, _ = build_page(snap, RefRegistry(), ObserveConfig(max_section_tokens=100), captured_at=0)
    assert [section.type for section in page.sections] == ["dialog", "list"]
    assert page.sections[0].heading == "Choose records"


def test_taller_small_siblings_coalesce_with_same_owner():
    from ebrowse.core.snapshot import DomNode

    parent = DomNode(
        tag="div",
        rect=(0, 0, 700, 1200),
        children=[
            DomNode(tag="section", rect=(0, 0, 600, 300), text="First cohesive block"),
            DomNode(tag="section", rect=(0, 310, 600, 300), text="Second cohesive block"),
        ],
    )
    snap = DomSnapshot(
        url="https://x.test/",
        title="Blocks",
        viewport=(1280, 800),
        scroll_y=0,
        doc_height=1200,
        truncated=False,
        root=DomNode(tag="body", rect=(0, 0, 1280, 1200), children=[parent]),
    )
    page, _ = build_page(snap, RefRegistry(), ObserveConfig(), captured_at=0)
    content = [section for section in page.sections if section.type == "content"]
    assert len(content) == 1
    assert "First cohesive block" in content[0].preview


def test_same_owner_form_fragments_are_coalescible():
    from ebrowse.core.snapshot import DomNode
    from ebrowse.core.split import RawSection, _coalesce_small

    first = RawSection(
        node=DomNode(tag="div", rect=(0, 0, 500, 280), text="First fields"),
        parent_tags=("body", "form"),
        stype="form",
        context_key=42,
        estimated_chars=100,
    )
    second = RawSection(
        node=DomNode(tag="div", rect=(0, 290, 500, 280), text="Second fields"),
        parent_tags=("body", "form"),
        stype="form",
        context_key=42,
        estimated_chars=100,
    )
    merged = _coalesce_small([first, second], max_chars=1000)
    assert len(merged) == 1
    assert merged[0].stype == "form"
    assert merged[0].node.subtree_text() == "First fields Second fields"


def test_heading_only_section_attaches_to_taller_following_content():
    from ebrowse.core.snapshot import DomNode

    parent = DomNode(
        tag="div",
        rect=(0, 0, 700, 1200),
        children=[
            DomNode(tag="h2", rect=(0, 0, 600, 50), text="Coverage details"),
            DomNode(
                tag="article",
                rect=(0, 60, 600, 700),
                children=[DomNode(tag="p", rect=(0, 60, 600, 100), text="Covered services")],
            ),
        ],
    )
    snap = DomSnapshot(
        url="https://x.test/",
        title="Coverage",
        viewport=(1280, 800),
        scroll_y=0,
        doc_height=1200,
        truncated=False,
        root=DomNode(tag="body", rect=(0, 0, 1280, 1200), children=[parent]),
    )
    page, raws = build_page(snap, RefRegistry(), ObserveConfig(), captured_at=0)
    assert len(page.sections) == 1
    assert page.sections[0].heading == "Coverage details"
    expanded = render.render_section_markdown(page.sections[0], raws["s1"], ObserveConfig())
    assert "Covered services" in expanded


def test_candidate_discovery_semantics():
    """Weak-evidence candidates: exposed with provenance in expand, excluded
    from outline counts, suppressed around strong elements, decoys dropped."""
    page, raw_by_sid = build("custom_widgets")
    s2 = page.section("s2")
    assert s2 is not None
    # every custom widget on this page has a real listener, and "listener" is
    # the top of the evidence ladder, so all candidates carry that provenance
    by_id = {e.desc.id: e for e in s2.elements if e.state.candidate}
    assert set(by_id) == {"save-action", "plan-card", "dark-toggle", "notif-expander"}
    assert all(e.state.candidate == "listener" for e in by_id.values())
    texts = {e.desc.text_head for e in s2.elements}
    assert any("Save changes" in t for t in texts)
    # decoy with zero signals never becomes an element
    assert not any(t == "Settings saved automatically" for t in texts)
    # the tabindex wrapper around native buttons is suppressed; buttons stay.
    # the listbox is a container widget: it AND its option children are all
    # strong elements (a role=listbox is clickable, its role=option items are
    # the real targets — clickable.py) — so the qty wrapper is the only
    # tabindex node that must not appear.
    strong = {e.desc.text_head for e in s2.elements if not e.state.candidate}
    assert {"−", "+", "Recently Used", "All", "Accounts"} <= strong
    assert "1" not in strong  # #quantity-row tabindex wrapper suppressed
    # outline counts ignore candidates entirely (listbox + 3 options + 2 qty
    # buttons are all strong; the 4 weak custom widgets are excluded)
    assert s2.counts_desc() == "3 links, 1 input, 2 buttons"
    # expand renders the '?' provenance marker, outline never does
    md = render.render_section_markdown(s2, raw_by_sid["s2"], ObserveConfig(), cursor=0)
    assert "(@e4 ?)" in md
    assert "?" not in render.render_outline(page)


def test_container_widget_children_render_their_own_refs():
    """A clickable ARIA container (role=listbox) must not swallow the refs of
    its interactive descendants: the renderer descends past a ref'd node that
    has ref'd descendants. Regression for the Salesforce Category listbox that
    rendered as one opaque [Category] with 16 invisible-but-clickable options.
    """
    page, raw_by_sid = build("custom_widgets")
    s2 = page.section("s2")
    assert s2 is not None
    refs = {e.desc.text_head: e.ref for e in s2.elements}
    container_ref = next(e.ref for e in s2.elements if e.desc.role == "listbox")
    md = render.render_section_markdown(s2, raw_by_sid["s2"], ObserveConfig(), cursor=0)
    # the container ref AND every option ref appear as distinct lines
    assert f"({container_ref})" in md
    for opt in ("Recently Used", "All", "Accounts"):
        assert f"[{opt} ({refs[opt]})]" in md.replace("](→ #)", "]")


def test_section_fingerprints_stable_and_distinct():
    page1, _ = build("article")
    page2, _ = build("article")
    fps1 = [s.fingerprint for s in page1.sections]
    fps2 = [s.fingerprint for s in page2.sections]
    assert fps1 == fps2
    assert len(set(fps1)) == len(fps1), "fingerprints should be distinct on this page"


# ---------------------------------------------------------------- goldens ----


@pytest.mark.parametrize("name", PAGES)
def test_golden_outline(name: str):
    page, _ = build(name)
    check_golden(f"outline_{name}", render.render_outline(page))


def test_outline_preview_appends_text_to_summary():
    """`outline --preview` (hybrid line): a short verbatim preview is appended
    after the ≈ summary, keeping both provenance markers. Default is unchanged
    and sections without a summary are byte-identical between the two modes."""
    page, _ = build("list")
    sec = next(s for s in page.sections if not s.cross_origin and (s.heading or s.preview))
    sec.summary = "Injected test summary"

    default = render.render_outline(page)
    combined = render.render_outline(page, preview=True, preview_chars=40)

    def line(text: str, sid: str) -> str:
        return next(ln for ln in text.splitlines() if ln.startswith(sid + " "))

    dline, cline = line(default, sec.sid), line(combined, sec.sid)
    # default: summary only
    assert "≈ Injected test summary" in dline and '| "' not in dline
    # preview: summary first, then a verbatim `|` preview — both markers, in order
    assert '≈ Injected test summary  | "' in cline
    assert cline.index("≈") < cline.index("|")
    # the appended preview honors the char cap (+ quotes/ellipsis overhead)
    assert len(cline) - len(dline) <= 40 + 6

    # sections that have no summary render identically in both modes
    for s in page.sections:
        if s.summary is None:
            assert line(default, s.sid) == line(combined, s.sid)


@pytest.mark.parametrize(
    ("name", "sid", "cursor"),
    [("article", "s2", 0), ("form", "s2", 0), ("list", "s4", 0), ("list", "s4", 20),
     ("table", "s4", 0), ("dropdown", "s2", 0), ("custom_widgets", "s2", 0)],
)  # fmt: skip
def test_golden_expand(name: str, sid: str, cursor: int):
    page, raw_by_sid = build(name)
    section = page.section(sid)
    assert section is not None, f"{name} has no {sid}"
    md = render.render_section_markdown(section, raw_by_sid[sid], ObserveConfig(), cursor=cursor)
    check_golden(f"expand_{name}_{sid}_{cursor}", md)


# ------------------------------------------------------------------ units ----


def test_disabled_controls_still_clickable_class():
    # disabled controls keep refs (agents must see the grayed-out submit);
    # weak-evidence candidates stay gated on enabled
    from ebrowse.core.clickable import candidate_evidence, is_clickable
    from ebrowse.core.snapshot import DomNode

    btn = DomNode(tag="button", rect=(0, 0, 100, 30), attrs={"dis": 1}, signals={"tg": 1})
    assert is_clickable(btn)
    div = DomNode(tag="div", rect=(0, 0, 100, 30), attrs={"dis": 1}, signals={"el": 1})
    assert candidate_evidence(div) is None


def test_candidate_evidence_ladder():
    from ebrowse.core.clickable import candidate_evidence
    from ebrowse.core.snapshot import DomNode

    def mk(signals, attrs=None):
        return DomNode(tag="div", rect=(0, 0, 100, 30), attrs=attrs or {}, signals=signals)

    assert candidate_evidence(mk({"el": 1})) == "listener"
    assert candidate_evidence(mk({"tb": 1})) == "focusable"
    assert candidate_evidence(mk({"as": 1})) == "aria-state"
    assert candidate_evidence(mk({"el": 1, "tb": 1, "as": 1})) == "listener"
    assert candidate_evidence(mk({"tg": 1})) is None  # strong signal: not a candidate
    assert candidate_evidence(mk({"el": 1}, {"dis": 1})) is None  # disabled gate
    assert candidate_evidence(mk({"el": 1, "cp": 1})) is None  # strong wins
    assert candidate_evidence(mk({})) is None


def _select_node(n_opts: int, total: int | None = None, multi: bool = False):
    from ebrowse.core.snapshot import DomNode

    attrs = {"nm": "Country", "opt": [f"Opt {i}" for i in range(1, n_opts + 1)], "sel": "Opt 1"}
    if total:
        attrs["optn"] = total
    if multi:
        attrs["mul"] = 1
    node = DomNode(tag="select", rect=(0, 0, 200, 30), attrs=attrs)
    node.ref = "@e5"
    return node


def test_render_select_options_pagination():
    out = render.render_select_options(_select_node(120))
    assert out.startswith('SELECT Country (@e5) ▾ "Opt 1" — 120 options')
    assert "1. Opt 1" in out and "50. Opt 50" in out and "51. Opt 51" not in out
    assert "… 70 more options — expand @e5 --cursor 50" in out
    out = render.render_select_options(_select_node(120), cursor=100)
    assert "(options 101–120 of 120)" in out
    assert "120. Opt 120" in out and "more options" not in out
    out = render.render_select_options(_select_node(120), show_all=True)
    assert "120. Opt 120" in out and "more options" not in out


def test_render_select_options_truncated_capture_and_multiple():
    # tail past the capture cap: honestly absent, live-match escape hatch named
    out = render.render_select_options(_select_node(350, total=400), cursor=300)
    assert "— 400 options" in out
    assert "(options 301–350 of 400)" in out
    assert "options beyond 350 were not captured" in out
    out = render.render_select_options(_select_node(8, multi=True))
    assert "— 8 options, multiple" in out
    assert "8. Opt 8" in out and "not captured" not in out


def test_normalize_class_strips_state():
    assert normalize_class("btn btn-primary is-active css-1x2y3z open") == "btn btn-primary"
    assert normalize_class("menu selected hover") == "menu"


def test_normalize_href():
    page_url = "https://shop.example.com/a/b"
    assert normalize_href("https://shop.example.com/x?q=1", page_url) == "/x?q=1"
    assert normalize_href("/y", page_url) == "/y"
    assert normalize_href("https://other.com/z", page_url) == "https://other.com/z"
    assert normalize_href("javascript:void(0)", page_url) is None
    assert normalize_href("#top", page_url) == "#top"


def test_registry_nth_disambiguation():
    from ebrowse.model import ElementDesc

    registry = RefRegistry()
    descs = [ElementDesc(tag="a", href="/same"), ElementDesc(tag="a", href="/same")]
    refs = registry.assign(descs)
    assert refs == ["@e1", "@e2"]
    assert descs[0].nth_hint == 0 and descs[1].nth_hint == 1
    # same page again: stable
    descs2 = [ElementDesc(tag="a", href="/same"), ElementDesc(tag="a", href="/same")]
    assert registry.assign(descs2) == ["@e1", "@e2"]
    # one disappears: survivor keeps first slot (strict order-based matching)
    assert registry.assign([ElementDesc(tag="a", href="/same")]) == ["@e1"]


def test_identity_mismatch_rules():
    from ebrowse.core.locate import identity_mismatch
    from ebrowse.model import ElementDesc

    btn = ElementDesc(tag="button", role="button", text_head="Remove", id="rm-3")
    ok = {"tag": "button", "id": "rm-3", "testid": None, "text": "Remove"}
    assert identity_mismatch(btn, ok) is None
    # strong facts are strict: tag, and id/testid when the descriptor has them
    assert identity_mismatch(btn, {**ok, "tag": "a"})
    assert identity_mismatch(btn, {**ok, "id": "rm-0"})
    assert identity_mismatch(btn, {**ok, "id": None})
    tid = ElementDesc(tag="button", text_head="Remove", testid="cart-rm")
    assert identity_mismatch(tid, {"tag": "button", "testid": "other", "text": "Remove"})
    # descriptor without id/testid doesn't care what the live element carries
    anon = ElementDesc(tag="button", role="button", text_head="Remove")
    assert identity_mismatch(anon, ok) is None
    # text: lenient to truncation/extension, whitespace, and case...
    long = ElementDesc(tag="a", text_head="Read the full story about the thing"[:20])
    assert (
        identity_mismatch(long, {"tag": "a", "text": "Read the full story about the thing"}) is None
    )
    assert identity_mismatch(anon, {"tag": "button", "text": "  REMOVE\nitem "}) is None
    # ...and to a live element we can't read text from
    assert identity_mismatch(anon, {"tag": "button", "text": ""}) is None
    # ...but clearly different text means a different sibling -> refuse
    r = identity_mismatch(
        ElementDesc(tag="button", text_head="Dismiss notice B"),
        {"tag": "button", "text": "Dismiss notice A"},
    )
    assert r and "text" in r
    # in doubt, refuse: a full in-place relabel also mismatches (ADR 0003)
    assert identity_mismatch(
        ElementDesc(tag="button", text_head="Add to cart"), {"tag": "button", "text": "Added"}
    )
    # form controls: rendered text is state, not identity
    sel = ElementDesc(tag="select", text_head="Red Green Blue")
    assert identity_mismatch(sel, {"tag": "select", "text": "Green"}) is None
