// discover.js — single-pass DOM walk returning a JSON tree.
//
// This is the ONLY code that runs inside the page. It must stay:
//  - read-only (never mutate the DOM),
//  - fast (one getComputedStyle per element, no layout thrash beyond rects),
//  - compact (short keys; prune hidden subtrees and boilerplate tags).
//
// Node shape (short keys to keep the evaluate payload small):
//   t: tag (lowercase)          a: curated attrs (only when present)
//   r: [x, y, w, h] absolute page CSS px (rounded)
//   x: own text (direct text-node children, whitespace-collapsed)
//   c: children                 k: clickable signals (only when any):
//                                  strong {tg,rl,ls,cp} | weak candidate {el,tb,as}
//   sh: 1 if children include a shadow root's content
//
// Attr keys in `a`: id, cls, role, nm (resolved accessible name), tid (testid),
// href, ph (placeholder), typ (input type), val, chk, dis, exp (aria-expanded),
// pop (aria-haspopup), alt, src, ttl (title attr), lbl (label[for] text),
// opt (select option texts), sel (selected option text), req, con (contenteditable)
//
// The clickable-signal sets below are templated in by Python from
// ebrowse/core/clickable.py — single source of truth. Placeholders:
// __CLICKABLE_TAGS__ __CLICKABLE_ROLES__ __LISTENER_ATTRS__ __SKIP_TAGS__

