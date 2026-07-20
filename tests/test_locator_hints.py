"""Pure tests for the anonymous-element locator hints (ADR 0015 follow-up):
ElementDesc.cls / .attrs population and the locator_class_tokens filter."""

from __future__ import annotations

from ebrowse.config import ObserveConfig
from ebrowse.core.fingerprint import RefRegistry, locator_class_tokens
from ebrowse.core.pipeline import build_page
from ebrowse.core.snapshot import DomSnapshot


def test_locator_class_tokens_filtering() -> None:
    # state classes dropped; hashy build tokens KEPT (session-stable, the
    # best discriminators — unlike fingerprint normalization); CSS-unsafe
    # tokens (need escaping) dropped; document order kept; capped at 6
    cls = (
        "is-active rt-Button PetSearchBar_searchBar__searchButton__HOBKK "
        "md:flex 2col selected iconbtn a b c d e"
    )
    assert locator_class_tokens(cls) == [
        "rt-Button",
        "PetSearchBar_searchBar__searchButton__HOBKK",
        "iconbtn",
        "a",
        "b",
        "c",
    ]
    assert locator_class_tokens("") == []


def _page(elements: list[dict]):
    snap = DomSnapshot.from_dict(
        {
            "url": "http://x.test/",
            "title": "t",
            "vw": 1280,
            "vh": 800,
            "scrollY": 0,
            "docH": 800,
            "truncated": False,
            "root": {
                "t": "body",
                "r": [0, 0, 1280, 800],
                "c": [{"t": "main", "r": [0, 0, 1280, 400], "c": elements}],
            },
        }
    )
    pm, _ = build_page(snap, RefRegistry(), ObserveConfig())
    return [e for s in pm.sections for e in s.elements]


def test_anonymous_element_gets_cls_and_attrs_hints() -> None:
    els = _page(
        [
            {
                "t": "button",
                "r": [10, 10, 40, 40],
                "a": {"cls": "iconbtn is-active", "xa": {"data-kind": "search"}},
                "k": {"tg": 1},
            }
        ]
    )
    assert len(els) == 1
    d = els[0].desc
    assert d.cls == "iconbtn"
    assert d.attrs == (("data-kind", "search"),)


def test_named_element_gets_cls_but_no_attrs() -> None:
    # attrs are hints of last resort: an element with a name never needs them
    els = _page(
        [
            {
                "t": "button",
                "r": [10, 10, 40, 40],
                "a": {"cls": "cta", "nm": "Submit", "xa": {"data-kind": "cta"}},
                "k": {"tg": 1},
            }
        ]
    )
    d = els[0].desc
    assert d.cls == "cta"
    assert d.attrs == ()


def test_hints_do_not_affect_ref_identity() -> None:
    # cls/attrs are EXCLUDED from match_key: hashy class tokens change across
    # deploys and must never churn ref reuse
    els = _page(
        [
            {"t": "button", "r": [10, 10, 40, 40], "a": {"cls": "one"}, "k": {"tg": 1}},
            {"t": "button", "r": [60, 10, 40, 40], "a": {"cls": "two"}, "k": {"tg": 1}},
        ]
    )
    a, b = els[0].desc, els[1].desc
    assert a.cls != b.cls
    assert a.match_key() == b.match_key()
    assert (a.nth_hint, b.nth_hint) == (0, 1)  # disambiguated positionally, as before
