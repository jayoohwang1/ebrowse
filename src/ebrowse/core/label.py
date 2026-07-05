"""Deterministic section labels: heading detection + text previews.

These are the zero-LLM fallback labels shown in outlines (provenance marker '|').
The optional summarizer replaces them with one-line model summaries ('≈').
"""

from __future__ import annotations

from ebrowse.core.split import RawSection

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "legend", "caption", "summary", "figcaption")


def section_heading(raw: RawSection) -> str | None:
    """Nearest heading: aria name on the section node, else first heading tag inside."""
    for node in raw.all_nodes():
        nm = node.attrs.get("nm")
        if nm:
            return nm[:80]
    # Headings inside list/table items describe one item, not the section
    # (e.g. the first product card's <h3>) — only explicit captions qualify.
    if raw.node.is_list_group or raw.node.tag in ("ul", "ol", "dl"):
        return None
    if raw.node.tag == "table":
        for n in raw.iter_walk():
            if n.tag == "caption":
                text = n.subtree_text(cap=120).strip()
                return text[:80] or None
        return None
    best: tuple[int, str] | None = None  # (heading level rank, text)
    for n in raw.iter_walk():
        if n.tag in _HEADING_TAGS:
            text = n.subtree_text(cap=200).strip()
            if not text:
                continue
            rank = _HEADING_TAGS.index(n.tag) if n.tag in _HEADING_TAGS else len(_HEADING_TAGS)
            if best is None or rank < best[0]:
                best = (rank, text[:80])
            if rank == 0:
                break
    return best[1] if best else None


def section_preview(raw: RawSection, heading: str | None, chars: int) -> str:
    """First N chars of the section's text, skipping the heading itself."""
    parts: list[str] = []
    budget = chars + 40
    for n in raw.iter_walk():
        if budget <= 0:
            break
        if n.tag in _HEADING_TAGS:
            continue
        if n.text:
            parts.append(n.text)
            budget -= len(n.text)
    text = " ".join(parts).strip()
    if heading and text.startswith(heading):
        text = text[len(heading) :].strip()
    if len(text) > chars:
        text = text[: chars - 1].rstrip() + "…"
    return text


def deterministic_label(heading: str | None, preview: str, max_len: int = 90) -> str:
    """The '|'-provenance outline label: verbatim page text, quoted by renderer."""
    if heading and preview:
        label = f"{heading} — {preview}"
    else:
        label = heading or preview or ""
    if len(label) > max_len:
        label = label[: max_len - 1].rstrip() + "…"
    return label


def media_label(raw: RawSection) -> str | None:
    """For media sections: alt text is usually the best deterministic label."""
    for n in raw.iter_walk():
        if n.tag == "img" and n.attrs.get("alt"):
            return f"image: {n.attrs['alt'][:70]}"
        if n.tag == "video":
            return "video"
    return None
