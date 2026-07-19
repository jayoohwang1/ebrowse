"""DomSnapshot: the raw page data every pure-core function operates on.

`capture()` is the ONLY function in core/ that touches Playwright (per AGENTS.md
principle 3): one evaluate() per frame, results stitched into a single tree.
Everything downstream is pure and testable from JSON fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ebrowse import debug

_JS_PATH = Path(__file__).parent / "js" / "discover.js"
_DIAGNOSE_JS_PATH = Path(__file__).parent / "js" / "diagnose.js"
_js_cache: str | None = None
_diagnose_cache: str | None = None


def discover_js() -> str:
    global _js_cache
    if _js_cache is None:
        from ebrowse.core.clickable import render_js_template

        _js_cache = render_js_template(_JS_PATH.read_text())
    return _js_cache


async def probe_blocker(handle) -> dict[str, Any]:
    """Failure-only diagnostic: classify what is blocking a refused click.
    One evaluate against the target's element handle, in its own frame
    (js/diagnose.js). Raises whatever the evaluate raises — callers treat
    this as best-effort."""
    global _diagnose_cache
    if _diagnose_cache is None:
        _diagnose_cache = _DIAGNOSE_JS_PATH.read_text()
    return await handle.evaluate(_diagnose_cache)


_COVER_ABOVE_JS_PATH = Path(__file__).parent / "js" / "cover_above.js"
_cover_above_cache: str | None = None


async def probe_cover_above(frame_element_handle, cx: float, cy: float) -> dict[str, Any]:
    """Parent-document probe for iframe targets (js/cover_above.js): hit-test
    the target's viewport point in the frame element's OWN document, where a
    banner/modal above the iframe is visible. Raises on evaluate failure —
    callers treat this as best-effort."""
    global _cover_above_cache
    if _cover_above_cache is None:
        _cover_above_cache = _COVER_ABOVE_JS_PATH.read_text()
    return await frame_element_handle.evaluate(_cover_above_cache, [cx, cy])


@dataclass(slots=True)
class DomNode:
    tag: str
    rect: tuple[int, int, int, int]  # x, y, w, h — absolute page CSS px
    attrs: dict[str, Any] = field(default_factory=dict)
    text: str = ""  # own text (direct text children only)
    children: list[DomNode] = field(default_factory=list)
    signals: dict[str, int] = field(default_factory=dict)  # tg/rl/ls/cp
    iframe_path: tuple[str, ...] = ()
    cross_origin: bool = False  # iframe node we could not enter
    # runtime annotations (not serialized): set during element extraction
    ref: str | None = None
    # CDP binding (engine="cdp" captures only): consumed by the act-time fast
    # path (ADR 0015). Dies with the node — never persisted across captures.
    backend_node_id: int | None = None
    is_list_group: bool = False  # synthetic node wrapping grouped siblings
    candidate: str | None = None  # weak-evidence provenance (clickable.candidate_evidence)
    # A shallow projection used by the section splitter can own a wrapper's
    # direct text/signals without owning its descendants. Interactive identity
    # must still use the original live subtree text when resolving the wrapper.
    identity_text: str | None = None
    # Splitter memoization: one capture can contain 15k nodes, so repeated
    # subtree scans during recursive partitioning must remain O(n).
    split_cost: int | None = None
    has_collection_descendant: bool | None = None

    def bbox_area(self) -> int:
        return max(0, self.rect[2]) * max(0, self.rect[3])

    def subtree_text(self, cap: int = 100_000) -> str:
        """All visible text in document order, whitespace-collapsed."""
        parts: list[str] = []
        budget = cap

        def rec(n: DomNode) -> None:
            nonlocal budget
            if budget <= 0:
                return
            if n.text:
                parts.append(n.text[:budget])
                budget -= len(n.text)
            for c in n.children:
                rec(c)

        rec(self)
        return " ".join(parts)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"t": self.tag, "r": list(self.rect)}
        if self.attrs:
            d["a"] = self.attrs
        if self.text:
            d["x"] = self.text
        if self.signals:
            d["k"] = self.signals
        if self.iframe_path:
            d["fp"] = list(self.iframe_path)
        if self.cross_origin:
            d["xo"] = 1
        if self.children:
            d["c"] = [c.to_dict() for c in self.children]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], iframe_path: tuple[str, ...] = ()) -> DomNode:
        path = tuple(d.get("fp") or iframe_path)
        return cls(
            tag=d.get("t", "div"),
            rect=tuple(d.get("r", [0, 0, 0, 0])),  # type: ignore[arg-type]
            attrs=d.get("a", {}) or {},
            text=d.get("x", "") or "",
            children=[cls.from_dict(c, path) for c in d.get("c", []) or []],
            signals=d.get("k", {}) or {},
            iframe_path=path,
            cross_origin=bool(d.get("xo")),
        )


@dataclass(slots=True)
class DomSnapshot:
    url: str
    title: str
    viewport: tuple[int, int]
    scroll_y: int
    doc_height: int
    truncated: bool
    root: DomNode

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "vw": self.viewport[0],
            "vh": self.viewport[1],
            "scrollY": self.scroll_y,
            "docH": self.doc_height,
            "truncated": self.truncated,
            "root": self.root.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DomSnapshot:
        return cls(
            url=d["url"],
            title=d.get("title", ""),
            viewport=(d.get("vw", 1280), d.get("vh", 1280)),
            scroll_y=d.get("scrollY", 0),
            doc_height=d.get("docH", 0),
            truncated=bool(d.get("truncated")),
            root=DomNode.from_dict(d["root"]),
        )


_MIN_FRAME_W, _MIN_FRAME_H = 100, 60


async def _discover_main_frame(page) -> dict[str, Any]:
    """Run discover.js in the main frame, preferring one CDP Runtime.evaluate
    with includeCommandLineAPI so the walk can use the devtools
    getEventListeners() API (weak `el` candidate signal) — still a single
    round trip, per architecture principle 3. Chromium-only; any failure
    degrades to a plain evaluate with no listener signals."""
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            r = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": f"({discover_js()})()",
                    "includeCommandLineAPI": True,
                    "returnByValue": True,
                },
            )
            value = r.get("result", {}).get("value")
            if isinstance(value, dict) and value.get("root"):
                return value
        finally:
            await cdp.detach()
    except Exception:
        pass
    return await page.evaluate(discover_js())


async def capture(page, engine: str = "cdp") -> DomSnapshot:
    """Snapshot the page. engine="cdp" (default, ADR 0015): one
    DOMSnapshot.captureSnapshot — no JS runs in the page, DomNodes carry
    backend_node_id bindings, same-process iframes come with the payload.
    engine="js" (temporary escape hatch) or any CDP failure: the discover.js
    evaluate walk. Either way, frames the CDP payload did not cover (OOPIFs
    on the cdp path; all child frames on the js path) are stitched by
    per-frame evaluate; frames we cannot evaluate in become cross_origin
    leaf nodes.
    """
    snap: DomSnapshot | None = None
    if engine == "cdp":
        snap = await _capture_cdp_main(page)
        if snap is None:
            debug.emit("capture", "cdp_engine_failed", level="warn")
    if snap is None:
        raw = await _discover_main_frame(page)
        snap = DomSnapshot.from_dict(raw)
    await _stitch_child_frames(page, snap)
    return snap


async def _capture_cdp_main(page) -> DomSnapshot | None:
    """DOMSnapshot capture of the main target (same-process frames included);
    None on any CDP failure so capture() can degrade to the js engine."""
    from ebrowse.core.cdp_capture import COMPUTED_STYLES, translate

    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            metrics = await cdp.send("Page.getLayoutMetrics")
            payload = await cdp.send(
                "DOMSnapshot.captureSnapshot",
                {"computedStyles": COMPUTED_STYLES, "includeDOMRects": True},
            )
        finally:
            await cdp.detach()
        css = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
        viewport = (int(css.get("clientWidth", 1280)), int(css.get("clientHeight", 1280)))
        return translate(payload, viewport)
    except Exception:
        return None


async def _stitch_child_frames(page, snap: DomSnapshot) -> None:
    # index iframe nodes in the main tree by id/title/src for stitching
    iframe_nodes = [n for n in snap.root.walk() if n.tag == "iframe"]

    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            handle = await frame.frame_element()
            box = await handle.bounding_box()
        except Exception:
            continue
        if not box or box["width"] < _MIN_FRAME_W or box["height"] < _MIN_FRAME_H:
            continue
        # fid must be resolvable later by locate._frame_scope (iframe[id|title|src=...]),
        # so prefer the verbatim src attribute over frame.url for id-less frames
        fid = (
            await handle.get_attribute("id")
            or await handle.get_attribute("title")
            or await handle.get_attribute("src")
            or frame.url
        )
        node = _match_iframe_node(iframe_nodes, fid, frame.url)
        if node is None:
            continue
        try:
            # plain evaluate: child frames get no `el` listener signal (the
            # CDP command-line API binds to the main frame's session only)
            child_raw = await frame.evaluate(discover_js())
        except Exception:
            node.cross_origin = True
            continue
        child = DomNode.from_dict(child_raw["root"])
        path = (*node.iframe_path, fid)
        _offset_and_tag(child, int(box["x"]) - child.rect[0], int(box["y"]) - child.rect[1], path)
        node.children = [child]


def _match_iframe_node(nodes: list[DomNode], fid: str, url: str) -> DomNode | None:
    for n in nodes:
        if n.children:  # already stitched
            continue
        if n.attrs.get("id") == fid or n.attrs.get("ttl") == fid:
            return n
        src = n.attrs.get("src") or ""
        if src and (src == fid or url.endswith(src) or src in url):
            return n
    return None


def _offset_and_tag(node: DomNode, dx: int, dy: int, path: tuple[str, ...]) -> None:
    node.rect = (node.rect[0] + dx, node.rect[1] + dy, node.rect[2], node.rect[3])
    node.iframe_path = path
    for c in node.children:
        _offset_and_tag(c, dx, dy, path)
