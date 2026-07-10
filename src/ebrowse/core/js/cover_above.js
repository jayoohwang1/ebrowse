// cover_above.js — parent-document probe for targets inside iframes.
//
// The in-frame occlusion/diagnosis probes cannot see a parent-page banner or
// modal sitting ABOVE the iframe. This runs against the iframe element in its
// parent document, hit-testing the target's viewport point [cx, cy] there.
// Returns:
//   covering: display name of a parent-document element covering the point
//             (anything that is not the iframe chain itself)
//   coverDialog: the cover's dialog ancestor, if any
//   chain:    the cover's ancestor chain (identifying attrs, for ref matching)
//   inside:   up to 5 interactive-looking DESCENDANTS of the cover — a consent
//             bar's own OK button is usually the recovery action
//   modal:    a visible modal elsewhere in the parent document
(fr, [cx, cy]) => {
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
  const ident = (n) => ({
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

  const out = { covering: null, coverDialog: null, chain: [], inside: [], modal: null };
  if (cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight) {
    const t = document.elementFromPoint(cx, cy);
    if (t && !within(fr, t) && !within(t, fr)) {
      out.covering = name(t);
      const dlg = t.closest("dialog,[role=dialog],[role=alertdialog]");
      if (dlg) out.coverDialog = name(dlg);
      let n = t;
      for (let i = 0; n && n !== document.documentElement && i < 12; i++) {
        out.chain.push(ident(n));
        n = n.parentElement || (n.getRootNode() instanceof ShadowRoot ? n.getRootNode().host : null);
      }
      try {
        const controls = t.querySelectorAll("button, a[href], input, [role=button], [onclick]");
        for (const c of controls) {
          out.inside.push(ident(c));
          if (out.inside.length >= 5) break;
        }
      } catch (e) { /* selector unsupported */ }
    }
  }
  try {
    for (const c of document.querySelectorAll(':modal,[aria-modal="true"]')) {
      const cr = c.getBoundingClientRect();
      if (cr.width > 0 && cr.height > 0 && getComputedStyle(c).visibility !== "hidden") {
        out.modal = name(c);
        break;
      }
    }
  } catch (e) { /* :modal unsupported */ }
  return out;
}
