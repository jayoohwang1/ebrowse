"""Diff engine: PageMem x PageMem -> Diff (pure).

Adapted from WebChallenger UpdateSection's element delta (paper Alg. 2),
generalized to whole pages: sections matched by fingerprint (order-preserving
for duplicates), element add/remove by descriptor multiset, state changes by
ref. Navigation vs partial is the *caller's* call (URL comparison lives in the
session); this module only compares two observed pages.
"""

from __future__ import annotations

import difflib

from ebrowse import debug
from ebrowse.model import Diff, Element, PageMem, Section, SectionDiff

_TRACKED_STATE = ("value", "checked", "expanded", "disabled", "pressed", "selected")

# Added-text quoting budgets, in characters (~4 chars/token). The default keeps
# diffs lean; sections the agent has *expanded* this page get a much larger
# budget via diff_pages(expanded_fps=...) — it is actively reading them, so
# verbose diffs there are worth the spend (issue #11).
TEXT_BUDGET = 500
EXPANDED_TEXT_BUDGET = 8000
# Fragments at or under this length rank as "status-sized" (status messages,
# validation errors, result counts) and are quoted before bulk insertions.
_SHORT_FRAGMENT = 100
_MAX_FRAGMENTS = 5


def _elide(frag: str, cap: int) -> str:
    """Fit one fragment into `cap` chars. An over-long fragment quotes its
    start AND end joined by an ellipsis — bulk insertions often carry the
    summary line at one end, so a bare prefix would lose it."""
    if len(frag) <= cap:
        return frag
    words = frag.split()
    half = max(1, (cap - 3) // 2)  # 3 = len(" … ")
    head: list[str] = []
    used = -1
    for w in words:
        if head and used + 1 + len(w) > half:
            break
        head.append(w)
        used += 1 + len(w)
    rest = words[len(head) :]
    if not rest:  # a single word longer than the cap
        return frag[: cap - 1] + "…"
    tail: list[str] = []
    used = -1
    for w in reversed(rest):
        if tail and used + 1 + len(w) > half:
            break
        tail.append(w)
        used += 1 + len(w)
    out = " ".join(head) + " … " + " ".join(reversed(tail))
    return out if len(out) <= cap else out[: cap - 1] + "…"


def added_text(old: str, new: str, max_len: int = TEXT_BUDGET) -> str:
    """Text present in `new` but not `old` — what a status message / validation
    error / result count 'said' after an action. Word-level diff so an appended
    sentence is quoted alone, not the whole surrounding section.

    Quoting rules (deterministic; this output is golden-tested):
    - a replaced fragment carries one unchanged word of context per side — a
      "20" → "30" result-count tick quotes as "Showing 30 results.", not a
      bare "30" (which the noise filter would drop);
    - fragments ≤ 100 chars are quoted before longer ones (status messages beat
      bulk content), document order within each tier;
    - each fragment is capped at max_len // min(n_fragments, 3) chars (floor
      120) so one long insertion cannot crowd out the others — a lone fragment
      may use the whole budget;
    - an over-cap fragment is elided as "start … end", not a bare prefix;
    - up to 5 fragments, joined by " … ", within `max_len` total."""
    old_words, new_words = old.split(), new.split()
    sm = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    fresh: list[str] = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag not in ("insert", "replace") or j2 <= j1:
            continue
        lo, hi = j1, j2
        if tag == "replace":  # replaced words need neighbors to mean anything
            lo, hi = max(0, j1 - 1), min(len(new_words), j2 + 1)
        fresh.append(" ".join(new_words[lo:hi]))
    fresh = [f for f in fresh if len(f) > 3]
    if not fresh:
        return ""
    ranked = [f for f in fresh if len(f) <= _SHORT_FRAGMENT]
    ranked += [f for f in fresh if len(f) > _SHORT_FRAGMENT]
    frag_cap = max(120, max_len // min(len(ranked), 3))
    quotes: list[str] = []
    used = 0
    for frag in ranked[:_MAX_FRAGMENTS]:
        room = max_len - used - (3 if quotes else 0)
        if quotes and room < 24:  # not worth quoting a sliver
            break
        q = _elide(frag, min(frag_cap, room))
        used += len(q) + (3 if quotes else 0)
        quotes.append(q)
    return " … ".join(quotes)


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
    prev: Section,
    new: Section,
    prev_text: str = "",
    new_text: str = "",
    text_budget: int = TEXT_BUDGET,
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
    if debug.enabled():
        # Which comparison drove the verdict: element multiset delta, tracked
        # state fields, and/or the section content hash.
        debug.emit(
            "diff",
            "section_verdict",
            sid=new.sid,
            verdict="changed",
            added=len(added),
            removed=len(removed),
            state_changes=len(state_changes),
            content_hash_changed=text_changed,
        )
        # refs that stopped resolving on the SAME page across observes — the
        # per-ref "ref_gone" anomaly channel (navigation churn never reaches
        # here: diff_pages is same-page only). Capped: a bulk section swap is
        # already visible in the counts above.
        for e in [e for bucket in prev_by_key.values() for e in bucket][:5]:
            debug.emit("diff", "ref_gone", level="warn", ref=e.ref, sid=new.sid,
                       desc=e.desc.short_desc())  # fmt: skip
    return SectionDiff(
        sid=new.sid,
        kind="changed",
        added=added,
        removed=removed,
        state_changes=state_changes,
        text_added=added_text(prev_text, new_text, text_budget) if text_changed else "",
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
    expanded_fps: set[str] | None = None,
) -> Diff:
    """Same-page diff. Caller has already ruled out navigation.

    prev_texts/new_texts: optional sid -> subtree text maps, used to quote
    newly appeared text (status messages, validation errors) in the diff.
    expanded_fps: fingerprints of sections the agent has expanded on this page —
    their text diffs get EXPANDED_TEXT_BUDGET instead of TEXT_BUDGET."""
    pairs, appeared, disappeared = _pair_sections(prev, new)
    prev_texts = prev_texts or {}
    new_texts = new_texts or {}
    expanded_fps = expanded_fps or set()

    if debug.enabled():
        for s in appeared:
            debug.emit("diff", "section_verdict", sid=s.sid, verdict="new",
                       fingerprint=s.fingerprint, type=s.type)  # fmt: skip
        for s in disappeared:
            debug.emit("diff", "section_verdict", sid=s.sid, verdict="gone",
                       fingerprint=s.fingerprint, type=s.type)  # fmt: skip
        # section_reshaped anomaly: a fingerprint failed to match across
        # observes of the same page while the content clearly overlaps — the
        # fingerprint inputs (class/heading/ancestry) churned under stable
        # content, which defeats section-level diffing and summary caching.
        for s in appeared:
            for old in disappeared:
                if s.content_hash == old.content_hash or (
                    s.heading and s.heading == old.heading and s.type == old.type
                ):
                    debug.emit(
                        "diff", "section_reshaped", level="warn",
                        new_sid=s.sid, new_fingerprint=s.fingerprint,
                        old_fingerprint=old.fingerprint, heading=s.heading or "",
                        content_hash_equal=s.content_hash == old.content_hash,
                    )  # fmt: skip
                    break

    sections: list[SectionDiff] = []
    for s in appeared:
        sections.append(SectionDiff(sid=s.sid, kind="appeared", section=s))
    for s in disappeared:
        sections.append(SectionDiff(sid=s.sid, kind="disappeared", section=s))
    for old, cur in pairs:
        budget = EXPANDED_TEXT_BUDGET if cur.fingerprint in expanded_fps else TEXT_BUDGET
        delta = _element_delta(
            old, cur, prev_texts.get(old.sid, ""), new_texts.get(cur.sid, ""), budget
        )
        if delta:
            sections.append(delta)

    if debug.enabled():
        changed = sum(1 for sd in sections if sd.kind == "changed")
        debug.emit(
            "diff",
            "summary",
            matched=len(pairs),
            unchanged=len(pairs) - changed,
            changed=changed,
            new=len(appeared),
            gone=len(disappeared),
        )
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
