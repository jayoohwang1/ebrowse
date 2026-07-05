"""Stable identity: section fingerprints, element descriptors, and the RefRegistry.

Section fingerprints (docs/output-contracts.md) survive DOM mutations and revisits so diffs
and summary caches can say "same section, changed contents". Element refs are
session-scoped, assigned once per durable descriptor, and reused across
re-snapshots and navigations (a persistent header keeps its refs on every page).
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from ebrowse.model import ElementDesc

_STATE_CLASS_RE = re.compile(
    r"^(is-|has-|js-)|"
    r"(^|-)(active|open|opened|closed|selected|checked|hidden|show|shown|visible|"
    r"hover|focus|focused|disabled|expanded|collapsed|current|loading|dirty)($|-)",
)
_HASHY_CLASS_RE = re.compile(r"\d|^css-|^jsx-|^sc-|__|--[0-9a-f]{4,}$")
_MAX_CLASS_TOKENS = 4


def normalize_class(cls: str) -> str:
    """Strip state/generated class tokens; keep a few stable ones, sorted."""
    tokens = []
    for tok in cls.split():
        if _STATE_CLASS_RE.search(tok) or _HASHY_CLASS_RE.search(tok):
            continue
        tokens.append(tok)
    return " ".join(sorted(tokens)[:_MAX_CLASS_TOKENS])


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:10]


def section_fingerprint(
    tag: str,
    cls: str,
    role: str,
    heading: str,
    iframe_path: tuple[str, ...],
    parent_tags: tuple[str, ...],
) -> str:
    return _short_hash(
        tag, normalize_class(cls), role, heading.lower()[:60], "/".join(iframe_path),
        ">".join(parent_tags)
    )  # fmt: skip


def content_hash(text: str, element_keys: list[tuple]) -> str:
    return _short_hash(text[:4000], repr(sorted(map(repr, element_keys))))


def normalize_href(href: str, page_url: str) -> str | None:
    """Same-origin absolute/relative hrefs -> path?query; external kept whole."""
    if not href:
        return None
    href = href.strip()
    if href.startswith(("javascript:", "#")):
        return None if href.startswith("javascript:") else href
    try:
        page = urlsplit(page_url)
        target = urlsplit(href)
    except ValueError:
        return href[:200]
    if target.scheme and target.netloc and target.netloc != page.netloc:
        return href[:200]  # external: keep whole (scheme+host matter)
    path = target.path or "/"
    if target.query:
        path += f"?{target.query}"
    if target.fragment and not target.path and not target.query:
        return f"#{target.fragment}"
    return path[:200]


class RefRegistry:
    """Session-global mapping descriptor -> ref (@eN), monotonic, never reused.

    Matching is exact on ElementDesc.match_key() with nth-order disambiguation:
    the k-th element with a given key on the page binds to the k-th registered
    ref for that key. Strict by design — misbinding is worse than ref churn
    (see docs/adr/0003-strict-ref-matching.md).
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple, list[str]] = {}
        self._desc_by_ref: dict[str, ElementDesc] = {}
        self._next = 1

    def assign(self, descs: list[ElementDesc]) -> list[str]:
        """Assign refs to a page's descriptors (document order). Sets nth_hint."""
        seen_count: dict[tuple, int] = {}
        refs: list[str] = []
        for desc in descs:
            key = desc.match_key()
            nth = seen_count.get(key, 0)
            seen_count[key] = nth + 1
            desc.nth_hint = nth
            existing = self._by_key.setdefault(key, [])
            if nth < len(existing):
                ref = existing[nth]
                self._desc_by_ref[ref] = desc  # refresh stored desc (volatile-adjacent)
            else:
                ref = f"@e{self._next}"
                self._next += 1
                existing.append(ref)
                self._desc_by_ref[ref] = desc
            refs.append(ref)
        return refs

    def lookup(self, ref: str) -> ElementDesc | None:
        return self._desc_by_ref.get(ref)

    def __len__(self) -> int:
        return len(self._desc_by_ref)
