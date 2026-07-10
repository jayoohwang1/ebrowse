// diagnose.js — failure-only blocker diagnostic, run AFTER a click was refused
// (trial failure or Playwright interception). One evaluate against the target
// element, in its own frame. Read-only, no layout thrash beyond rects.
//
// Returns:
//   cover:       display name of the unrelated element at the target's center
//   chain:       cover's composed ancestor chain (identifying attrs only), used
//                by the action layer to find an exposed @ref for the cover
//   coverDialog: display name of the cover's dialog ancestor, if any
//   openDialog:  display name of a visible dialog-ish element anywhere on the
//                page that does not contain the target (a backdrop div often
//                covers the target while the dialog itself sits elsewhere)
//   inert / disabledFieldset / pointerEvents: target effective-state clues
(el) => {
  const name = (n) =>
    n.tagName.toLowerCase() +
    (n.id ? "#" + n.id : "") +
    ((n.getAttribute("aria-label") || n.textContent || "").trim().slice(0, 40)
      ? ' "' + (n.getAttribute("aria-label") || n.textContent).trim().slice(0, 40) + '"'
      : "");
  const within = (anc, n) => {
    while (n) {
      if (n === anc) return true;
      n = n.parentNode || (n instanceof ShadowRoot ? n.host : null);
    }
    return false;
  };
  const visible = (n) => {
    const r = n.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(n).visibility !== "hidden";
  };
  // browser-defined label activation: decoration inside an associated <label>
  // is a click surface for the control, never a blocker (mirrors the preflight
  // in actions.py — without this, a restyled control's own icon would be
  // misreported as "no exposed ref (likely a new overlay)")
  const inLabel = (t) => {
    const labs = el.labels ? Array.from(el.labels) : [];
    const wrap = el.closest ? el.closest("label") : null;
    if (wrap && !labs.includes(wrap)) labs.push(wrap);
    return labs.some((l) => within(l, t));
  };

  const out = {
    cover: null,
    chain: [],
    coverDialog: null,
    openDialog: null,
    coverInLabel: 0,
    inert: !!el.closest("[inert]"),
    disabledFieldset: !!(el.matches && el.matches("fieldset:disabled *")),
    pointerEvents: getComputedStyle(el).pointerEvents,
  };

  const r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2,
    cy = r.top + r.height / 2;
  if (cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight) {
    const t = document.elementFromPoint(cx, cy);
    if (t && !within(el, t) && !within(t, el) && inLabel(t)) out.coverInLabel = 1;
    if (t && !within(el, t) && !within(t, el) && !inLabel(t)) {
      out.cover = name(t);
      const dlg = t.closest("dialog,[role=dialog],[role=alertdialog]");
      if (dlg) out.coverDialog = name(dlg);
      let n = t;
      for (let i = 0; n && n !== document.documentElement && i < 12; i++) {
        out.chain.push({
          tag: n.tagName.toLowerCase(),
          id: n.id || null,
          tid:
            n.getAttribute("data-testid") ||
            n.getAttribute("data-qa") ||
            n.getAttribute("data-test") ||
            null,
          role: n.getAttribute("role") || null,
          nm: (n.getAttribute("aria-label") || "").trim().slice(0, 120) || null,
        });
        n = n.parentElement || (n.getRootNode() instanceof ShadowRoot ? n.getRootNode().host : null);
      }
    }
  }

  // A dialog can block without being the hit target (its backdrop div is).
  // Visible candidates only — closed dialogs commonly keep role=dialog.
  try {
    for (const c of document.querySelectorAll(
      ':modal,[aria-modal="true"],dialog[open],[role=dialog],[role=alertdialog]'
    )) {
      if (c === el || within(c, el)) continue;
      if (visible(c)) {
        out.openDialog = name(c);
        break;
      }
    }
  } catch (e) {
    /* :modal unsupported on this engine */
  }
  return out;
}
