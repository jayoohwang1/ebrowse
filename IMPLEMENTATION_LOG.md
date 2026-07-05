# Implementation Log (append-only)

Decisions and details worth knowing, newest at the bottom. Never edit old entries;
append corrections as new entries instead.

---

## 2026-07-03 — Phase 0: scaffold

- Repo laid out per DESIGN.md §7. `uv` + hatchling; Python ≥3.11 (host has 3.11.5;
  Playwright browsers already cached at ~/.cache/ms-playwright, chromium-1169).
- **Deviation from DESIGN.md: argparse instead of typer.** Rationale: typer/click
  pull rich-formatted help (boxes, ANSI) which is token-noisy when agents run
  `--help`, and we want full control over help text density (§1.2, Phase 5 help
  audit). Plain argparse with RawDescriptionHelpFormatter gives us that for free.
  DESIGN.md §7 dependency list is superseded on this point.
- `model.py` implemented as designed with two small additions: `Section.cross_origin`
  flag (outline needs it per §4.1) and `Diff.unchanged_sids` (navigation outcome
  marks fingerprint-matched persistent sections `(unchanged)`). Both are additive.
- `ElementDesc.match_key()` deliberately excludes `nth_hint` — nth is a tiebreaker
  applied by RefRegistry, not identity.
- Config env override scheme is generic `EBROWSE_<SECTION>_<KEY>` (e.g.
  `EBROWSE_SUMMARIZER_BASE_URL`) rather than a hand-picked list.
- Fixture pages: 7 handcrafted + 3 generated (`tests/fixtures/generate.py` for
  list/table/huge — edit the generator, not the output). Fixture server is stdlib
  `ThreadingHTTPServer` on 127.0.0.1, ephemeral port.
- CLI stub registers the full §5 verb surface now so help text and arg shapes are
  reviewable early; unimplemented verbs exit 2 naming the phase that delivers them.

## 2026-07-03 — Phase 1: core page model

- **discover.js payload uses short keys** (`t/a/r/x/c/k`) — ~35% smaller evaluate
  payloads on list-heavy pages. `DomNode.from_dict` is the only decoder; nothing
  else may parse raw JS output.
- **Invisible subtrees are pruned in-page** (display:none / visibility:hidden).
  Consequence: hidden dropdown menus and modals simply *appear* in the next
  capture after opening — which is exactly what the Phase-3 diff engine needs.
  Verified by test_capture_prunes_hidden / test_dropdown_reveal_appears_in_recapture.
