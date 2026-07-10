"""Pure diff-engine tests: mutate captured DomSnapshots, rebuild, diff."""

from __future__ import annotations

import json
from pathlib import Path

from ebrowse.config import ObserveConfig
from ebrowse.core import render
from ebrowse.core.diff import added_text, diff_pages, navigation_diff
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import DomSnapshot

SNAPSHOT_DIR = Path(__file__).parent / "fixtures" / "domsnapshots"

_DIALOG_NODE = {
    "t": "div",
    "a": {"role": "dialog", "nm": "Cookie consent"},
    "r": [200, 200, 400, 200],
    "x": "We use cookies. Accept our policy to continue browsing this site today.",
    "c": [{"t": "button", "r": [220, 320, 80, 30], "x": "Accept", "k": {"tg": 1}}],
}


def _load_raw(name: str) -> dict:
    return json.loads((SNAPSHOT_DIR / f"{name}.json").read_text())


def _walk_dicts(node: dict):
    yield node
    for c in node.get("c", []) or []:
        yield from _walk_dicts(c)


def _build(raw: dict, registry: RefRegistry):
    snap = DomSnapshot.from_dict(raw)
    return build_page(snap, registry, ObserveConfig(), captured_at=0.0)[0]


def _build_full(raw: dict, registry: RefRegistry):
    snap = DomSnapshot.from_dict(raw)
    return build_page(snap, registry, ObserveConfig(), captured_at=0.0)


def test_no_change_on_identical_rebuild():
    registry = RefRegistry()
    raw = _load_raw("form")
    d = diff_pages(_build(raw, registry), _build(raw, registry))
    assert d.kind == "no_change"


def test_input_value_change_detected():
    registry = RefRegistry()
    raw = _load_raw("form")
    prev = _build(raw, registry)
    for n in _walk_dicts(raw["root"]):
        if n.get("a", {}).get("id") == "fullname":
            n["a"]["val"] = "Jayoo"
    new = _build(raw, registry)
    d = diff_pages(prev, new)
    assert d.kind == "partial"
    changes = [c for sd in d.sections for c in sd.state_changes]
    assert any(f == "value" and nv == "Jayoo" for _r, f, _ov, nv in changes)


def test_removed_elements_detected():
    registry = RefRegistry()
    raw = _load_raw("form")
    prev = _build(raw, registry)
    for n in _walk_dicts(raw["root"]):
        kids = n.get("c")
        if kids:
            n["c"] = [c for c in kids if c.get("a", {}).get("id") != "submit-btn"]
    new = _build(raw, registry)
    d = diff_pages(prev, new)
    assert d.kind == "partial"
    removed = [r for sd in d.sections for r in sd.removed]
    assert any("Create account" in (r.name or r.text_head or "") for r in removed)


def test_appeared_dialog_section_sets_dialog_kind():
    registry = RefRegistry()
    raw = _load_raw("article")
    prev = _build(raw, registry)
    raw["root"]["c"] = (raw["root"].get("c") or []) + [
        {
            "t": "div",
            "a": {"role": "dialog", "nm": "Cookie consent"},
            "r": [200, 200, 400, 200],
            "x": "We use cookies. Accept our policy to continue browsing this site today.",
            "c": [
                {"t": "button", "r": [220, 320, 80, 30], "x": "Accept", "k": {"tg": 1}},
            ],
        }
    ]
    new = _build(raw, registry)
    d = diff_pages(prev, new)
    assert d.kind == "dialog"
    appeared = [sd for sd in d.sections if sd.kind == "appeared"]
    assert appeared and appeared[0].section.type == "dialog"