() => {
  const CLICKABLE_TAGS = new Set(__CLICKABLE_TAGS__);
  const CLICKABLE_ROLES = new Set(__CLICKABLE_ROLES__);
  const LISTENER_ATTRS = __LISTENER_ATTRS__;
  const SKIP_TAGS = new Set(__SKIP_TAGS__);
  const CANDIDATE_ARIA_ATTRS = __CANDIDATE_ARIA_ATTRS__;
  const CANDIDATE_LISTENER_TYPES = __CANDIDATE_LISTENER_TYPES__;
  const MAX_NODES = 15000;
  const TEXT_CAP = 4000;

  // getEventListeners is the devtools command-line API: present only when this
  // script runs via CDP Runtime.evaluate with includeCommandLineAPI (main
  // frame; see core/snapshot.py). Absent under plain evaluate — degrade to
  // no `el` signals rather than fail.
  const canSniffListeners = typeof getEventListeners === "function";
  function hasPointerListener(el) {
    if (!canSniffListeners) return false;
    try {
      const ls = getEventListeners(el);
      for (const t of CANDIDATE_LISTENER_TYPES) if (ls[t] && ls[t].length) return true;
    } catch (e) { /* cross-origin node or API hiccup */ }
    return false;
  }

  let nodeCount = 0;
  let truncated = false;

  // label[for] map, built once
  const labelFor = {};
  for (const lab of document.querySelectorAll("label[for]")) {
    const t = (lab.textContent || "").replace(/\s+/g, " ").trim();
    if (t) labelFor[lab.getAttribute("for")] = t.slice(0, 120);
  }

  const collapse = (s) => (s || "").replace(/\s+/g, " ").trim();

  function accName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return collapse(aria).slice(0, 120);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const parts = [];
      for (const id of lb.split(/\s+/)) {
        const ref = document.getElementById(id);
        if (ref) parts.push(collapse(ref.textContent));
      }
      const joined = parts.join(" ").trim();
      if (joined) return joined.slice(0, 120);
    }
    if (el.id && labelFor[el.id]) return labelFor[el.id];
    // wrapped-label pattern: <label>Text <input></label> (common for radios/checkboxes)
    const tag = el.tagName.toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") {
      const wrap = el.closest("label");
      if (wrap) {
        const t = collapse(wrap.textContent);
        if (t) return t.slice(0, 120);
      }
    }
    const title = el.getAttribute("title");
    if (title) return collapse(title).slice(0, 120);
    const alt = el.getAttribute("alt");
    if (alt) return collapse(alt).slice(0, 120);
    return null;
  }

  function curatedAttrs(el, tag) {
    const a = {};
    if (el.id) a.id = el.id;
    const cls = el.getAttribute("class");
    if (cls) a.cls = collapse(cls).slice(0, 200);
    const role = el.getAttribute("role");
    if (role) a.role = role;
    const nm = accName(el);
    if (nm) a.nm = nm;
    const tid =
      el.getAttribute("data-testid") || el.getAttribute("data-qa") || el.getAttribute("data-test");
    if (tid) a.tid = tid;
    const href = el.getAttribute("href");
    if (href) a.href = href.slice(0, 500);
    const ph = el.getAttribute("placeholder");
    if (ph) a.ph = ph;
    const ttl = el.getAttribute("title");
    if (ttl) a.ttl = collapse(ttl).slice(0, 120);
    if (el.hasAttribute("contenteditable") && el.getAttribute("contenteditable") !== "false")
      a.con = 1;
    if (tag === "label" && el.hasAttribute("for")) a.for = 1;
    const exp = el.getAttribute("aria-expanded");
    if (exp !== null) a.exp = exp === "true" ? 1 : 0;
    const pop = el.getAttribute("aria-haspopup");
    if (pop && pop !== "false") a.pop = pop;
    // ARIA state evidence: toggles/tabs/options whose ONLY reaction to a
    // click is a state flip would otherwise diff as "no change"
    const prs = el.getAttribute("aria-pressed");
    if (prs !== null) a.prs = prs === "true" ? 1 : 0;
    const asel = el.getAttribute("aria-selected");
    if (asel !== null) a.asel = asel === "true" ? 1 : 0;
    if (role && ["checkbox", "radio", "switch", "menuitemcheckbox",
                 "menuitemradio"].includes(role)) {
      const ac = el.getAttribute("aria-checked");
      if (ac !== null) a.chk = ac === "true" ? 1 : 0;
    }
    // :disabled matches inherited fieldset disabling too, unlike el.disabled
    let dis = el.getAttribute("aria-disabled") === "true";
    if (!dis) {
      try { dis = el.matches(":disabled"); } catch (e) { dis = el.disabled === true; }
    }
    if (dis) a.dis = 1;

    if (tag === "input" || tag === "textarea") {
      a.typ = tag === "textarea" ? "textarea" : (el.getAttribute("type") || "text").toLowerCase();
      if (a.typ === "checkbox" || a.typ === "radio") {
        a.chk = el.checked ? 1 : 0;
      } else if (a.typ === "password") {
        // never read the actual secret, but DO record fill state — otherwise
        // filling a password field diffs as "no change" (a confusing no-op)
        if (el.value) a.val = "•••";
      } else if (typeof el.value === "string" && el.value) {
        a.val = el.value.slice(0, 200);
      }
      if (el.required) a.req = 1;
    } else if (tag === "select") {
      const opts = [];
      for (const o of el.options) {
        opts.push(collapse(o.text).slice(0, 80));
        if (opts.length >= 50) break;
      }
      a.opt = opts;
      // honest total when the list is truncated ("of 80 options", not "of 50")
      if (el.options.length > opts.length) a.optn = el.options.length;
      if (el.multiple) a.mul = 1;
      const chosen = [];
      for (const o of el.selectedOptions) {
        chosen.push(collapse(o.text).slice(0, 80));
        if (chosen.length >= 5) break;
      }
      if (chosen.length) a.sel = chosen.join(", ");
    } else if (tag === "img") {
      const alt = el.getAttribute("alt");
      if (alt) a.alt = collapse(alt).slice(0, 160);
      const src = el.currentSrc || el.getAttribute("src") || "";
      if (src) a.src = src.slice(0, 300);
    } else if (tag === "iframe") {
      // frame identity for stitching + locator resolution when id/title absent
      const src = el.getAttribute("src");
      if (src) a.src = src.slice(0, 300);
    }
    return a;
  }

  function walk(el, parentCursorPointer) {
    if (truncated) return null;
    const tag = el.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag)) return null;

    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return null;

    if (++nodeCount > MAX_NODES) {
      truncated = true;
      return null;
    }

    const rect = el.getBoundingClientRect();
    const r = [
      Math.round(rect.left + window.scrollX),
      Math.round(rect.top + window.scrollY),
      Math.round(rect.width),
      Math.round(rect.height),
    ];

    const node = { t: tag, r };
    const a = curatedAttrs(el, tag);
    // real inner scroll container (body/html scroll via the window instead):
    // scr = [scrollTop, maxScrollTop] so outlines can say "more below the fold"
    if (tag !== "body" && tag !== "html") {
      const oy = style.overflowY;
      if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 4) {
        a.scr = [Math.round(el.scrollTop), Math.round(el.scrollHeight - el.clientHeight)];
      }
    }
    if (Object.keys(a).length) node.a = a;

    // clickable signals (strong tier: tg/rl/ls/cp)
    const cursorPointer = style.cursor === "pointer";
    const k = {};
    if (CLICKABLE_TAGS.has(tag)) k.tg = 1;
    const role = el.getAttribute("role");
    if (role && CLICKABLE_ROLES.has(role)) k.rl = 1;
    for (const la of LISTENER_ATTRS) {
      if (el.hasAttribute(la)) {
        k.ls = 1;
        break;
      }
    }
    if (cursorPointer && !parentCursorPointer) k.cp = 1;
    if (a.con) k.tg = 1; // contenteditable acts as an input
    // candidate signals (weak tier: el/tb/as) — only sniffed when no strong
    // signal exists; they yield expand-only '?' refs, never default behavior
    if (!Object.keys(k).length) {
      if (el.hasAttribute("tabindex") && (parseInt(el.getAttribute("tabindex"), 10) || 0) >= 0) {
        k.tb = 1;
      }
      for (const aa of CANDIDATE_ARIA_ATTRS) {
        if (el.hasAttribute(aa)) {
          k.as = 1;
          break;
        }
      }
      if (el.draggable === true && tag !== "img" && tag !== "a") k.dg = 1;
      if (hasPointerListener(el)) k.el = 1;
    }
    if (Object.keys(k).length) {
      node.k = k;
      // effective-state annotation for interactive nodes only: an element
      // under [inert] renders normally but can never be interacted with
      if (el.closest("[inert]")) {
        if (!node.a) node.a = a;
        a.inr = 1;
      }
    }

    // own text (direct text-node children only)
    let own = "";
    for (const child of el.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) own += child.textContent;
    }
    own = collapse(own);
    if (own) node.x = own.slice(0, TEXT_CAP);

    // children: svg and iframe are leaves; shadow roots are inlined
    if (tag === "svg" || tag === "iframe" || tag === "select") return node;
    const kids = [];
    if (el.shadowRoot) {
      node.sh = 1;
      for (const child of el.shadowRoot.children) {
        const cn = walk(child, cursorPointer);
        if (cn) kids.push(cn);
      }
    }
    for (const child of el.children) {
      const cn = walk(child, cursorPointer);
      if (cn) kids.push(cn);
    }
    if (kids.length) node.c = kids;
    return node;
  }

  const root = walk(document.body, false) || { t: "body", r: [0, 0, 0, 0] };
  return {
    url: location.href,
    title: document.title,
    vw: window.innerWidth,
    vh: window.innerHeight,
    scrollY: Math.round(window.scrollY),
    docH: Math.round(Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)),
    truncated,
    root,
  };
}
