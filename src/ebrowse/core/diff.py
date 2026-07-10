"""Diff engine: PageMem x PageMem -> Diff (pure).

Adapted from WebChallenger UpdateSection's element delta (paper Alg. 2),
generalized to whole pages: sections matched by fingerprint (order-preserving
for duplicates), element add/remove by descriptor multiset, state changes by
ref. Navigation vs partial is the *caller's* call (URL comparison lives in the
session); this module only compares two observed pages.
"""

from __future__ import annotations

import difflib

from ebrowse.model import Diff, Element, PageMem, Section, SectionDiff

_TRACKED_STATE = ("value", "checked", "expanded", "disabled", "pressed", "selected")


def added_text(old: str, new: str, max_len: int = 160) -> str:
    """Text present in `new` but not `old` — what a status message / validation
    error / result count 'said' after an action. Word-level diff so an appended
    sentence is quoted alone, not the whole surrounding section."""
    old_words, new_words = old.split(), new.split()
    sm = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    fresh = [
        " ".join(new_words[j1:j2])
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes()
        if tag in ("insert", "replace") and j2 > j1
    ]
    fresh = [f for f in fresh if len(f) > 3]
    if not fresh:
        return ""
    out = " … ".join(fresh[:3])
    return out if len(out) <= max_len else out[: max_len - 1] + "…"


def _pair_sections(
    prev: PageMem, new: PageMem
) -> tuple[list[tuple[Section, Section]], list[Section], list[Section]]:
    """Match sections across observations by fingerprint, preserving order for
    duplicate fingerprints (two identical card rails stay distinct)."""
    prev_pool: dict[str, list[Section]] = {}
    for s in prev.sections:
        prev_pool.setdefault(s.fingerprint, []).append(s)
    pairs: list[tuple[Section, Section]] = []
    appeared: list[Section] = []
    for s in new.sections:
        bucket = prev_pool.get(s.fingerprint)
        if bucket:
            pairs.append((bucket.pop(0), s))
        else:
            appeared.append(s)
    disappeared = [s for bucket in prev_pool.values() for s in bucket]
    return pairs, appeared, disappeared


def _element_delta(
    prev: Section, new: Section, prev_text: str = "", new_text: str = ""
) -> SectionDiff | None:
    prev_by_key: dict[tuple, list[Element]] = {}
    for e in prev.elements:
        prev_by_key.setdefault(e.desc.match_key(), []).append(e)

    added: list[Element] = []
    state_changes: list[tuple[str, str, str, str]] = []
    for e in new.elements:
        bucket = prev_by_key.get(e.desc.match_key())
        if bucket:
            old = bucket.pop(0)
            for field_name in _TRACKED_STATE:
                ov, nv = getattr(old.state, field_name), getattr(e.state, field_name)
                if ov != nv and not (ov is None and nv is None):
                    state_changes.append((e.ref, field_name, _fmt(ov), _fmt(nv)))
        else:
            added.append(e)
    removed = [e.desc for bucket in prev_by_key.values() for e in bucket]

    text_changed = prev.content_hash != new.content_hash
    if not added and not removed and not state_changes and not text_changed:
        return None
    return SectionDiff(
        sid=new.sid,
        kind="changed",
        added=added,
        removed=removed,
        state_changes=state_changes,
        text_added=added_text(prev_text, new_text) if text_changed else "",
    )


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def diff_pages(
    prev: PageMem,
    new: PageMem,
    prev_texts: dict[str, str] | None = None,
    new_texts: dict[str, str] | None = None,
) -> Diff:
    """Same-page diff. Caller has already ruled out navigation.

    prev_texts/new_texts: optional sid -> subtree text maps, used to quote
    newly appeared text (status messages, validation errors) in the diff."""
    pairs, appeared, disappeared = _pair_sections(prev, new)
    prev_texts = prev_texts or {}
    new_texts = new_texts or {}

    sections: list[SectionDiff] = []
    for s in appeared:
        sections.append(SectionDiff(sid=s.sid, kind="appeared", section=s))
    for s in disappeared:
        sections.append(SectionDiff(sid=s.sid, kind="disappeared", section=s))
    for old, cur in pairs:
        delta = _element_delta(old, cur, prev_texts.get(old.sid, ""), new_texts.get(cur.sid, ""))
        if delta:
            sections.append(delta)

    if not sections:
        return Diff(kind="no_change")
    kind = "dialog" if any(
        sd.kind == "appeared" and sd.section and sd.section.type == "dialog" for sd in sections
    ) else "partial"  # fmt: skip
    # stable presentation order: appeared, disappeared, changed
    order = {"appeared": 0, "disappeared": 1, "changed": 2}
    sections.sort(key=lambda sd: (order[sd.kind], sd.sid))
    return Diff(kind=kind, sections=sections)


def navigation_diff(prev: PageMem | None, new: PageMem) -> Diff:
    """Cross-page diff: full new outline, unchanged persistent chrome marked."""
    unchanged: list[str] = []
    if prev is not None:
        prev_keys = {(s.fingerprint, s.content_hash) for s in prev.sections}
        unchanged = [s.sid for s in new.sections if (s.fingerprint, s.content_hash) in prev_keys]
    return Diff(kind="navigation", new_page=new, unchanged_sids=unchanged)
