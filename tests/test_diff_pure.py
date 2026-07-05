"""Pure diff-engine tests: mutate captured DomSnapshots, rebuild, diff."""

from __future__ import annotations

import json
from pathlib import Path

from ebrowse.config import ObserveConfig
from ebrowse.core.diff import added_text, diff_pages, navigation_diff
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import DomSnapshot

SNAPSHOT_DIR = Path(__file__).parent / "fixtures" / "domsnapshots"


def _load_raw(name: str) -> dict:
    return json.loads((SNAPSHOT_DIR / f"{name}.json").read_text())


def _walk_dicts(node: dict):
    yield node
    for c in node.get("c", []) or []:
        yield from _walk_dicts(c)


def _build(raw: dict, registry: RefRegistry):
    snap = DomSnapshot.from_dict(raw)
    return build_page(snap, registry, ObserveConfig(), captured_at=0.0)[0]


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
