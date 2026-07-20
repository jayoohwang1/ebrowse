"""DOMSnapshot.captureSnapshot payload -> DomSnapshot (pure; ADR 0015).

Translates the CDP flat-array snapshot into the exact DomNode tree shape
discover.js produces, so everything downstream (split/extract/render/golden
tests) is untouched. Runs no JavaScript in the page: logic that discover.js
computed in-page (accessible names, label[for] maps, fieldset disabling,
clickable signals) is recomputed here over the flat arrays, which include
hidden nodes.

Each DomNode additionally carries `backend_node_id` — the CDP node binding
consumed by the act-time fast path.

Payload shape (see DOMSnapshot domain): a string table plus one document entry
per same-process frame; per-document parallel arrays for nodes (parentIndex,
nodeName, attributes, backendNodeId, form state, isClickable, ...) and for
laid-out nodes (nodeIndex, bounds, styles for the requested whitelist,
scroll/client rects). Nodes without a layout entry are not rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ebrowse.core.clickable import (
    CANDIDATE_ARIA_ATTRS,
    CLICKABLE_ROLES,
    CLICKABLE_TAGS,
    LISTENER_ATTRS,
    SKIP_TAGS,
)
from ebrowse.core.snapshot import DomNode, DomSnapshot

# Order must match the computedStyles list requested in snapshot.capture()
COMPUTED_STYLES = ["display", "visibility", "cursor", "overflow-y", "opacity", "pointer-events"]
_STYLE_IDX = {name: i for i, name in enumerate(COMPUTED_STYLES)}

MAX_NODES = 15000
TEXT_CAP = 4000

_CLICKABLE_TAGS = set(CLICKABLE_TAGS)
_CLICKABLE_ROLES = set(CLICKABLE_ROLES)
_SKIP_TAGS = set(SKIP_TAGS)

_ELEMENT_NODE = 1
_TEXT_NODE = 3


def _rare_bool(data: dict[str, Any]) -> set[int]:
    return set(data.get("index", []))


def _rare_map(data: dict[str, Any]) -> dict[int, int]:
    return dict(zip(data.get("index", []), data.get("value", []), strict=False))


@dataclass(slots=True)
class _Doc:
    """One document's arrays, decoded just enough to walk."""

    url: str
    title: str
    scroll_x: int
    scroll_y: int
    content_height: int
    parent: list[int]
    node_type: list[int]
    tag: list[str]  # lowercased nodeName
    node_value: list[str]
    backend_id: list[int]
    attrs: list[dict[str, str]]
    checked: set[int]
    selected: set[int]
    clickable: set[int]
    input_value: dict[int, int]  # node idx -> string idx
    text_value: dict[int, int]
    content_doc: dict[int, int]  # node idx -> document idx
    shadow_type: dict[int, int]  # node idx -> string idx of shadowRootType
    current_src: dict[int, int]
    children: list[list[int]]
    layout_of: dict[int, int]  # node idx -> layout row
    bounds: list[list[float]]
    styles: list[list[int]]
    scroll_rects: list[list[float] | None]
    client_rects: list[list[float] | None]


def _decode_doc(doc: dict[str, Any], strings: list[str]) -> _Doc:
    nodes = doc["nodes"]
    layout = doc["layout"]
    n = len(nodes["nodeName"])

    def s(i: int) -> str:
        return strings[i] if 0 <= i < len(strings) else ""

    attrs: list[dict[str, str]] = []
    for flat in nodes["attributes"]:
        a = {}
        for j in range(0, len(flat), 2):
            a[s(flat[j]).lower()] = s(flat[j + 1])
        attrs.append(a)

    parent = nodes["parentIndex"]
    children: list[list[int]] = [[] for _ in range(n)]
    for i, p in enumerate(parent):
        if 0 <= p < n:
            children[p].append(i)

    return _Doc(
        url=s(doc.get("documentURL", -1)),
        title=s(doc.get("title", -1)),
        scroll_x=round(doc.get("scrollOffsetX", 0)),
        scroll_y=round(doc.get("scrollOffsetY", 0)),
        content_height=round(doc.get("contentHeight", 0)),
        parent=parent,
        node_type=nodes["nodeType"],
        tag=[s(i).lower() for i in nodes["nodeName"]],
        node_value=[s(i) for i in nodes["nodeValue"]],
        backend_id=nodes["backendNodeId"],
        attrs=attrs,
        checked=_rare_bool(nodes.get("inputChecked", {})),
        selected=_rare_bool(nodes.get("optionSelected", {})),
        clickable=_rare_bool(nodes.get("isClickable", {})),
        input_value=_rare_map(nodes.get("inputValue", {})),
        text_value=_rare_map(nodes.get("textValue", {})),
        content_doc=_rare_map(nodes.get("contentDocumentIndex", {})),
        shadow_type=_rare_map(nodes.get("shadowRootType", {})),
        current_src=_rare_map(nodes.get("currentSourceURL", {})),
        children=children,
        layout_of=dict((ni, li) for li, ni in enumerate(layout["nodeIndex"])),
        bounds=layout["bounds"],
        styles=layout["styles"],
        scroll_rects=layout.get("scrollRects") or [],
        client_rects=layout.get("clientRects") or [],
    )