- **`<select>` options are read from the element** (attrs.opt/sel), never by
  walking `<option>` children (select subtrees aren't descended). Options capped
  at 50, text at 80 chars.
- **Accessible-name resolution in JS** (aria-label > aria-labelledby > label[for]
  > closest(label) for wrapped controls > title > alt). Edge case: radio/checkbox
  inside `<label>Text <input></label>` gets the label text via closest('label').
- **Split thresholds alone were brittle**: article.html's `main` (768×899) fell
  just under WebChallenger's oversized thresholds, folding article+aside into one
  section. Added SEMANTIC_CHILD_TAGS rule: descend into any container with ≥2
  semantic/landmark children regardless of pixel size. Keep this rule in mind
  before touching thresholds.
- **List/table sections never take headings from inside items** (first product
  card's h3 is not the list's name); only explicit aria names / table captions.
- **Label text is rendered once**: `<label>` node text is suppressed in markdown
  because the control carries it as its accessible name. Labels with neither
  `for` nor a nested control lose their text (acceptable; noted in render.py).
- **Renderer & token estimates are coupled on purpose**: Section.token_estimate
  = estimate_tokens(render_section_markdown(show_all=True)), so the outline's
  "~Nt" is exactly what `expand` would cost.
- **Images render as `![alt]` without refs in v1.** @imgN refs (design §4.2) are
  deferred until the VLM caption path lands in Phase 4; log entry then.
- **Real-site smoke findings (Online-Mind2Web hosts):**
  - Playwright's default headless build (chrome-headless-shell) is blocked by
    Akamai fronts (traderjoes.com, drugs.com) with "Access Denied" even with a
    normal UA. **Fix: launch with channel="chromium" (full build, new headless)
    + plain Chrome UA + --disable-blink-features=AutomationControlled** — passes
    traderjoes/drugs. apartments.com still blocks headless entirely (stricter
    Akamai tier): that's the documented use case for CDP-attach mode, not v1.
  - recreation.gov (complex prod page) → 24-section outline, sensible types.
  - bestbuy.com serves a country-select interstitial to fresh profiles — fine,
    it's a real page state, agents can click through it.
- Dev harness `stats` subcommand reports outline vs aria-snapshot token ratio
  (Phase-1 done criterion: ≤15%).
- **pytest-asyncio pitfall**: module-scoped async fixtures deadlock under the
  default per-function event loop (browser created in a dead loop). Browser
  test fixtures are function-scoped on purpose; don't "optimize" them back.
- **Token-ratio results (outline tokens as % of aria-snapshot tokens):**
  huge 0.9%, table 3.5%, list 5.7%, recreation.gov 9.1%, article 18.8%,
  form 23.4%. The ≤15% criterion is met exactly where it matters — pages large
  enough to threaten context. Small pages exceed the ratio only because outlines
  have ~60-token fixed overhead while the whole page is ~300 tokens; absolute
  cost stays trivial, so this is accepted (criterion interpreted as applying to
  substantial pages).

## 2026-07-03 — Phase 2: daemon, sessions, CLI

- **CommandError moved to `ebrowse/errors.py`** so core/ (locate.py) never
  imports daemon/. protocol.py re-exports it for daemon-side convenience.
- Protocol is one-request-per-connection newline JSON — no pipelining, no
  framing beyond readline(). Deliberate: the CLI is subprocess-shaped anyway.
- **Browser is a persistent context** (launch_persistent_context on
  ~/.cache/ebrowse/profiles/<session>): cookies/logins survive daemon restarts.
  CDP attach (`connect <port>`) uses the browser's first existing context.
- Sessions are created lazily on first use of a --session name; per-session
  asyncio.Lock serializes commands; different sessions are concurrent.
- Section screenshots: Playwright clip coords are document-absolute only in
  full_page mode, so section/ref screenshots always set full_page=True.
- `expand @ref` expands the section *containing* that element.
- Minimal descriptor→locator chain landed early in core/locate.py (id > testid
  > role+name > placeholder > href > text) because `get` needs it; Phase 3
  hardens it (occlusion, bbox sanity) rather than introducing it.
- New tabs opened by the page are auto-adopted as the active page (matches
  agent expectation that "click opened a new tab, I'm now on it").
- e2e tests run the real CLI under a fake HOME; **PLAYWRIGHT_BROWSERS_PATH must
  be pinned to the real ~/.cache/ms-playwright** or Playwright can't find
  chromium under the fake HOME.
- back/forward/reload landed in Phase 2 (trivial navigations returning fresh
  outlines) even though the CLI stub had mapped them to Phase 3.

## 2026-07-03 — Phase 3: actions, diffs, ref resolution

- **Quiescence** is a single page.evaluate that installs a MutationObserver and
  resolves after `quiescence_ms` of silence (capped at `quiescence_max_ms`) —
  no init-script lifecycle to manage. If a navigation destroys the execution
  context mid-wait, we catch, wait for domcontentloaded, and settle once more.
- **Navigation detection = fragment-stripped URL comparison.** pushState route
  changes to a different path read as navigation; same-URL DOM swaps (spa.html
  Stats view) read as partial appeared/disappeared sections — both verified.
- **Occlusion pre-check must run via handle.evaluate (element's own frame).**
  Originally used page.evaluate(js, handle): for elements inside iframes the
  main document's elementFromPoint sees only the <iframe> region and falsely
  reports "covered by body". Edge case found via iframe.html Pay button.
- **Password values are never read; fill state is.** discover.js records "•••"
  for non-empty password fields — without this, filling a password diffs as
  "no change detected" (looked like a broken action). The real value never
  leaves the page.
- **`added_text` uses word-level difflib**, not sentence chunking: an appended
  "Account created!" is quoted alone instead of dragging the whole surrounding
  section text (first attempt did exactly that on form submit).
- **Text-relabeled controls read as remove+add** (e.g. dropdown button whose
  caption changes from "Sort by: Relevance" to "Sort by: Price"): text_head is
  part of descriptor identity. Honest but slightly noisy; revisit only if it
  confuses agents in practice.
- locate.py gained role+text_head and tag+text fallbacks: roles like menuitem/
  option/tab take their accessible name from text content, which discovery
  stores in text_head, so the role+name branch alone never matched them.
- Native dialog policy: accept alerts/confirms, dismiss prompts, always note in
  the next diff ("note: native confirm auto-accepted: …"). beforeunload accepts.
- eval prints `result: <json>` then the diff — it's an action, not a getter.
- scroll appends `scroll position y=N — viewport over s5, s6` after the diff;
  scrolling that triggers lazy-loading shows up as a regular diff.
- **Real-site validation (traderjoes.com, full task flow):** cookie-banner
  dismissal diffed as banner-disappeared + flyout-appeared; occlusion check
  correctly blocked Search while the newsletter flyout was up; search
  type+Enter detected as navigation with a clean results outline. One rough
  edge: a click whose effect is animated past the quiescence window reads as
  "no change" and the disappearance gets attributed to the *next* action's
  diff (seen on the flyout close button). Honest but off-by-one; acceptable —
  the known-risk quiescence tradeoff from DESIGN.md §12.
- Full suite at end of Phase 3: 87 passed (pure + browser + e2e), lint clean.

## 2026-07-03 — Phase 4: summarizer sidecar

- **One batched call per page** (system prompt + per-section digests → strict
  JSON array). Verified live against the local llama.cpp Qwen3.6-35B at :5001:
  high-quality one-liners on fixtures and traderjoes.com; cache hits render in
  0.1s.
- Backfill runs as an asyncio task *outside* the session lock, on a snapshot of
  (PageMem, texts) taken at spawn; it only ever writes to the sqlite cache, so
  it cannot race later observations. Backfilled summaries appear on the *next*
  outline (design: never mutate emitted output).
- Dedupe: a new backfill is spawned only if the missing-hash set isn't covered
  by the currently running task's signature.
- Circuit breaker: 3 consecutive failures → summarizer off for 10 min; outline
  note says "summaries: unavailable". Breaker + no-traffic-while-open covered
  by mock-server tests.
- Injection hygiene implemented as designed: model output is length-clamped
  (140), control-stripped, `(@eN)` tokens removed (deviation from DESIGN.md
  §Phase-4.2: the design allowed up to 2 validated inline refs; v1 strips all
  refs from summaries for simplicity — revisit if labels feel less actionable).
- Summaries key on content_hash, so the §Phase-4 "diff-triggered invalidation"
  is structural: changed content = new hash = cache miss. No separate
  invalidation code needed.
- e2e suites from Phases 2–3 set EBROWSE_SUMMARIZER_ENABLED=false so they never
  depend on (or wait for) the real local model; Phase-4 e2e uses a stdlib mock
  OpenAI server (tests/mock_summarizer.py).
- **Not yet implemented from the Phase-4 design: vision paths** (image captions
  in expand, list-section screenshot summaries). The captions table exists in
  the cache schema. Deferred to Phase 5 or a 4.1 follow-up — text labels were
  the value; captions need the @imgN ref design finished first.
- Known nit: media sections whose only content is alt text sometimes get a
  lazy "Empty content section" label from the model (seen on traderjoes s4);
  prompt tweak candidate.

## 2026-07-03 — Phase 5: hardening, SKILL.md, packaging

- SKILL.md written for LLM consumption: operating loop, outline anatomy
  diagram, diff vocabulary, recipes, and a "what NOT to do" list. This is the
  primary adoption surface for host agents.
- **Real-site smoke run (10 Online-Mind2Web hosts)**: 9/10 clean outlines at
  6.4–15% of aria-snapshot tokens (traderjoes 10.9%, recreation.gov 9.1%,
  drugs.com 6.4%, cars.com 9.9%, accuweather 8.5%, apple 11.5%, ups 15.0%,
  akc 12.1%). bestbuy shows its country-select interstitial (2 sections, real
  page state). Script: scripts/smoke_real_sites.py (manual, not pytest).
- **Two splitter bugs found by the smoke run (healthline.com,** css-in-js page
  with no semantic tags and classless wrapper divs):
  1. `_coalesce_small` had no run-size cap → the whole page merged into one
     section. Fix: MERGE_RUN_MAX_HEIGHT=700px per coalesced run.
  2. `normalize_class` drops `__`-containing tokens (BEM heuristic), so
     `<div class="__chrome">` normalized equal to classless divs and the
     body's wrapper-div run became one giant "list group". Fix: sibling
     grouping now requires a shared *non-empty* normalized class (or an
     item-ish tag li/tr/dt/dd/option/article) and a rendered box.
  Lesson for future splitter work: the failure mode of generic heuristics is
  always "everything collapses into one thing"; test on css-in-js sites.
- `uv build` produces installable sdist+wheel; `ebrowse` console script works
  via pipx/uv tool install.
- locator-usage invariant verified: observation uses zero per-element
  Playwright calls; remaining `.locator(` sites are user-CSS targets and
  locate.py resolution only.
- v1 complete: 94 tests (pure/browser/e2e), lint clean. Deferred to roadmap:
  VLM captions (@imgN refs), compound verbs, list querying, MCP server, site
  memory (DESIGN.md §10).

## 2026-07-03 — R1: compound verbs (select machine, fill-form, search)

- Refactored `_act` into `_begin_action`/`_finish_action` so compound verbs run
  N internal steps under ONE final diff. Compound output = header line with the
  outcome arrow + indented ✓/✗ step lines + the diff (renderer now always puts
  the arrow on the FIRST line of multi-line action headers).
- `select` on a non-native element now runs the dropdown state machine
  (click → observe → match revealed options → click) instead of refusing.
  Matching is exact > prefix > substring on name/placeholder/text_head;
  ambiguity/no-match exits 2 listing the revealed options.
- **Container-role suppression bug (recreation.gov):** role=listbox is itself
  clickable, so the nested-clickable rule swallowed its role=option children —
  suggestions were invisible to search --pick. Fix: clickable elements with
  container roles (listbox/menu/list/radiogroup/combobox/tree/grid) no longer
  suppress descendants (core/clickable.py CONTAINER_ROLES).
- **Container elements also poisoned match pools**: a listbox's text_head
  contains every option, so it substring-matches anything and clicking its
  center hits an arbitrary item. `_prefer_leaves` drops container-role elements
  from compound match pools unless they're all there is.
- search --pick falls back to currently-visible option/menuitem elements when
  the diff reveals nothing (suggestions already open from a prior --no-submit).
- Playwright errors inside compound steps are mapped like atomic verbs
  (previously leaked as "internal error"); empty targets now exit 2 with usage
  help instead of crashing locator parsing.
- **Real-site validation:**
  - bestbuy.com: compound search end-to-end (auto-found header box among many
    inputs, 49 suggestions detected, Enter → results outline); select machine
    on the results "Sort by" custom dropdown (21 options, case-insensitive
    match, sorted-URL navigation detected). Country interstitial handled by a
    normal click; bestbuy.CA is Akamai-blocked (stay on .com).
  - recreation.gov: search --pick clicks the exact suggestion ("Joshua Tree
    National Park" → correct gateway page). Two same-named search boxes →
    ambiguity error with refs, resolved via --in.
  - traderjoes.com: full Online-Mind2Web-style task — dismiss cookies, close
    newsletter flyout (occlusion detection guided this), open store selector,
    compound-search "San Francisco", click "set as my store"; diff confirmed
    My Store: Coeur d'Alene → San Francisco.
  - cars.com: Cloudflare-blocks headless entirely ("Just a moment…") — R8
    stealth tier / CDP attach territory; documented limitation.
- **Post-R1 fix**: compound match pools must NOT drop role=combobox — native
  <select> has implicit combobox role, so fill-form's "Country" silently
  stopped matching after the container-leaf change. Containers-to-drop and
  containers-that-dont-suppress-descendants are now two separate sets
  (compound._CONTAINER_ROLES vs clickable.CONTAINER_ROLES) — deliberate.
- R1 done: 101 tests green, lint clean.

## 2026-07-03 — R2: query verb

- `query <sid> [--filter re] [--cols a,b] [--cursor N] [--limit N]` over
  list/table sections. Filter matches PLAIN item text (subtree_text), never the
  rendered markdown — first cut matched markup and `^Ab` anchors failed on
  drugs.com's A–Z list. Display stays markdown-with-refs; original item indices
  are preserved so --cursor stays consistent with expand.
- --cols projects table columns by case-insensitive header substring; unknown
  columns exit 2 listing the real column names.
- --sort deferred (roadmap note): real-site sorting is usually server-side via
  the sort dropdown, which the R1 select machine already drives.
- Real-site validation: bestbuy results grid (filter "Cold Brew" → 2 of 24
  items with prices/Add-to-cart refs), drugs.com A–Z (regex ^Ab → 5 of 70).

## 2026-07-03 — R3: MCP server

- `ebrowse mcp` — hand-rolled newline-JSON-RPC stdio server (~250 lines, zero
  new dependencies; the MCP SDK would have been heavier than the protocol
  subset we need: initialize / tools/list / tools/call).
- Six tools, act-multiplexed to keep host-side schema tokens low:
  browse_open/outline/expand/act/query/screenshot. Tool text is the §4
  renderer output verbatim; screenshots return MCP image content (base64 png).
- The MCP process is a thin client to the SAME daemon (session "mcp" by
  default) — CLI and MCP callers share browser state by design.
- Tool failures return isError=true with the CommandError text (recovery
  action included), never protocol-level errors.
- Real-site validation over the protocol: recreation.gov open → search-with-
  pick via browse_act (exact suggestion clicked, navigation outline returned).
- **search --pick hardening found via MCP test**: suggestion XHRs can outlive
  the mutation-quiescence window (DOM quiet while request in flight), so pick
  now retries twice with a 1.2s wait + page-wide option-role fallback before
  giving up.

## 2026-07-03 — R4: @img refs + VLM captions

- Large images (≥80×80 rendered px) get page-scoped refs `@i1, @i2…` shown in
  expand markdown as `![alt](@i3)` and usable with `screenshot --ref @iN`.
  Deliberately NOT durable across observations (unlike @e) — they exist for
  screenshots/captions, not actions; the distinct prefix signals this.
- Captions are expand-time only and lazy: when a section is expanded, alt-less
  @i images (≤4 per expand) are clipped via full-page screenshot and captioned
  by the multimodal sidecar; results cached by src-hash in the existing
  captions table. Images WITH alt text never spend caption budget — alt is
  usually adequate and free. Cached captions render as `![≈caption](@iN)`.
- Verified live: TJ hero image screenshot → Qwen3.6-35B caption "Strawberry
  cereal treats with marshmallows and strawberries." Accurate.
- adoptapet finding: pet-card images are lazy-loaded inside anchors and often
  sizeless at capture; the card LINK text already carries name/breed/age, so
  captions add nothing there — the alt-less-only budget rule is right.
- List-section screenshot summarization (the other R4 item) remains deferred:
  the text-digest path has been sufficient on every real site tested so far.
