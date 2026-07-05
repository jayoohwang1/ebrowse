"""ElementDesc -> Playwright locator resolution.

Strategy chain (adapted from WebChallenger get_elem_locator, agent.py ~L1424):
id > testid > role+name > placeholder > href suffix > text. Each candidate is
verified for existence; ambiguous matches fall back to nth_hint order. The
occlusion pre-check lives in actions.py.
"""

from __future__ import annotations

from ebrowse.errors import CommandError
from ebrowse.model import ElementDesc

_CSS_ESCAPE = str.maketrans({c: f"\\{c}" for c in "!\"#$%&'()*+,./:;<=>?@[\\]^`{|}~"})


def _css_escape(s: str) -> str:
    return s.translate(_CSS_ESCAPE)


def _frame_scope(page, desc: ElementDesc):
    """Resolve the frame the element lives in (iframe_path from discovery)."""
    scope = page
    for fid in desc.iframe_path:
        scope = scope.frame_locator(f'iframe[id="{fid}"], iframe[title="{fid}"]')
    return scope


async def resolve(page, desc: ElementDesc):
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
    if desc.href:
        candidates.append(scope.locator(f'a[href$="{desc.href}"]'))
        if "?" in desc.href:
            candidates.append(scope.locator(f'a[href$="{desc.href.split("?")[0]}"]'))
    if desc.role and desc.text_head:
        # roles like menuitem/option/tab take their accessible name from text
        # content, which discovery stores in text_head rather than name
        candidates.append(scope.get_by_role(desc.role, name=desc.text_head, exact=True))
    if desc.text_head and desc.tag in ("a", "button", "summary"):
        candidates.append(scope.locator(desc.tag).filter(has_text=desc.text_head[:60]).first)
    if desc.text_head:
        candidates.append(scope.locator(desc.tag, has_text=desc.text_head[:60]))

    for loc in candidates:
        try:
            n = await loc.count()
        except Exception:
            continue
        if n == 1:
            return loc
        if n > 1 and desc.nth_hint < n:
            return loc.nth(desc.nth_hint)

    raise CommandError(
        f"could not locate {desc.short_desc()} on the live page "
        "(it may have changed) — run 'ebrowse outline'",
        2,
    )
