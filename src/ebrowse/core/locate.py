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


def _frame_scope(page, desc: ElementDesc):
    """Resolve the frame the element lives in (iframe_path from discovery).
    fid is the frame's id, title, or src attribute — whichever capture used
    (core/snapshot.py) — so try all three."""
    scope = page
    for fid in desc.iframe_path:
        q = fid.replace("\\", "\\\\").replace('"', '\\"')
        scope = scope.frame_locator(f'iframe[id="{q}"], iframe[title="{q}"], iframe[src="{q}"]')
    return scope


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


async def _live_facts(loc) -> dict | None:
    """One evaluate fetching identity facts; None if the element is gone or
    the evaluate fails (the action itself will then surface the real error)."""
    try:
        return await loc.evaluate(_FACTS_JS)
    except Exception:
        return None


async def resolve(page, desc: ElementDesc, ref: str | None = None):
    """Return a single-element locator for desc or raise CommandError(2)."""
    scope = _frame_scope(page, desc)
    candidates = []
    if desc.id:
        candidates.append(scope.locator(f"#{_css_escape(desc.id)}"))
    if desc.testid:
        for attr in ("data-testid", "data-qa", "data-test"):
            candidates.append(scope.locator(f'[{attr}="{desc.testid}"]'))
    if desc.role and desc.name:
        candidates.append(scope.get_by_role(desc.role, name=desc.name, exact=True))
        candidates.append(scope.get_by_role(desc.role, name=desc.name))
    if desc.placeholder:
        candidates.append(scope.get_by_placeholder(desc.placeholder, exact=True))
    if desc.role and desc.text_head:
        # roles like link/menuitem/option/tab take their accessible name from
        # text content, which discovery stores in text_head rather than name.
        # MUST come before the href candidates: repeated hrefs ("#", "/cart")
        # match many links, and nth_hint counts identical DESCRIPTORS, not
        # href matches — resolving 'Products' as the 0th 'a[href$="#"]' once
        # hovered the Home link while reporting 'link "Products"'.
        candidates.append(scope.get_by_role(desc.role, name=desc.text_head, exact=True))
    if desc.href:
        base = scope.locator(f'a[href$="{desc.href}"]')
        # same wrong-element risk: constrain repeated hrefs by the link text
        candidates.append(base.filter(has_text=desc.text_head[:60]) if desc.text_head else base)
        if "?" in desc.href:
            candidates.append(scope.locator(f'a[href$="{desc.href.split("?")[0]}"]'))
    if desc.text_head and desc.tag in ("a", "button", "summary"):
        candidates.append(scope.locator(desc.tag).filter(has_text=desc.text_head[:60]).first)
    if desc.text_head:
        candidates.append(scope.locator(desc.tag, has_text=desc.text_head[:60]))

    # Pre-act verification (issue #12): a unique match with no earlier
    # suspicion returns at zero cost; any disambiguated pick — and every pick
    # after a mismatch has been seen (the .first fallbacks would otherwise
    # smuggle the same wrong sibling through) — is checked against the stored
    # descriptor with ONE evaluate. On mismatch we try the next candidate,
    # which often recovers the right element (e.g. an exact-text candidate
    # after a too-broad name match); only if none verifies do we refuse.
    # NOTE: siblings whose descriptors are FULLY identical (no id/testid,
    # same text) are indistinguishable by identity facts; a reorder among
    # them still misbinds. Detecting that would need extra stored state.
    mismatch: str | None = None
    for i, loc in enumerate(candidates):
        try:
            n = await loc.count()
        except Exception:
            continue
        if n == 0:
            continue
        if n == 1:
            if mismatch is None:
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

    debug.emit("locate", "locate_failed", level="warn", ref=ref, mismatch=mismatch or "")
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
