"""Core data model. FROZEN interface per docs/output-contracts.md.

Modules communicate exclusively through these types plus the renderers.
Extend by adding optional fields; never repurpose existing ones.
All types are JSON-serializable via to_dict()/from_dict().
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SectionType = Literal[
    "nav",
    "header",
    "footer",
    "form",
    "list",
    "table",
    "dialog",
    "content",
    "media",
    "iframe",
]

DiffKind = Literal["no_change", "partial", "navigation", "dialog"]
SectionDiffKind = Literal["appeared", "disappeared", "changed"]


@dataclass(slots=True)
class BBox:
    """Absolute page coordinates in CSS pixels (scroll-independent)."""

    x: float
    y: float
    width: float
    height: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BBox:
        return cls(x=d["x"], y=d["y"], width=d["width"], height=d["height"])

    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)


@dataclass(slots=True)
class ElementDesc:
    """Durable identity of an interactive element.

    Basis for refs (session registry) and locator construction.
    Fields must be derivable from the DOM alone and reasonably stable
    across page loads. Volatile data belongs in ElementState.
    """

    tag: str
    role: str | None = None  # explicit or implicit ARIA role
    id: str | None = None
    testid: str | None = None  # data-testid / data-qa / data-test
    name: str | None = None  # accessible name (aria-label > label > title > alt)
    placeholder: str | None = None
    href: str | None = None  # normalized: path+query, origin stripped
    input_type: str | None = None
    text_head: str = ""  # first 80 chars visible text, whitespace-collapsed
    nth_hint: int = 0  # disambiguator among identical descriptors on a page
    iframe_path: tuple[str, ...] = ()  # ancestor frame ids/titles; () = main frame

    def match_key(self) -> tuple:
        """Exact-match identity used by RefRegistry (excludes nth_hint)."""
        return (
            self.tag,
            self.role,
            self.id,
            self.testid,
            self.name,
            self.placeholder,
            self.href,
            self.input_type,
            self.text_head,
            self.iframe_path,
        )

    def short_desc(self) -> str:
        """Human/agent-readable one-phrase description, e.g. 'button "Add to Cart"'."""
        kind = self.role or self.tag
        if self.tag == "input" and self.input_type:
            kind = f"{self.input_type} input" if self.input_type != "text" else "input"
        label = self.name or self.placeholder or self.text_head or self.id or ""
        label = label.strip()
        if len(label) > 40:
            label = label[:37] + "..."
        return f'{kind} "{label}"' if label else kind

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["iframe_path"] = list(self.iframe_path)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ElementDesc:
        d = dict(d)
        d["iframe_path"] = tuple(d.get("iframe_path") or ())
        return cls(**d)


@dataclass(slots=True)
class ElementState:
    """Volatile per-observation state of an element."""

    bbox: BBox
    visible: bool = True
    value: str | None = None
    checked: bool | None = None
    disabled: bool = False
    expanded: bool | None = None  # aria-expanded
    options: list[str] | None = None  # native <select> only

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bbox"] = self.bbox.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ElementState:
        d = dict(d)
        d["bbox"] = BBox.from_dict(d["bbox"])
        return cls(**d)


@dataclass(slots=True)
class Element:
    ref: str  # "@e12" — session-scoped, durable
    desc: ElementDesc
    state: ElementState

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "desc": self.desc.to_dict(), "state": self.state.to_dict()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Element:
        return cls(
            ref=d["ref"],
            desc=ElementDesc.from_dict(d["desc"]),
            state=ElementState.from_dict(d["state"]),
        )


@dataclass(slots=True)
class Section:
    sid: str  # "s1".. document order on current page
    fingerprint: str  # stable identity across mutations/revisits
    type: SectionType
    heading: str | None  # nearest heading/landmark text (deterministic)
    preview: str  # first N chars of collapsed text content
    elements: list[Element]
    content_hash: str  # summary cache key
    token_estimate: int  # len(rendered_markdown) // 4
    bbox: BBox
    summary: str | None = None  # LLM one-liner; provenance: model-generated
    item_count: int | None = None  # list/table sections
    iframe_path: tuple[str, ...] = ()
    cross_origin: bool = False  # iframe sections we cannot enter

    def counts_desc(self) -> str:
        """Deterministic element-count phrase for outline lines."""
        counts: dict[str, int] = {}
        for el in self.elements:
            k = _count_bucket(el.desc)
            counts[k] = counts.get(k, 0) + 1

        def plural(n: int, word: str) -> str:
            return f"{n} {word}" if n != 1 else f"1 {word.rstrip('s')}"

        parts = []
        if self.item_count is not None:
            parts.append(plural(self.item_count, "items"))
        for k in ("links", "inputs", "buttons", "elements"):
            if counts.get(k):
                parts.append(plural(counts[k], k))
        return ", ".join(parts) if parts else "no elements"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "fingerprint": self.fingerprint,
            "type": self.type,
            "heading": self.heading,
            "preview": self.preview,
            "summary": self.summary,
            "elements": [e.to_dict() for e in self.elements],
            "item_count": self.item_count,
            "content_hash": self.content_hash,
            "token_estimate": self.token_estimate,
            "bbox": self.bbox.to_dict(),
            "iframe_path": list(self.iframe_path),
            "cross_origin": self.cross_origin,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Section:
        return cls(
            sid=d["sid"],
            fingerprint=d["fingerprint"],
            type=d["type"],
            heading=d.get("heading"),
            preview=d.get("preview", ""),
            summary=d.get("summary"),
            elements=[Element.from_dict(e) for e in d.get("elements", [])],
            item_count=d.get("item_count"),
            content_hash=d["content_hash"],
            token_estimate=d.get("token_estimate", 0),
            bbox=BBox.from_dict(d["bbox"]),
            iframe_path=tuple(d.get("iframe_path") or ()),
            cross_origin=d.get("cross_origin", False),
        )


def _count_bucket(desc: ElementDesc) -> str:
    if desc.tag == "a" or desc.role == "link":
        return "links"
    if desc.tag in ("input", "textarea", "select") or desc.role in (
        "textbox",
        "combobox",
        "checkbox",
        "radio",
        "searchbox",
        "spinbutton",
        "listbox",
        "switch",
        "slider",
    ):
        return "inputs"
    if desc.tag == "button" or desc.role == "button":
        return "buttons"
    return "elements"


@dataclass(slots=True)
class PageMem:
    url: str
    title: str
    sections: list[Section]
    captured_at: float
    nav_id: int  # increments on navigation; scopes sids
    # VLM one-line visual gist of the screenshot (◉ in the outline). Untrusted
    # routing signal, never load-bearing; None when no vision sidecar. Populated
    # by the summarizer, cached per screen_key. Provenance: model-generated.
    screen_gist: str | None = None

    def section(self, sid: str) -> Section | None:
        for s in self.sections:
            if s.sid == sid:
                return s
        return None

    def find_element(self, ref: str) -> tuple[Section, Element] | None:
        for s in self.sections:
            for e in s.elements:
                if e.ref == ref:
                    return (s, e)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "sections": [s.to_dict() for s in self.sections],
            "captured_at": self.captured_at,
            "nav_id": self.nav_id,
            "screen_gist": self.screen_gist,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PageMem:
        return cls(
            url=d["url"],
            title=d["title"],
            sections=[Section.from_dict(s) for s in d.get("sections", [])],
            captured_at=d.get("captured_at", 0.0),
            nav_id=d.get("nav_id", 0),
            screen_gist=d.get("screen_gist"),
        )


@dataclass(slots=True)
class SectionDiff:
    sid: str
    kind: SectionDiffKind
    section: Section | None = None  # populated for "appeared" (rendered as outline line)
    added: list[Element] = field(default_factory=list)
    removed: list[ElementDesc] = field(default_factory=list)
    # (ref, field, old, new) — field in {"value", "checked", "expanded", "disabled", "text"}
    state_changes: list[tuple[str, str, str, str]] = field(default_factory=list)
    text_added: str = ""  # newly appeared text (status messages, validation errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "kind": self.kind,
            "section": self.section.to_dict() if self.section else None,
            "added": [e.to_dict() for e in self.added],
            "removed": [d.to_dict() for d in self.removed],
            "state_changes": [list(c) for c in self.state_changes],
            "text_added": self.text_added,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SectionDiff:
        return cls(
            sid=d["sid"],
            kind=d["kind"],
            section=Section.from_dict(d["section"]) if d.get("section") else None,
            added=[Element.from_dict(e) for e in d.get("added", [])],
            removed=[ElementDesc.from_dict(x) for x in d.get("removed", [])],
            state_changes=[tuple(c) for c in d.get("state_changes", [])],
            text_added=d.get("text_added", ""),
        )


@dataclass(slots=True)
class Diff:
    kind: DiffKind
    sections: list[SectionDiff] = field(default_factory=list)  # partial / dialog
    new_page: PageMem | None = None  # navigation
    unchanged_sids: list[str] = field(default_factory=list)  # navigation: matched sections
    notes: list[str] = field(default_factory=list)  # alert auto-accepted, popups adopted, ...

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sections": [s.to_dict() for s in self.sections],
            "new_page": self.new_page.to_dict() if self.new_page else None,
            "unchanged_sids": self.unchanged_sids,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Diff:
        return cls(
            kind=d["kind"],
            sections=[SectionDiff.from_dict(s) for s in d.get("sections", [])],
            new_page=PageMem.from_dict(d["new_page"]) if d.get("new_page") else None,
            unchanged_sids=d.get("unchanged_sids", []),
            notes=d.get("notes", []),
        )


def estimate_tokens(text: str) -> int:
    """chars//4 heuristic, consistent everywhere token sizes are reported."""
    return max(1, len(text) // 4)
