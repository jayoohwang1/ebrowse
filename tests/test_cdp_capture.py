"""Pure tests for core/cdp_capture.py over recorded captureSnapshot payloads.

Fixtures in tests/fixtures/cdp/ are recorded from the fixture site (see ADR
0015); cross-engine outline parity is covered by the browser-marked test in
test_capture_parity.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebrowse.config import ObserveConfig
from ebrowse.core.cdp_capture import translate
from ebrowse.core.fingerprint import RefRegistry
from ebrowse.core.pipeline import build_page

CDP_DIR = Path(__file__).parent / "fixtures" / "cdp"
PAYLOADS = sorted(CDP_DIR.glob("*.json"))


def _load(name: str) -> dict:
    return json.loads((CDP_DIR / name).read_text())


@pytest.mark.parametrize("path", PAYLOADS, ids=lambda p: p.stem)
def test_translates_and_builds(path: Path) -> None:
    snap = translate(json.loads(path.read_text()), (1280, 800))
    assert snap.url.endswith(f"{path.stem}.html")
    pm, _ = build_page(snap, RefRegistry(), ObserveConfig())
    assert pm.sections
    els = [e for s in pm.sections for e in s.elements]
    assert els, "every fixture page has interactive elements"


def test_backend_node_ids_present() -> None:
    snap = translate(_load("form.json"), (1280, 800))
    ids = [n.backend_node_id for n in snap.root.walk()]
    assert all(isinstance(i, int) for i in ids)
    assert len(set(ids)) == len(ids), "backend node ids are unique"


def test_form_controls_and_names() -> None:
    snap = translate(_load("form.json"), (1280, 800))
    pm, _ = build_page(snap, RefRegistry(), ObserveConfig())
    descs = [e.desc.short_desc() for s in pm.sections for e in s.elements]
    assert 'input "Full name"' in descs  # label[for] resolution, Python-side
    assert 'email input "Email address"' in descs
    assert any(d.startswith("combobox") for d in descs)  # the country select


def test_iframe_stitched_with_frame_path() -> None:
    snap = translate(_load("iframe.json"), (1280, 800))
    framed = [n for n in snap.root.walk() if n.iframe_path]
    assert framed, "same-process iframe content is stitched from the payload"
    tags = {n.tag for n in framed}
    assert "input" in tags and "button" in tags
    # child-doc coords were offset to page space: inside the iframe's box
    iframe = next(n for n in snap.root.walk() if n.tag == "iframe")
    inner = next(n for n in framed if n.tag == "button")
    assert inner.rect[0] >= iframe.rect[0] and inner.rect[1] >= iframe.rect[1]


def test_labels_are_not_click_candidates() -> None:
    # Blink marks <label> isClickable (click forwarding); the translator must
    # not surface that as a weak `el` signal (parity + ADR 0009 label route)
    snap = translate(_load("form.json"), (1280, 800))
    labels = [n for n in snap.root.walk() if n.tag == "label"]
    assert labels
    assert not any(n.signals.get("el") for n in labels)