def test_render_diff_expands_appeared_dialog():
    registry = RefRegistry()
    raw = _load_raw("article")
    prev = _build(raw, registry)
    raw["root"]["c"] = (raw["root"].get("c") or []) + [dict(_DIALOG_NODE)]
    new, raws = _build_full(raw, registry)
    d = diff_pages(prev, new)
    assert d.kind == "dialog"
    # without raws: just the one-line [appeared] summary
    plain = render.render_diff("CLICK @e1 (button)", d)
    assert "[appeared]" in plain and "## " not in plain
    # with raws: the dialog is expanded inline (markdown header + @ref content)
    expanded = render.render_diff("CLICK @e1 (button)", d, raws, ObserveConfig())
    assert "[appeared]" in expanded
    assert "## " in expanded and "dialog" in expanded
    assert "(@e" in expanded  # refs from the expanded content, absent in the line


def test_oversized_appeared_dialog_renders_compact():
    registry = RefRegistry()
    raw = _load_raw("article")
    prev = _build(raw, registry)
    big = "lorem ipsum dolor sit amet consectetur " * 900  # ~36k chars of prose
    raw["root"]["c"] = (raw["root"].get("c") or []) + [
        {
            "t": "div",
            "a": {"role": "dialog", "nm": "Terms of Service"},
            "r": [100, 100, 800, 3000],
            "c": [
                {"t": "p", "r": [100, 120, 800, 2800], "x": big},
                {"t": "button", "r": [200, 2950, 100, 40], "x": "Accept", "k": {"tg": 1}},
                {"t": "button", "r": [340, 2950, 100, 40], "x": "Decline", "k": {"tg": 1}},
            ],
        }
    ]
    new, raws = _build_full(raw, registry)
    d = diff_pages(prev, new)
    assert d.kind == "dialog"
    out = render.render_diff("CLICK @e1 (button)", d, raws, ObserveConfig())
    # every control is kept (with @refs), prose is truncated, expand hint present
    assert "Accept" in out and "Decline" in out and "(@e" in out
    assert "text truncated" in out and "expand" in out
    assert out.count("lorem") < 900  # not the full prose


def test_coalesced_dialog_reported_as_dialog():
    # When a modal is absorbed into a content section (rather than split out),
    # the diff is a *changed* section whose added controls are dialog-scoped. The
    # renderer must detect that and report `→ dialog` + tag the line, since the
    # coalesced form is otherwise indistinguishable from an ordinary change.
    from ebrowse.model import Diff, SectionDiff

    registry = RefRegistry()
    raw = _load_raw("article")
    raw["root"]["c"] = (raw["root"].get("c") or []) + [dict(_DIALOG_NODE)]
    page, raws = _build_full(raw, registry)
    dsec = next(s for s in page.sections if s.type == "dialog")
    sd = SectionDiff(sid=dsec.sid, kind="changed", added=list(dsec.elements))
    d = Diff(kind="partial", sections=[sd])
    out = render.render_diff("CLICK @e1 (button)", d, raws, ObserveConfig())
    assert "→ dialog" in out  # outcome elevated from partial change
    assert f"{dsec.sid} [dialog]:" in out  # line tagged
    assert "Accept" in out and "(@e" in out
    # without raws the renderer can't detect it → plain partial change
    assert "→ partial change" in render.render_diff("CLICK @e1 (button)", d)


def test_render_outline_includes_visual_gist():
    registry = RefRegistry()
    page = _build(_load_raw("list"), registry)
    assert "◉" not in render.render_outline(page)  # none unless set
    page.screen_gist = "a product grid, no overlays"
    out = render.render_outline(page)
    lines = out.splitlines()
    assert lines[0].startswith("PAGE") and lines[1] == "◉ a product grid, no overlays"


def test_navigation_diff_marks_unchanged():
    registry = RefRegistry()
    raw = _load_raw("list")
    prev = _build(raw, registry)
    new = _build(raw, registry)
    d = navigation_diff(prev, new)
    assert d.kind == "navigation"
    assert set(d.unchanged_sids) == {s.sid for s in new.sections}
    assert navigation_diff(None, new).unchanged_sids == []