def _collapse(s: str) -> str:
    return " ".join(s.split())


# attrs never useful as identity: curated elsewhere, style/state, or handlers
_XA_EXCLUDE = frozenset(
    [
        "id",
        "class",
        "style",
        "role",
        "href",
        "placeholder",
        "title",
        "alt",
        "src",
        "type",
        "name",
        "value",
        "tabindex",
        "contenteditable",
        "draggable",
        "disabled",
        "required",
        "multiple",
        "checked",
        "selected",
        "for",
        "data-testid",
        "data-qa",
        "data-test",
    ]
)


def _extra_attrs(el: dict[str, str]) -> dict[str, str] | None:
    """Filtered custom attributes for an otherwise-anonymous element: no
    curated/standard keys, no on* handlers, no aria-* (state, not identity),
    values capped. At most 4 pairs, sorted for determinism."""
    xa = {}
    for k, v in el.items():
        if k in _XA_EXCLUDE or k.startswith(("on", "aria-")):
            continue
        xa[k] = v[:60]
    if not xa:
        return None
    return dict(sorted(xa.items())[:4])


class _DocWalker:
    """Walks one decoded document into DomNodes (discover.js semantics)."""

    def __init__(self, doc: _Doc, strings: list[str], budget: _Budget,
                 origin: tuple[int, int], iframe_path: tuple[str, ...],
                 stitch: _Stitcher) -> None:  # fmt: skip
        self.d = doc
        self.strings = strings
        self.budget = budget
        self.ox, self.oy = origin  # page offset of this doc's origin
        self.iframe_path = iframe_path
        self.stitch = stitch
        # id -> node idx over ALL nodes (hidden included), for labelledby/label[for]
        self.by_id: dict[str, int] = {}
        self.label_for: dict[str, str] = {}
        # subtree that actually renders — keeps display:contents wrappers,
        # prunes display:none subtrees (which have no layout rows at all)
        self.renders: list[bool] = [False] * len(doc.tag)
        self._index()

    def _index(self) -> None:
        d = self.d
        for i, t in enumerate(d.tag):
            if d.node_type[i] != _ELEMENT_NODE:
                continue
            a = d.attrs[i]
            el_id = a.get("id")
            if el_id and el_id not in self.by_id:
                self.by_id[el_id] = i
            if t == "label" and a.get("for"):
                txt = _collapse(self._text_content(i))
                if txt:
                    self.label_for.setdefault(a["for"], txt[:120])
        # bottom-up: a node renders if it has a layout row or any child does
        for i in range(len(d.tag) - 1, -1, -1):
            r = i in d.layout_of or self.renders[i]
            if r:
                self.renders[i] = True
                p = d.parent[i]
                if p >= 0:
                    self.renders[p] = True

    def _text_content(self, idx: int, cap: int = 2000) -> str:
        """All descendant text (hidden included), like DOM textContent."""
        d, parts, budget = self.d, [], cap
        stack = [idx]
        while stack and budget > 0:
            i = stack.pop()
            if d.node_type[i] == _TEXT_NODE:
                v = d.node_value[i]
                parts.append(v[:budget])
                budget -= len(v)
            stack.extend(reversed(d.children[i]))
        return "".join(parts)

    def _style(self, idx: int, name: str) -> str:
        li = self.d.layout_of.get(idx)
        if li is None:
            return ""
        row = self.d.styles[li]
        si = _STYLE_IDX[name]
        if si >= len(row):
            return ""
        v = row[si]
        return self.strings[v] if 0 <= v < len(self.strings) else ""

    # -- accessible name (mirrors discover.js accName) ---------------------

    def _acc_name(self, idx: int, tag: str) -> str | None:
        a = self.d.attrs[idx]
        aria = a.get("aria-label")
        if aria:
            return _collapse(aria)[:120] or None
        lb = a.get("aria-labelledby")
        if lb:
            parts = []
            for ref in lb.split():
                ri = self.by_id.get(ref)
                if ri is not None:
                    parts.append(_collapse(self._text_content(ri)))
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                return joined[:120]
        el_id = a.get("id")
        if el_id and el_id in self.label_for:
            return self.label_for[el_id]
        if tag in ("input", "select", "textarea"):
            j = self.d.parent[idx]
            while j >= 0:
                if self.d.tag[j] == "label":
                    t = _collapse(self._text_content(j))
                    if t:
                        return t[:120]
                j = self.d.parent[j]
        title = a.get("title")
        if title:
            return _collapse(title)[:120] or None
        alt = a.get("alt")
        if alt:
            return _collapse(alt)[:120] or None
        return None

    # -- curated attrs (mirrors discover.js curatedAttrs) ------------------

    def _curated(self, idx: int, tag: str, fieldset_disabled: bool) -> dict[str, Any]:
        d = self.d
        el = d.attrs[idx]
        a: dict[str, Any] = {}
        if el.get("id"):
            a["id"] = el["id"]
        if el.get("class"):
            a["cls"] = _collapse(el["class"])[:200]
        if el.get("role"):
            a["role"] = el["role"]
        nm = self._acc_name(idx, tag)
        if nm:
            a["nm"] = nm
        tid = el.get("data-testid") or el.get("data-qa") or el.get("data-test")
        if tid:
            a["tid"] = tid
        if el.get("href"):
            a["href"] = el["href"][:500]
        if el.get("placeholder"):
            a["ph"] = el["placeholder"]
        if el.get("title"):
            a["ttl"] = _collapse(el["title"])[:120]
        if "contenteditable" in el and el.get("contenteditable") != "false":
            a["con"] = 1
        if tag == "label" and "for" in el:
            a["for"] = 1
        exp = el.get("aria-expanded")
        if exp is not None:
            a["exp"] = 1 if exp == "true" else 0
        pop = el.get("aria-haspopup")
        if pop and pop != "false":
            a["pop"] = pop
        prs = el.get("aria-pressed")
        if prs is not None:
            a["prs"] = 1 if prs == "true" else 0
        asel = el.get("aria-selected")
        if asel is not None:
            a["asel"] = 1 if asel == "true" else 0
        role = el.get("role")
        if role in ("checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"):
            ac = el.get("aria-checked")
            if ac is not None:
                a["chk"] = 1 if ac == "true" else 0
        # fieldset inheritance replaces the in-page :disabled match; the
        # first-<legend> exemption is deliberately ignored (vanishingly rare)
        dis = el.get("aria-disabled") == "true" or "disabled" in el or fieldset_disabled
        if dis:
            a["dis"] = 1

        if tag in ("input", "textarea"):
            typ = "textarea" if tag == "textarea" else (el.get("type") or "text").lower()
            a["typ"] = typ
            if typ in ("checkbox", "radio"):
                a["chk"] = 1 if idx in d.checked else 0
            elif typ == "password":
                # CDP exposes the real value — mask, never emit the secret
                if d.input_value.get(idx) is not None and self._string(d.input_value[idx]):
                    a["val"] = "•••"
            else:
                si = d.text_value.get(idx) if tag == "textarea" else d.input_value.get(idx)
                val = self._string(si) if si is not None else ""
                if val:
                    a["val"] = val[:200]
            if "required" in el:
                a["req"] = 1
        elif tag == "select":
            opts, chosen, total = [], [], 0
            for ci in d.children[idx]:
                if d.tag[ci] != "option":
                    continue
                total += 1
                txt = _collapse(self._text_content(ci))[:80]
                if len(opts) < 350:
                    opts.append(txt)
                if ci in d.selected and len(chosen) < 5:
                    chosen.append(txt)
            a["opt"] = opts
            if total > len(opts):
                a["optn"] = total
            if "multiple" in el:
                a["mul"] = 1
            if chosen:
                a["sel"] = ", ".join(chosen)
        elif tag == "img":
            if el.get("alt"):
                a["alt"] = _collapse(el["alt"])[:160]
            src_i = d.current_src.get(idx)
            src = self._string(src_i) if src_i is not None else el.get("src", "")
            if src:
                a["src"] = src[:300]
        elif tag == "iframe":
            if el.get("src"):
                a["src"] = el["src"][:300]
        return a

    def _string(self, i: int) -> str:
        return self.strings[i] if 0 <= i < len(self.strings) else ""

    # -- the walk ----------------------------------------------------------

    def walk(self, idx: int, parent_cursor_pointer: bool = False,
             fieldset_disabled: bool = False, in_inert: bool = False) -> DomNode | None:  # fmt: skip
        d = self.d
        tag = d.tag[idx]
        if tag in _SKIP_TAGS or not self.renders[idx]:
            return None
        if self._style(idx, "visibility") == "hidden":
            return None
        if not self.budget.take():
            return None

        li = d.layout_of.get(idx)
        if li is not None:
            b = d.bounds[li]
            r = (round(b[0]) + self.ox, round(b[1]) + self.oy, round(b[2]), round(b[3]))
        else:
            r = (0, 0, 0, 0)  # display:contents wrapper — renders via children

        node = DomNode(tag=tag, rect=r, iframe_path=self.iframe_path)
        node.backend_node_id = d.backend_id[idx]
        node.attrs = {}
        el = d.attrs[idx]
        if "inert" in el:
            in_inert = True
        if tag == "fieldset" and ("disabled" in el or el.get("aria-disabled") == "true"):
            fieldset_disabled = True
        a = self._curated(idx, tag, fieldset_disabled)

        # inner scroll container (body/html scroll via the window instead)
        if tag not in ("body", "html") and li is not None:
            oy_style = self._style(idx, "overflow-y")
            sr = self.scroll_rect(li)
            cr = self.client_rect(li)
            if oy_style in ("auto", "scroll") and sr and cr and sr[3] > cr[3] + 4:
                # scrollRects x/y are the current scroll offsets (Blink fills
                # them from scrollOffsetX/Y of the box's scrollable area)
                a["scr"] = [round(sr[1]), round(sr[3] - cr[3])]
        if a:
            node.attrs = a

        # clickable signals — strong tier (tg/rl/ls/cp)
        cursor_pointer = self._style(idx, "cursor") == "pointer"
        k: dict[str, int] = {}
        if tag in _CLICKABLE_TAGS:
            k["tg"] = 1
        role = el.get("role")
        if role in _CLICKABLE_ROLES:
            k["rl"] = 1
        if any(la in el for la in LISTENER_ATTRS):
            k["ls"] = 1
        if cursor_pointer and not parent_cursor_pointer:
            k["cp"] = 1
        if a.get("con"):
            k["tg"] = 1
        # weak candidate tier (tb/as/dg/el) — only when no strong signal.
        # `el` uses Blink's isClickable (set for nodes with click handlers),
        # replacing the getEventListeners sweep of the JS engine (ADR 0015).
        if not k:
            try:
                if int(el.get("tabindex", "")) >= 0:
                    k["tb"] = 1
            except ValueError:
                pass
            if any(aa in el for aa in CANDIDATE_ARIA_ATTRS):
                k["as"] = 1
            if el.get("draggable") == "true" and tag not in ("img", "a"):
                k["dg"] = 1
            # Blink marks <label> clickable (click forwarding) where the JS
            # engine's getEventListeners did not; labels have their own
            # activation route (ADR 0009) and must not become candidates
            if idx in d.clickable and tag != "label":
                k["el"] = 1
        if k:
            node.signals = k
            if in_inert:
                node.attrs = node.attrs or {}
                node.attrs["inr"] = 1
            # custom-attr locator hints for anonymous interactive elements:
            # pages that name nothing usually still hang framework attributes
            # on functional elements (data-action, data-cy, ...) — recorded
            # only when no standard identity exists, consumed by locate's
            # unique-match fallback candidates (ADR 0015 follow-up)
            if not (a.get("id") or a.get("tid") or a.get("nm") or a.get("ph") or a.get("href")):
                xa = _extra_attrs(el)
                if xa:
                    a["xa"] = xa
                    node.attrs = a

        # own text: direct text-node children only
        own = "".join(d.node_value[ci] for ci in d.children[idx] if d.node_type[ci] == _TEXT_NODE)
        own = _collapse(own)
        if own:
            node.text = own[:TEXT_CAP]

        # svg / select are leaves; iframe stitches its content document
        if tag == "svg" or tag == "select":
            return node
        if tag == "iframe":
            self.stitch.iframe(node, idx, self)
            return node

        kids: list[DomNode] = []
        for ci in d.children[idx]:
            if d.node_type[ci] != _ELEMENT_NODE:
                # shadow roots appear as document-fragment children with a
                # shadowRootType; inline author roots, skip UA internals
                st = d.shadow_type.get(ci)
                if st is not None and self._string(st) != "user-agent":
                    for si in d.children[ci]:
                        if d.node_type[si] == _ELEMENT_NODE:
                            cn = self.walk(si, cursor_pointer, fieldset_disabled, in_inert)
                            if cn:
                                kids.append(cn)
                continue
            cn = self.walk(ci, cursor_pointer, fieldset_disabled, in_inert)
            if cn:
                kids.append(cn)
        node.children = kids
        return node

    def scroll_rect(self, li: int) -> list[float] | None:
        sr = self.d.scroll_rects
        return sr[li] if li < len(sr) and sr[li] else None

    def client_rect(self, li: int) -> list[float] | None:
        cr = self.d.client_rects
        return cr[li] if li < len(cr) and cr[li] else None

    def body_index(self) -> int | None:
        for i, t in enumerate(self.d.tag):
            if t == "body" and self.d.node_type[i] == _ELEMENT_NODE:
                return i
        return None


