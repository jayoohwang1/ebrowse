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
    # the tabindex wrapper around native buttons is suppressed; buttons stay
    strong = [e for e in s2.elements if not e.state.candidate]
    assert {e.desc.text_head for e in strong} == {"−", "+"}
    # outline counts ignore candidates entirely
    assert s2.counts_desc() == "2 buttons"
    # expand renders the '?' provenance marker, outline never does
    md = render.render_section_markdown(s2, raw_by_sid["s2"], ObserveConfig(), cursor=0)
    assert "(@e4 ?)" in md
    assert "?" not in render.render_outline(page)


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