def test_added_text_word_level():
    old = "Create your account Full name Email address"
    new = "Create your account Full name Email address Account created! Check your email."
    assert added_text(old, new) == "Account created! Check your email."
    assert added_text(new, new) == ""
    # replacement counts as new text
    assert "closed" in added_text("Store is open today", "Store is closed today")


def test_added_text_replace_carries_context():
    # a replaced word alone ("30") is sub-noise-filter and meaningless; one
    # unchanged word of context per side makes it a quotable status line
    assert added_text("Showing 20 results.", "Showing 30 results.") == "Showing 30 results."


def test_added_text_lazy_load_keeps_status_update():
    # the issue #11 nested_scroll case: 10 new rows + a result-count tick;
    # the status must be quoted FIRST, the bulk capped after it
    rows = " ".join(f"Result item {i}" for i in range(1, 21))
    new_rows = " ".join(f"Result item {i}" for i in range(21, 31))
    old = f"{rows} Showing 20 results."
    new = f"{rows} {new_rows} Showing 30 results."
    out = added_text(old, new)
    assert out.startswith("Showing 30 results.")
    assert "Result item 21" in out
    assert len(out) <= 500


def test_added_text_status_ranked_before_bulk():
    # A short status fragment must win over a long bulk insertion (issue #11)
    old = "header middle footer"
    bulk = ("lorem ipsum dolor sit amet " * 30).strip()  # ~800 chars
    new = f"header {bulk} middle Showing 30 results. footer"
    out = added_text(old, new)
    assert out.startswith("Showing 30 results.")
    assert "lorem" in out  # the bulk is still represented (capped)
    assert len(out) <= 500


def test_added_text_short_fragments_keep_document_order():
    old = "a b c d e"
    new = "a Error: email is required b c Draft saved. d e"
    assert added_text(old, new) == "Error: email is required … Draft saved."


def test_added_text_per_fragment_cap_with_start_end_elision():
    # One long insertion: capped per-fragment, quoted as "start … end" so
    # summary info at either end of a bulk insertion survives
    words = [f"w{i:03d}" for i in range(200)]  # ~1000 chars
    old = "before after"
    new = "before " + " ".join(words) + " after"
    out = added_text(old, new)
    assert len(out) <= 500
    assert out.startswith("w000") and out.endswith("w199")
    assert " … " in out
    # with a large budget (expanded section) the same fragment fits whole
    assert added_text(old, new, max_len=8000) == " ".join(words)


def test_added_text_total_budget():
    # two separate long insertions: the joined quote respects the 500-char budget
    frags = [f"fragment number {i} " + "pad " * 60 for i in range(2)]  # each ~250 chars
    old = "x y"
    new = "x " + frags[0] + " y " + frags[1]
    out = added_text(old, new)
    assert "fragment number 0" in out and "fragment number 1" in out
    assert len(out) <= 500


def test_diff_pages_expanded_section_gets_larger_text_budget():
    registry = RefRegistry()
    raw = _load_raw("article")
    prev = _build(raw, registry)
    for n in _walk_dicts(raw["root"]):
        if len(n.get("x") or "") > 50:  # a body-text node (not a heading/label)
            n["x"] = n["x"] + " trailing marker appended"
            break
    new = _build(raw, registry)
    base = diff_pages(prev, new)
    assert base.kind == "partial"
    sid = base.sections[0].sid
    fp = next(s for s in new.sections if s.sid == sid).fingerprint
    bulk = " ".join(f"word{i:04d}" for i in range(800))  # ~7.2k chars of fresh text
    prev_texts = {sid: "unchanged lead-in"}
    new_texts = {sid: "unchanged lead-in " + bulk}
    plain = diff_pages(prev, new, prev_texts, new_texts)
    verbose = diff_pages(prev, new, prev_texts, new_texts, expanded_fps={fp})
    assert len(plain.sections[0].text_added) <= 500
    assert len(verbose.sections[0].text_added) > 2000
    assert verbose.sections[0].text_added == bulk  # fits the expanded budget whole
