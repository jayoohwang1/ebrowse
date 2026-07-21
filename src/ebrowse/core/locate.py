"""ElementDesc -> Playwright locator resolution.

Strategy chain (adapted from WebChallenger get_elem_locator, agent.py ~L1424):
id > testid > role+name > placeholder > href suffix > text. Each candidate is
verified for existence; ambiguous matches fall back to nth_hint order. The
occlusion pre-check lives in actions.py.

Disambiguated picks (nth / .first collapses) are verified against the stored
descriptor before being returned (identity_mismatch below): the page may have
reordered descriptor-identical siblings between observation and action, in
which case the pick silently binds to the wrong element. Refuse > misbind
(docs/adr/0003-strict-ref-matching.md); a false refusal costs one re-outline,
a wrong click can cost the whole task.
"""

from __future__ import annotations

from ebrowse import debug
from ebrowse.errors import CommandError, ExitCode
from ebrowse.model import ElementDesc

_CSS_ESCAPE = str.maketrans({c: f"\\{c}" for c in "!\"#$%&'()*+,./:;<=>?@[\\]^`{|}~"})


def _css_escape(s: str) -> str:
    return s.translate(_CSS_ESCAPE)


def _css_escape_value(s: str) -> str:
    """Escape a string for use inside a double-quoted attribute selector."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _frame_scope_css(page, desc: ElementDesc):
    """CSS fallback frame resolution: re-query each path segment as an iframe
    selector. Strict-mode fails when two iframes share the fid (Salesforce
    keeps a hidden stale duplicate of its Report Builder frame), which is why
    the live frame graph below is tried first."""
    scope = page
    for fid in desc.iframe_path:
        q = fid.replace("\\", "\\\\").replace('"', '\\"')
        scope = scope.frame_locator(f'iframe[id="{q}"], iframe[title="{q}"], iframe[src="{q}"]')
    return scope


_FRAME_ATTRS_JS = "e => [e.id, e.title, e.getAttribute('src'), e.getAttribute('name')]"


async def _match_child_frame(parent_frame, fid: str):
    """The child Frame of parent_frame whose element matches fid (the id,
    title, or src the capture recorded — core/snapshot.py), or None.
    Duplicate matches are broken by visible geometry: the frame element with
    the largest live box wins (a detached-but-lingering duplicate typically
    has display:none and no box at all)."""
    matches = []
    for f in parent_frame.child_frames:
        if f.is_detached():
            continue
        try:
            el = await f.frame_element()
            attrs = await el.evaluate(_FRAME_ATTRS_JS)
        except Exception:
            continue
        src = attrs[2] or ""
        if fid in (attrs[0], attrs[1], attrs[3]) or (src and src == fid) or f.url == fid:
            matches.append((f, el))
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0][0]
    best, best_area = None, -1.0
    for f, el in matches:
        try:
            box = await el.bounding_box()
        except Exception:
            box = None
        area = (box["width"] * box["height"]) if box else 0.0
        if area > best_area:
            best, best_area = f, area
    debug.emit("locate", "frame_ambiguous", level="warn", fid=fid,
               matches=len(matches), picked_area=round(best_area))  # fmt: skip
    return best


async def _frame_scope(page, desc: ElementDesc):
    """Resolve the frame the element lives in (iframe_path from discovery).

    Walks the LIVE Playwright frame graph, matching each path segment against
    the frame element's id/title/src/name — frame_locator CSS re-query is the
    fallback only. Frame objects, unlike frame_locator, tolerate fid
    duplicates (disambiguated by geometry above) and cost nothing per action
    afterwards."""
    if not desc.iframe_path:
        return page
    frame = page.main_frame
    for fid in desc.iframe_path:
        frame = await _match_child_frame(frame, fid)
        if frame is None:
            return _frame_scope_css(page, desc)
    return frame


# Live identity facts for pre-act verification. innerText (layout-aware,
# matches what the user sees) with textContent fallback for non-HTML elements.
_FACTS_JS = """(el) => ({
  tag: el.tagName.toLowerCase(),
  id: el.id || null,
  testid: el.getAttribute('data-testid') || el.getAttribute('data-qa')
          || el.getAttribute('data-test') || null,
  text: ((typeof el.innerText === 'string' ? el.innerText : el.textContent) || '').slice(0, 400),
})"""


def _text_key(s: str) -> str:
    """Comparison key for text heads: whitespace-insensitive (subtree_text
    joins text nodes with spaces where innerText may not), casefolded (CSS
    text-transform), truncated to match the 80-char text_head cap."""
    return "".join(s.split()).casefold()[:80]


def identity_mismatch(desc: ElementDesc, live: dict) -> str | None:
    """Reason the live element clearly is NOT desc, or None if plausible.

    Strong facts are strict: tag always; id/testid whenever the descriptor
    recorded them. Text is compared leniently — legitimate in-place changes
    happen ("Add to cart" -> "Added") — but a live text head that shares no
    prefix relation with the stored one indicates a different sibling, and
    when in doubt we refuse (ADR 0003). Form controls are exempt from the
    text check: their rendered text is state (value/options), not identity.
    """
    tag = live.get("tag")
    if tag and tag != desc.tag:
        return f"tag <{tag}> != <{desc.tag}>"
    if desc.id and live.get("id") != desc.id:
        return f"id {live.get('id') or '(none)'} != {desc.id}"
    if desc.testid and live.get("testid") != desc.testid:
        return f"testid {live.get('testid') or '(none)'} != {desc.testid}"
    if desc.text_head and desc.tag not in ("input", "textarea", "select"):
        live_text = (live.get("text") or "").strip()
        if live_text:
            a, b = _text_key(live_text), _text_key(desc.text_head)
            if a and b and not (a.startswith(b) or b.startswith(a)):
                want = desc.text_head.strip()
                return f'text "{live_text[:40]}" != "{want[:40]}"'
    return None


_WITNESS_TOLERANCE_PX = 3.0


async def _witness_override(picked, witness, ref: str | None, strategy: int):
    """Geometry cross-check of a suspicious pick against the ref's node
    binding. Returns the witness (act on the exact observed node) when both
    have live boxes that materially disagree; None means keep the pick
    (agreement, or no usable witness — dead binding, hidden element)."""
    if witness is None:
        return None
    try:
        wbox = await witness.bounding_box()
        if wbox is None:
            return None
        pbox = await picked.bounding_box(timeout=2000)
        if pbox is None:
            return None
    except Exception:
        return None
    delta = max(abs(pbox[k] - wbox[k]) for k in ("x", "y", "width", "height"))
    if delta <= _WITNESS_TOLERANCE_PX:
        return None
    debug.emit("locate", "binding_witness_override", level="warn", ref=ref,
               strategy=strategy, delta_px=round(delta, 1))  # fmt: skip
    return witness


async def _live_facts(loc) -> dict | None:
    """One evaluate fetching identity facts; None if the element is gone or
    the evaluate fails (the action itself will then surface the real error)."""
    try:
        return await loc.evaluate(_FACTS_JS)
    except Exception:
        return None


async def resolve(page, desc: ElementDesc, ref: str | None = None, witness=None):
    """Return a single-element locator for desc or raise CommandError(2).

    `witness` is the ref's capture-time CDP node binding (a CdpTarget or
    None): any SUSPICIOUS pick — nth-disambiguated, .first-collapsed, or made
    after a mismatch — is compared against the witness's live geometry, and
    on disagreement the witness wins (it is the exact node the outline
    described; ADR 0015). This closes the identical-siblings reorder hole the
    identity facts cannot see. The zero-cost happy path is unchanged.
    """
    scope = await _frame_scope(page, desc)
    candidates: list[tuple] = []  # (locator, suspicious, unique_only)
    if desc.id:
        candidates.append((scope.locator(f"#{_css_escape(desc.id)}"), False, False))
    if desc.testid:
        for attr in ("data-testid", "data-qa", "data-test"):
            candidates.append((scope.locator(f'[{attr}="{desc.testid}"]'), False, False))
    if desc.role and desc.name:
        candidates.append((scope.get_by_role(desc.role, name=desc.name, exact=True), False, False))
        candidates.append((scope.get_by_role(desc.role, name=desc.name), False, False))
    if desc.placeholder:
        candidates.append((scope.get_by_placeholder(desc.placeholder, exact=True), False, False))
    if desc.role and desc.text_head:
        # roles like link/menuitem/option/tab take their accessible name from
        # text content, which discovery stores in text_head rather than name.
        # MUST come before the href candidates: repeated hrefs ("#", "/cart")
        # match many links, and nth_hint counts identical DESCRIPTORS, not
        # href matches — resolving 'Products' as the 0th 'a[href$="#"]' once
        # hovered the Home link while reporting 'link "Products"'.
        candidates.append(
            (scope.get_by_role(desc.role, name=desc.text_head, exact=True), False, False)
        )
    if desc.href:
        base = scope.locator(f'a[href$="{desc.href}"]')
        # same wrong-element risk: constrain repeated hrefs by the link text
        candidates.append(
            (base.filter(has_text=desc.text_head[:60]) if desc.text_head else base, False, False)
        )
        if "?" in desc.href:
            candidates.append(
                (scope.locator(f'a[href$="{desc.href.split("?")[0]}"]'), False, False)
            )
    if desc.text_head and desc.tag in ("a", "button", "summary"):
        # .first collapses a multi-match to count()==1 — always suspicious
        candidates.append(
            (scope.locator(desc.tag).filter(has_text=desc.text_head[:60]).first, True, False)
        )
    if desc.text_head:
        candidates.append((scope.locator(desc.tag, has_text=desc.text_head[:60]), False, False))
    # Anonymous-element fallbacks (ADR 0015 follow-up): class tokens and
    # filtered custom attrs survive node replacement, which kills the CDP
    # binding. UNIQUE matches only — nth over a class-matched set would not
    # align with nth_hint (counted over descriptor-identical elements), and
    # without a live binding there is no witness to catch a misbind.
    if desc.cls:
        sel = desc.tag + "".join(f".{t}" for t in desc.cls.split())
        candidates.append((scope.locator(sel), True, True))
    if desc.attrs:
        sel = desc.tag + "".join(f'[{k}="{_css_escape_value(v)}"]' for k, v in desc.attrs)
        candidates.append((scope.locator(sel), True, True))

    # Pre-act verification (issue #12): a unique match with no earlier
    # suspicion returns at zero cost; any disambiguated pick — and every pick
    # after a mismatch has been seen (the .first fallbacks would otherwise
    # smuggle the same wrong sibling through) — is checked against the stored
    # descriptor with ONE evaluate. On mismatch we try the next candidate,
    # which often recovers the right element (e.g. an exact-text candidate
    # after a too-broad name match); only if none verifies do we refuse.
    # Reorders among FULLY identical siblings are invisible to identity facts;
    # those are caught by the witness geometry check on suspicious picks below.
    mismatch: str | None = None
    for i, (loc, suspicious, unique_only) in enumerate(candidates):
        try:
            n = await loc.count()
        except Exception:
            continue
        if n == 0:
            continue
        if unique_only and n != 1:
            continue  # refuse-over-misbind: never nth-guess a fallback match
        if n == 1:
            if mismatch is None and not suspicious:
                debug.emit("locate", "resolved", ref=ref, strategy=i, matches=1, verified=False)
                return loc  # happy path: unique match, nothing suspicious
            picked = loc
        elif desc.nth_hint < n:
            picked = loc.nth(desc.nth_hint)
        else:
            continue
        facts = await _live_facts(picked)
        reason = identity_mismatch(desc, facts) if facts is not None else None
        if reason is None:
            # identity facts are blind to reorders among FULLY identical
            # siblings — the witness geometry check catches those (ADR 0015)
            override = await _witness_override(picked, witness, ref, i)
            if override is not None:
                return override
            debug.emit(
                "locate", "resolved", ref=ref, strategy=i, matches=n,
                nth=desc.nth_hint if n > 1 else 0, verified=facts is not None,
                after_mismatch=mismatch or "",
            )  # fmt: skip
            return picked
        if mismatch is None:
            # a previously-issued ref resolving to a live element whose
            # identity facts contradict the stored descriptor — a candidate
            # ref rebinding (the page reordered/replaced siblings). If a later
            # strategy verifies, this was recovered; otherwise we refuse below.
            debug.emit("locate", "ref_rebound", level="warn", ref=ref,
                       strategy=i, reason=reason)  # fmt: skip
        mismatch = mismatch or reason

    debug.emit("locate", "locate_failed", level="warn", ref=ref,
               mismatch=mismatch or "", candidates=len(candidates))  # fmt: skip
    if not candidates:
        # anonymous element (no id/testid/name/text/href): the descriptor has
        # no locatable identity. Callers rescue via the capture-time node
        # binding (ADR 0015); this error surfaces only when that is dead too,
        # and a re-outline mints a fresh binding — the hint is genuine.
        who = f"{ref} ({desc.short_desc()})" if ref else desc.short_desc()
        raise CommandError(
            f"{who} has no locatable identity (icon-only/anonymous element) and its "
            "node binding from the last outline is gone — run 'ebrowse outline' to re-bind",
            ExitCode.USAGE,
        )
    if mismatch:
        who = f"stale ref {ref}" if ref else "stale ref"
        raise CommandError(
            f"{who}: {desc.short_desc()} now resolves to a different element "
            f"({mismatch}) — the page likely reordered; run 'ebrowse outline'",
            ExitCode.USAGE,
        )
    raise CommandError(
        f"could not locate {desc.short_desc()} on the live page "
        "(it may have changed) — run 'ebrowse outline'",
        ExitCode.USAGE,
    )
