"""Pure tests for the derived accessibility-tree section renderer."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ebrowse.config import ObserveConfig
from ebrowse.core.ax import IMPLICIT_ROLES, implicit_role, render_section_ax
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import DomNode, DomSnapshot
from ebrowse.core.split import RawSection
from ebrowse.model import BBox, Section

SNAPSHOTS = Path(__file__).parent / "fixtures" / "domsnapshots"
GOLDENS = Path(__file__).parent / "golden"


def build(name: str):
    snapshot = DomSnapshot.from_dict(json.loads((SNAPSHOTS / f"{name}.json").read_text()))
    return build_page(snapshot, RefRegistry(), ObserveConfig(), captured_at=0.0)


def check_golden(name: str, actual: str) -> None:
    path = GOLDENS / f"{name}.txt"
    if os.environ.get("EBROWSE_UPDATE_GOLDENS"):
        path.write_text(actual + "\n")
        return
    assert actual + "\n" == path.read_text()


@pytest.mark.parametrize(
    ("fixture", "sid", "cursor"),
    [
        ("form", "s2", 0),
        ("list", "s4", 0),
        ("list", "s4", 20),
        ("table", "s4", 0),
        ("ax_states", "s1", 0),
    ],
)
def test_ax_goldens(fixture: str, sid: str, cursor: int) -> None:
    page, raws = build(fixture)
    section = page.section(sid)
    assert section is not None
    actual = render_section_ax(section, raws[sid], ObserveConfig(), cursor=cursor)
    check_golden(f"ax_{fixture}_{sid}_{cursor}", actual)


@pytest.mark.parametrize(
    ("tag", "attrs", "role"),
    [
        ("a", {"href": "/x"}, "link"),
        ("button", {}, "button"),
        ("input", {}, "textbox"),
        ("input", {"typ": "search"}, "searchbox"),
        ("input", {"typ": "checkbox"}, "checkbox"),
        ("input", {"typ": "radio"}, "radio"),
        ("input", {"typ": "range"}, "slider"),
        ("input", {"typ": "number"}, "spinbutton"),
        ("textarea", {}, "textbox"),
        ("select", {}, "combobox"),
        ("select", {"mul": 1}, "listbox"),
        ("option", {}, "option"),
        ("h3", {}, "heading"),
        ("ul", {}, "list"),
        ("li", {}, "listitem"),
        ("table", {}, "table"),
        ("tr", {}, "row"),
        ("td", {}, "cell"),
        ("th", {}, "columnheader"),
        ("img", {}, "img"),
        ("nav", {}, "navigation"),
        ("main", {}, "main"),
        ("header", {}, "banner"),
        ("footer", {}, "contentinfo"),
        ("aside", {}, "complementary"),
        ("form", {}, "form"),
        ("article", {}, "article"),
        ("section", {"nm": "Named"}, "region"),
        ("fieldset", {}, "group"),
        ("details", {}, "group"),
        ("summary", {}, "button"),
        ("dialog", {}, "dialog"),
        ("hr", {}, "separator"),
        ("progress", {}, "progressbar"),
        ("figure", {}, "figure"),
        ("p", {}, "paragraph"),
        ("blockquote", {}, "blockquote"),
    ],
)
def test_implicit_role_mapping(tag: str, attrs: dict[str, object], role: str) -> None:
    assert implicit_role(DomNode(tag=tag, rect=(0, 0, 1, 1), attrs=attrs)) == role


def test_role_mapping_table_is_data_driven() -> None:
    assert IMPLICIT_ROLES["input:checkbox"] == "checkbox"
    assert IMPLICIT_ROLES["table"] == "table"


def test_generic_wrappers_promote_children_and_keep_own_text() -> None:
    raw = RawSection(
        node=DomNode(
            tag="div",
            rect=(0, 0, 100, 40),
            text="Before",
            children=[
                DomNode(
                    tag="div",
                    rect=(0, 0, 50, 20),
                    children=[DomNode(tag="button", rect=(0, 0, 50, 20), text="Go", ref="@e1")],
                )
            ],
        ),
        parent_tags=(),
    )
    section = Section("s1", "x", "content", None, "", [], "x", 1, BBox(0, 0, 100, 40))
    assert (
        render_section_ax(section, raw, ObserveConfig())
        == '## s1 content (ax)\n- text: "Before"\n- button "Go" (@e1)'
    )