class _Budget:
    def __init__(self, cap: int = MAX_NODES) -> None:
        self.left = cap
        self.truncated = False

    def take(self) -> bool:
        if self.left <= 0:
            self.truncated = True
            return False
        self.left -= 1
        return True


class _Stitcher:
    """Links iframe DomNodes to their same-process content documents."""

    def __init__(self, docs: list[_Doc], strings: list[str], budget: _Budget) -> None:
        self.docs = docs
        self.strings = strings
        self.budget = budget

    def iframe(self, node: DomNode, idx: int, parent: _DocWalker) -> None:
        di = parent.d.content_doc.get(idx)
        if di is None or not (0 <= di < len(self.docs)):
            return
        el = parent.d.attrs[idx]
        fid = el.get("id") or el.get("title") or el.get("src") or self.docs[di].url
        child_doc = self.docs[di]
        # child-doc coords are relative to its own origin; place at the iframe box
        origin = (node.rect[0], node.rect[1])
        w = _DocWalker(child_doc, self.strings, self.budget, origin,
                       (*node.iframe_path, fid), self)  # fmt: skip
        bi = w.body_index()
        if bi is None:
            return
        child = w.walk(bi)
        if child:
            node.children = [child]


def translate(payload: dict[str, Any], viewport: tuple[int, int]) -> DomSnapshot:
    """CDP captureSnapshot payload -> DomSnapshot. Pure."""
    strings: list[str] = payload["strings"]
    docs = [_decode_doc(d, strings) for d in payload["documents"]]
    main = docs[0]
    budget = _Budget()
    stitch = _Stitcher(docs, strings, budget)
    walker = _DocWalker(main, strings, budget, (0, 0), (), stitch)
    bi = walker.body_index()
    root = walker.walk(bi) if bi is not None else None
    if root is None:
        root = DomNode(tag="body", rect=(0, 0, 0, 0))
    return DomSnapshot(
        url=main.url,
        title=main.title,
        viewport=viewport,
        scroll_y=main.scroll_y,
        doc_height=main.content_height,
        truncated=budget.truncated,
        root=root,
    )
