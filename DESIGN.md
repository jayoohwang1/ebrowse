# ebrowse — Token-Efficient Browser Control for Agents

**Design document & implementation plan.** Status: approved design, pre-implementation.

`ebrowse` is a browser-control CLI (daemon-backed, Python + async Playwright) that any
agent harness can drive — via Bash from Claude Code, as wrapped tools from Hermes-style
harnesses, or later through an MCP wrapper. It adapts the reusable ideas from
**WebChallenger** (PageMem sectioning, diff-based observation, deterministic site
structure analysis) into a general-purpose tool shaped like the `agent-browser` CLI,
while fixing the dominant token pathologies of current browser tools:

1. **Full-page snapshot per step** — the whole accessibility tree lands in context on
   every action, and accumulates in history.
2. **No change observation** — after an action the agent must mentally diff two full
   snapshots to learn what happened, or misses that *nothing* happened.
3. **One round trip per micro-interaction** — forms and menus cost a full
   observe/decide cycle per field.
4. **Session amnesia and stale refs** — refs are snapshot-ordinal and die on
   re-snapshot; nothing persists across visits.

### How to use this document

This doc is written so separate agents can implement phases independently and
cohesively. Read **§1 Design Principles** and **§4 Output Contracts** before writing
any code — they are the coherence spine. Each phase in **§8** lists its deliverables,
the interfaces it must honor, reference material, and done criteria. Interfaces marked
**FROZEN** may only change with a doc update in the same commit.

Reference codebases (read-only):
- WebChallenger: `~/code/pytorch_docker/Web_Nav/wbs_storage/webchallenger/` —
  algorithms to adapt (see §9 adaptation map). Do **not** copy code verbatim; the
  implementation is a sync-Playwright god-object. Port the *ideas*.
- agent-browser (vendored at `./agent-browser/`, reference only, not a dependency) —
  CLI conventions to align with (§5).
- WebChallenger paper source: `~/code/jayoo_personal/paper_source/neurips_2026.tex` —
  appendix has the DividePage/UpdatePageMem algorithms and the clickable predicate.

---

## 1. Design Principles

These are binding on all phases.

1. **Determinism first; LLM as optional enhancement.** Every feature must work with no
   LLM configured. The summarizer sidecar only ever produces *navigational hints*
   (section labels, image captions); everything the host agent acts on — refs, element
   states, expanded content, diffs — is ground truth derived from the DOM. An LLM
   failure degrades label quality, never correctness.
2. **Token economy is a feature.** Every line of default output earns its place. The
   host model's context is the scarce resource; prefer one dense line over three sparse
   ones. All output formats have golden tests so token regressions are visible diffs.
3. **Core is pure.** The `core/` package operates on plain data (`DomSnapshot` in,
   `PageMem`/`Diff`/rendered text out) with **no** Playwright, daemon, or network
   dependency. All page inspection happens in a single injected-JS pass that returns
   JSON. This makes the interesting logic unit-testable from JSON fixtures and keeps
   browser round trips O(1) per observation instead of O(elements).
4. **The data model is the stable interface.** Phases communicate through
   `model.py` dataclasses and the renderers. Extend by adding optional fields; never
   repurpose existing ones.
5. **No site-specific logic.** Heuristics must be justified by DOM/ARIA semantics or
   generic visual structure, never by a particular website. (WebChallenger holds this
   line and it is why it generalizes.)
6. **Sensible defaults, escape hatches, options later.** Default behavior needs zero
   flags. Complex behavior (custom quiescence, raw output, JSON) is opt-in. When in
   doubt, do the simple thing and leave a config key documented as "future."
7. **Every phase ends with a working tool.** No phase may leave `ebrowse` in a state
   where previously working verbs break.
8. **Fail loud and actionable.** Every error message states what failed and the next
   command the agent should try (e.g. stale ref → "re-run `ebrowse outline`").

---

## 2. Architecture Overview

```
┌────────────┐  argv    ┌──────────────────────── daemon (asyncio) ───────────────────────┐
│ ebrowse CLI├─────────▶│ unix-socket JSON-RPC (protocol.py)                               │
│ (thin)     │◀─────────│   └─ SessionManager ── Session("default")                        │
└────────────┘  text/   │        ├─ BrowserHandle (Playwright launch │ CDP attach)         │
                json    │        ├─ PageState: PageMem, ref registry, last outline         │
                        │        ├─ actions.py: verb impl → quiescence → diff              │
                        │        └─ summarize/: batch labels, captions (async backfill)    │
                        │  core/ (pure): discover.js → DomSnapshot → split/label/          │
                        │                fingerprint/diff/render                           │
                        └──────────────────────────────────────────────────────────────────┘
```

- **CLI** (`cli/`): parses argv, autostarts the daemon if absent, sends one JSON-RPC
  request, prints the response. No logic.
- **Daemon** (`daemon/`): one process per user, owns Playwright, serves N named
  sessions. Commands within a session execute serially (per-session asyncio lock).
  Idle shutdown (default 30 min without commands).
- **Session**: one browser context + one "active page" (tab) + observation state.
- **core/**: pure functions. The only code that understands page *structure*.
- **summarize/**: optional client for an OpenAI-compatible endpoint (default: the
  local llama.cpp Qwen3.6-35B server at `http://127.0.0.1:5001/v1`, multimodal,
  131k ctx). Never on the critical path.

### Observation flow (the heart of the tool)

```
ebrowse outline
  → page.evaluate(discover.js)          # ONE round trip: full DOM walk in-page
  → DomSnapshot (JSON tree)             # nodes: tag/attrs/text/bbox/clickable signals
  → split(DomSnapshot) → [Section]      # WebChallenger DividePage adaptation
  → fingerprint + match vs prior PageMem → stable sids, reused refs, per-section diffs
  → label(Section) deterministic; summary cache lookup by content_hash
  → render_outline(PageMem) → stdout
  → (background) summarize.batch(page) → cache   # if summarizer enabled
```

```
ebrowse click @e12
  → resolve @e12 → ElementDesc → locator chain → occlusion check → click
  → wait_quiescence()                   # load-state + rAF settle + debounce
  → re-discover → new PageMem → diff(prev, new)
  → render_diff(...) → stdout           # NOT a full snapshot
```

---

## 3. Data Model (`model.py`) — **FROZEN after Phase 1**

All dataclasses are `slots=True`, JSON-serializable via `to_dict()/from_dict()`.
Representative fields; implementers may add optional fields.

```python
@dataclass
class ElementDesc:
    """Durable identity of an interactive element. Basis for refs and locators."""
    tag: str
    role: str | None            # ARIA role (explicit or implicit)
    id: str | None
    testid: str | None          # data-testid / data-qa / data-test
    name: str | None            # accessible name (aria-label > label > title > alt)
    placeholder: str | None
    href: str | None            # normalized: path+query, origin stripped
    input_type: str | None      # for <input>
    text_head: str              # first 80 chars of visible text, whitespace-collapsed
    nth_hint: int               # disambiguator among identical descriptors on a page
    iframe_path: tuple[str, ...]  # () for main frame; ids/titles of ancestor frames

@dataclass
class ElementState:
    """Volatile state, refreshed every observation."""
    value: str | None
    checked: bool | None
    disabled: bool
    expanded: bool | None       # aria-expanded
    visible: bool
    bbox: BBox
    options: list[str] | None   # native <select> only

@dataclass
class Element:
    ref: str                    # "@e12" — session-scoped, durable (see §3.1)
    desc: ElementDesc
    state: ElementState

SectionType = Literal["nav", "header", "footer", "form", "list", "table",
                      "dialog", "content", "media", "iframe"]

@dataclass
class Section:
    sid: str                    # "s1".. document order on current page
    fingerprint: str            # stable identity across mutations/revisits (§3.2)
    type: SectionType
    heading: str | None         # nearest heading/landmark text (deterministic)
    preview: str                # first N chars of text content (config, default 120)
    summary: str | None         # LLM one-liner; None until backfilled. Provenance: LLM.
    elements: list[Element]
    item_count: int | None      # list/table sections: number of items/rows
    content_hash: str           # hash of normalized text+element descs (summary cache key)
    token_estimate: int         # len(rendered_markdown)//4
    bbox: BBox
    iframe_path: tuple[str, ...]

@dataclass
class PageMem:
    url: str
    title: str
    sections: list[Section]
    captured_at: float
    nav_id: int                 # increments on navigation; scopes sids

@dataclass
class SectionDiff:
    sid: str
    kind: Literal["appeared", "disappeared", "changed"]
    added: list[Element]
    removed: list[ElementDesc]
    state_changes: list[tuple[str, str, str, str]]  # (ref, field, old, new)

@dataclass
class Diff:
    kind: Literal["no_change", "partial", "navigation", "dialog"]
    sections: list[SectionDiff]         # for "partial"/"dialog"
    new_page: PageMem | None            # for "navigation"
    notes: list[str]                    # e.g. "native alert auto-dismissed: '...'"
```

### 3.1 Ref semantics

- Refs are **session-scoped and monotonic**: `@e1, @e2, …` assigned on first sight,
  never reused for a different element. The session keeps a registry
  `ref → ElementDesc`.
- On every observation, discovered elements are **matched against the registry** by
  descriptor equality (tie-broken by `nth_hint`, then bbox proximity). Matched
  elements keep their refs — across re-snapshots, DOM mutations, *and page
  navigations*. A site's persistent chrome (header search box, nav links) keeps the
  same refs on every page, which lets host agents build cheap habits ("@e3 is always
  the search box").
- Acting on a ref that no longer resolves fails fast with:
  `stale ref @e12 (button "Add to cart"): not found on current page — run 'ebrowse outline'`.
- Elements are *also* addressable by CSS selector in every action verb
  (agent-browser parity, useful escape hatch). Refs are the recommended path.

### 3.2 Section identity

- `sid` (s1…sN) is positional *within the current page* and restarts each navigation
  (scoped by `nav_id`; the outline header shows the URL so there's no ambiguity).
- `fingerprint` provides identity across mutations and revisits:
  `hash(tag, normalized_class, landmark_role, heading_text, iframe_path,
  parent_chain_shape)`. Class normalization strips utility/state suffixes
  (WebChallenger `element_class_str` is the reference).
- Diffing and summary caching key on fingerprint + content_hash, never on sid.

---

## 4. Output Contracts — **FROZEN after Phase 3 (outline/expand after Phase 1)**

Default output is compact plaintext for LLM consumption. `--json` on any verb emits
the underlying dataclasses instead. Golden tests pin every format.

### 4.1 Outline (`ebrowse outline`)

```
PAGE Amazon.com : sony headphones — https://www.amazon.com/s?k=sony+headphones
s1 nav     12 links, 2 inputs   ~800t  ≈ Site header: search box (@e2), account menu, cart
s2 form    18 inputs            ~450t  ≈ Filter sidebar: brand, price, rating checkboxes
s3 list    24 items, ~48 links  ~6.2kt ≈ Search results: product cards, title/price/rating
s4 nav     7 links              ~150t  | "Pagination — 1 2 3 … Next"
s5 iframe  (cross-origin: ads.doubleclick.net)
summaries: 3/4 cached · backfill running
```

Format rules:
- One line per section: `sid type  <counts>  ~<tokens>  <label>`.
- Label provenance markers: `≈` prefix = LLM summary (model-paraphrased page content,
  untrusted); `|` prefix = deterministic (heading + preview, quoted verbatim).
- Prominent single elements may be inlined into labels by the summarizer prompt
  (as `(@ref)`) but only refs that exist.
- Cross-origin iframes are listed but not entered (v1 limitation, stated inline).
- Final status line only when summarizer enabled and not fully cached.

### 4.2 Expand (`ebrowse expand s2`, `ebrowse expand s3 --cursor 24`)

Markdown rendering of the section's full content with inline refs — **not** an
accessibility tree. Headings/text/links as normal markdown; interactive elements
annotated inline:

```
## s2 form — Filter sidebar
### Brand
- [ ] Sony (@e31) · [ ] Bose (@e32) · [x] JBL (@e33)
### Price
[min (@e34: "")] — [max (@e35: "")]  [Go (@e36)]
### Customer Reviews
[4 stars & up (@e37)](link)
```

Rules:
- Links: `[text (@ref)](→ /path)` — href shown path-only, origin stripped.
- Inputs: `[label (@ref: "current value")]`; checkboxes `[x]/[ ]`; native selects
  `[label (@ref) ▾ selected: "US" of 24 options]` (full option list included when
  ≤ 15 options, else count + `expand @ref` hint).
- Images: `![alt or ≈caption](@img4)` — captions only if VLM enabled and cached.
- List/table sections paginate: default first 20 items + 
  `… 104 more items — expand s3 --cursor 20`. Tables render as markdown tables
  with a header row; per-row action elements get refs.
- Oversized non-list sections (rare) paginate the same way.

### 4.3 Action result (every action verb)

```
CLICK @e42 (button "Add to Cart") → partial change
s7 dialog appeared  3 links, 1 button  ~200t  | "Added to cart — Sprite Stasis Ball"
~ @e12 cart badge: "0" → "1"
```

```
CLICK @e17 (link "Reviews") → navigation
PAGE Product Reviews — https://…/reviews
s1 nav     12 links, 2 inputs   ~800t  ≈ Site header (unchanged)
s2 list    30 items             ~4.1kt | "Customer reviews, sorted by Top"
…
```

```
TYPE @e2 "sony headphones" → partial change
~ @e2 value: "" → "sony headphones"
s8 list appeared  8 items  ~300t  | "Search suggestions"
```

```
CLICK @e9 (button "Save") → no change detected
(page DOM and URL unchanged after 1.2s — the click may have been a no-op,
 or its effect is outside the DOM. Check `ebrowse outline` or screenshot.)
```

Rules:
- First line: `VERB target (resolved description) → outcome`.
- `navigation` outcomes print the *new outline* (the agent needs it anyway; one round
  trip saved). Unchanged persistent sections matched by fingerprint are marked
  `(unchanged)` and reuse cached summaries.
- `partial` outcomes print only the diff, sorted: appeared sections, then
  disappeared, then state changes. Appeared sections use outline line format.
- Dialog/alert notes always surface: `note: native confirm auto-accepted ("Delete?")`.
- Occluded clicks fail before acting:
  `blocked: @e42 is covered by s9 dialog ("Cookie consent") — interact with s9 first`.

### 4.4 Errors

Single line, prefixed `error:`, always naming the recovery action. Exit codes:
0 success, 1 action failed, 2 bad usage/stale ref, 3 daemon/browser failure.

---

## 5. CLI Surface (v1)

Verb names align with agent-browser where semantics match. Global flags:
`--session NAME` (default `default`), `--json`, `--timeout MS`, `--quiet`.

```
# Navigation & lifecycle
ebrowse open <url>              # launch browser if needed + navigate (alias: goto)
ebrowse back / forward / reload
ebrowse close [--all]           # close session (browser stays if other sessions live)
ebrowse tabs                    # list tabs; ebrowse tab <n> to switch
ebrowse connect <cdp-url|port>  # attach mode instead of launch
ebrowse daemon status|stop
ebrowse doctor                  # env/browser/summarizer health with fix hints

# Observation
ebrowse outline [--refresh] [--wait-summaries] [--no-summaries]
ebrowse expand <sid|@ref> [--cursor N] [--all]
ebrowse screenshot [--section <sid>] [--ref @e] [--full] [-o PATH]
ebrowse get text|value|attr|title|url [<sel|@ref>] [<attr>]

# Actions (all return diffs per §4.3)
ebrowse click <@ref|css> [--double] [--right] [--new-tab]
ebrowse fill  <@ref|css> <text>          # clear + type
ebrowse type  <@ref|css> <text> [--enter]
ebrowse press <keys>                     # Enter, Control+a, Escape…
ebrowse check|uncheck <@ref|css>
ebrowse select <@ref|css> <value>        # native <select> only in v1
ebrowse scroll down|up|<sid|@ref> [--pages N]
ebrowse upload <@ref|css> <files…>
ebrowse eval <js>                        # escape hatch; prints JSON result, then diff
```

Deliberately **not** in v1 (see roadmap §10): `fill-form`, `search`, custom-dropdown
`select`, `query`, `explore`, `read` (article extraction), MCP server, streaming.

---

## 6. Configuration

`~/.config/ebrowse/config.toml`, overridable by env (`EBROWSE_*`) and flags.
Loader in `config.py`; every key has a default; unknown keys warn, don't fail.

```toml
[daemon]
idle_shutdown_minutes = 30

[browser]
mode = "launch"                 # "launch" | "cdp"
headless = true
cdp_url = ""                    # used when mode = "cdp" or via `ebrowse connect`
profile_dir = ""                # default: ~/.cache/ebrowse/profile (launch mode)
viewport = [1280, 1280]

[summarizer]
enabled = true                  # silently degrades to deterministic if unreachable
base_url = "http://127.0.0.1:5001/v1"
model = "default"               # llama.cpp ignores; set for multi-model servers
api_key = ""
vision = true                   # image captions + list-section screenshot summaries
max_input_tokens = 100000       # batch budget per page call
timeout_s = 60

[observe]
quiescence_ms = 300             # post-action settle debounce
quiescence_max_ms = 3000
preview_chars = 120
list_page_size = 20
resummarize_element_delta = 3   # WebChallenger UpdateSection threshold
max_sections = 60               # overflow → merged tail "s60 content (overflow)"

[security]
allowed_domains = []            # empty = all; enforced daemon-side on navigation
```

---

## 7. Package Layout

Repo root: `~/code/jayoo_personal/efficient_browsing/` (the `agent-browser/` checkout
stays as reference material; exclude from packaging).

```
pyproject.toml                  # uv-managed; deps: playwright, httpx, typer, loguru
src/ebrowse/
  __init__.py
  model.py                      # §3 dataclasses (FROZEN interface)
  config.py
  core/
    js/discover.js              # single-pass DOM walker (the only page-side code)
    snapshot.py                 # DomSnapshot types + evaluate() wrapper
    split.py                    # DomSnapshot → [Section] skeleton
    clickable.py                # interactable predicate (JS side mirrors this)
    label.py                    # deterministic heading/preview labels
    fingerprint.py              # section fingerprints, ElementDesc matching, RefRegistry
    diff.py                     # PageMem × PageMem → Diff
    render.py                   # outline / expand-markdown / diff renderers (FROZEN formats)
    locate.py                   # ElementDesc → Playwright locator chain + occlusion check
  summarize/
    client.py                   # OpenAI-compatible chat client (httpx)
    batch.py                    # per-page batched labels; VLM captions
    cache.py                    # sqlite content-hash → summary/caption store
  session.py                    # Session, PageState, observation orchestration
  actions.py                    # verbs, quiescence, diff orchestration
  daemon/
    protocol.py                 # JSON-RPC types over newline-delimited unix socket
    server.py                   # asyncio server, SessionManager
    lifecycle.py                # autostart, pidfile, socket path, idle shutdown
  cli/
    main.py                     # typer app; one function per verb; zero logic
tests/
  fixtures/pages/*.html         # served by tests/fixture_server.py
  fixtures/domsnapshots/*.json  # captured DomSnapshots for pure-core tests
  golden/*.txt                  # pinned outline/expand/diff renderings
  test_core_*.py                # pure, no browser
  test_browser_*.py             # pytest-asyncio + headless chromium (marked "browser")
  test_e2e_*.py                 # via CLI against daemon (marked "e2e")
AGENTS.md                       # implementer guide: principles digest + phase status
SKILL.md                        # host-agent usage guide (written Phase 5)
```

Tooling: `uv` for env/deps, `ruff` (format+lint), `pytest` + `pytest-asyncio`.
`make test` runs pure tests; `make test-browser` includes browser marks.

---

## 8. Implementation Phases

Dependency chain: P0 → P1 → P2 → P3 → P4 → P5, but P4 only depends on P2 and can run
parallel to P3 if staffed separately. Keep `AGENTS.md` phase-status table updated at
each phase completion.

---

### Phase 0 — Scaffold & fixtures

**Goal:** a repo other agents can work in without setup questions.

Deliverables:
- `pyproject.toml` (uv), `src/` layout as §7, ruff + pytest config, `make` targets
  (`test`, `test-browser`, `lint`, `fmt`).
- `model.py` and `config.py` fully implemented per §3/§6 (they're just dataclasses +
  a TOML loader — land them here so P1/P2 build against the frozen interface).
- Fixture HTTP server (`tests/fixture_server.py`, stdlib) + initial fixture pages:
  `article.html`, `form.html` (labels, selects, checkboxes, validation),
  `list.html` (product-card grid ≥ 30 items), `table.html` (sortable, row actions),
  `dropdown.html` (custom JS menu + native select), `spa.html` (button mutates DOM,
  pushState navigation), `iframe.html` (same-origin child), `dialogs.html`
  (alert/confirm + modal div), `huge.html` (100+ sibling sections for overflow).
- `ebrowse --help` works (verbs stubbed with "not implemented", exit 2).
- `AGENTS.md` with the §1 principles digest and a phase-status table.

Done when: `make lint test` green in a fresh clone; fixture server serves all pages.

---

### Phase 1 — Core page model (pure)

**Goal:** DomSnapshot → PageMem → rendered outline/expand, no daemon, no LLM.
This is the highest-judgement phase; the WebChallenger adaptation map (§9) is
primarily about this code.

Deliverables:
1. **`core/js/discover.js`** — one `page.evaluate()` pass returning a JSON DOM tree:
   per node `{tag, attrs (curated set), text, bbox, visible, clickable_signals
   {tag_hit, role_hit, listener_hit, cursor_pointer}, children}`. Traverses open
   shadow roots. Same-origin iframes are walked by the Python wrapper via separate
   `frame.evaluate()` calls and stitched into the tree with `iframe_path` set
   (cross-origin frames become leaf nodes flagged `cross_origin`). Curate attrs to
   the ElementDesc needs + `class` + data-* used by fingerprints; do not ship whole
   attribute maps.
2. **`core/snapshot.py`** — DomSnapshot dataclasses + the (thin, async) evaluate
   wrapper. The wrapper is the *only* function in core touching Playwright, kept
   separate so everything downstream is pure.
3. **`core/clickable.py`** — the interactable predicate: visibility/accessibility
   gate AND (interactable tag ∨ listener attr ∨ interactable ARIA role ∨
   cursor:pointer), per the WebChallenger appendix. Decision happens JS-side for
   speed; this module owns the canonical tag/role/listener sets, which are
   string-templated into discover.js at build time so there is one source of truth.
4. **`core/split.py`** — DividePage adaptation: recursive descent terminating at
   grouping tags (`ol ul table form fieldset aside article details p img embed code
   nav header footer` + role=group/dialog), size thresholds (node smaller than
   ~900×320 or ~500×800 CSS px → terminal), and sibling merging (≥4 consecutive
   same-tag+normalized-class siblings → one `list` section). Emits ≤
   `observe.max_sections`, merging overflow into a tail section. Assign
   `SectionType` from tag/role/landmark + composition heuristics (a terminal section
   whose elements are mostly inputs → `form`, etc.).
5. **`core/label.py`** — deterministic labels: nearest heading (h1–h6, aria-label,
   legend, caption, summary) + `preview_chars` of collapsed text; counts
   (links/inputs/buttons/items); token estimate = `len(rendered)//4`.
6. **`core/fingerprint.py`** — §3.2 fingerprints; class normalization (adapt
   WebChallenger `element_class_str`); `RefRegistry` with descriptor matching
   (exact durable-attr match → `nth_hint` → bbox proximity).
7. **`core/render.py`** — outline + expand renderers per §4.1/§4.2, including list
   pagination cursors. Golden-tested.
8. **Dev harness** — `python -m ebrowse.dev <url> [outline|expand sN]`: launches
   Playwright directly (no daemon) for manual iteration, and `--capture` writes the
   DomSnapshot JSON into `tests/fixtures/domsnapshots/`.

Interfaces honored: `model.py` frozen; render formats frozen at phase end.

Tests: pure tests from captured DomSnapshot fixtures (every fixture page gets one);
golden files for outline+expand of each fixture; browser-marked tests asserting
discover.js output shape on the fixture server; property checks (section bboxes tile
the page without gaps > threshold; every clickable element belongs to exactly one
section; refs stable across two identical discoveries).

Done when: dev harness renders sane outlines for the fixture set **and** 5 real sites
(news article, GitHub repo page, Amazon search results, Wikipedia article, a
docs site), reviewed by a human; outline of each fixture ≤ 15% of the token count of
its full aria snapshot (measure with `chars//4`; record in the PR).

---

### Phase 2 — Daemon, sessions, CLI plumbing

**Goal:** the full client→daemon→browser loop with observation verbs.

Deliverables:
1. `daemon/protocol.py` — request `{id, session, verb, args}`, response
   `{id, ok, output, json, error}` over newline-delimited JSON on a unix socket at
   `$XDG_RUNTIME_DIR/ebrowse.sock` (fallback `~/.cache/ebrowse/`).
2. `daemon/lifecycle.py` — CLI autostarts daemon (double-fork or
   `python -m ebrowse.daemon` detached), pidfile + stale-socket cleanup, idle
   shutdown, structured logs to `~/.cache/ebrowse/daemon.log`.
3. `daemon/server.py` — SessionManager: named sessions, per-session command lock,
   lazy browser start. Browser modes: launch (persistent context in
   `browser.profile_dir`) and CDP attach (`connect` verb / config). Adopt new tabs
   opened by page actions as the active tab; `tabs`/`tab <n>` to inspect/switch.
4. `session.py` — PageState holding current PageMem, RefRegistry, nav_id; the
   `observe()` orchestration (discover → split → fingerprint-match → label → render).
5. Verbs: `open/goto`, `back/forward/reload`, `outline` (deterministic labels only),
   `expand`, `screenshot` (viewport, `--full`, `--section` via bbox clip, `--ref`),
   `get`, `tabs/tab`, `close`, `connect`, `daemon status|stop`, `doctor`.
6. `doctor`: checks python/playwright/chromium install, socket writability, summarizer
   reachability (warn only), CDP url validity if configured; prints fix hints.

Interfaces honored: §4 outline/expand formats; §5 verb names/flags; §6 config keys.

Tests: e2e-marked pytest driving the real CLI against the fixture server (fresh
tmp HOME per test); daemon restart/stale-socket recovery; two concurrent sessions
don't interleave; CDP attach test against a launched chromium's own CDP port.

Done when: the §11 example transcript (observation half) works verbatim against the
fixture server and a real site.

---

### Phase 3 — Actions, refs, diffs

**Goal:** action verbs that return diffs; the tool becomes genuinely usable.

Deliverables:
1. `core/locate.py` — ElementDesc → locator resolution chain:
   `#id` → `[data-testid]` → `role+name` → `placeholder` → normalized-href →
   text match, each scoped to the element's section locator when ambiguous,
   `nth_hint` as final disambiguator. Resolution must verify uniqueness and bbox
   sanity vs. the descriptor. Occlusion check before click (elementFromPoint at the
   click point; on mismatch, report the covering element's section per §4.3).
2. `actions.py` — verbs `click fill type press check uncheck select scroll upload
   eval` implemented as: resolve → act → `wait_quiescence()` → re-observe → diff →
   render. Quiescence: wait for `domcontentloaded` if navigation started, else
   MutationObserver-based settle (no mutations for `quiescence_ms`, capped at
   `quiescence_max_ms`), installed once per page via init script.
3. `core/diff.py` — PageMem × PageMem → Diff per §3: sections matched by
   fingerprint; element add/remove by descriptor; state changes (value, checked,
   expanded, disabled) by ref. Navigation detection: URL change or document swap →
   `kind="navigation"` with full new outline, fingerprint-matched persistent
   sections marked `(unchanged)`.
4. Dialog & popup defaults: native dialogs auto-dismissed (confirm → accept,
   configurable later), always reported in diff notes; new tabs adopted and reported;
   file-chooser suppressed in favor of `upload`.
5. `no_change` detection with the honest caveat line (§4.3).
6. Diff-triggered cache maintenance: sections whose element delta ≥
   `resummarize_element_delta` get `summary=None` (re-summarization is P4's job).

Tests: fixture-driven action→diff goldens (dropdown click reveals menu items; SPA
button mutates DOM; pushState navigation; form validation error appears); stale-ref
error path; occluded-click path (modal fixture); scripted-mutation diff unit tests in
pure core (mutate a DomSnapshot JSON, assert Diff).

Done when: an agent (drive it by hand with Claude Code) can complete "search fixture
list page, open an item, fill the form, submit" using only outline/expand/diff
outputs — no raw snapshots — and every intermediate output matches §4 formats.

---

### Phase 4 — LLM sidecar (summaries & captions)

**Goal:** the `≈` labels. Depends on P2 only; parallelizable with P3.

Deliverables:
1. `summarize/client.py` — OpenAI-compatible chat client (httpx, async), text +
   image-content parts, hard timeout, single retry, circuit breaker (3 consecutive
   failures → disable for 10 min, log once, outline shows `summaries: unavailable`).
2. `summarize/batch.py` — **one call per page**: input is per-section digests
   (deterministic label + truncated content, budgeted to `max_input_tokens` across
   sections, long sections truncated head+tail); output is strict JSON
   `[{sid, summary}]` (retry once on parse failure with a "JSON only" nudge).
   Summaries: one line, ≤ 140 chars, imperative-free, may inline at most 2 `(@ref)`s
   that must exist (validate; strip invalid refs). Prompt lives in
   `summarize/prompts.py` as a plain constant.
3. Vision paths (config `vision=true`): list-section summaries from a section-bbox
   screenshot (the WebChallenger trick — cheaper than serializing 100 cards) when the
   section's text digest exceeds its budget; image captions for `<img>` ≥ 80×80 CSS px
   in expanded sections, cached by src-hash, rendered as `![≈caption](@imgN)`.
4. `summarize/cache.py` — sqlite at `~/.cache/ebrowse/summaries.db`:
   `(content_hash → summary)`, `(image_hash → caption)`; survives daemon restarts;
   prune LRU past 50k rows.
5. Wiring in `session.observe()`: cache lookup inline; misses → background asyncio
   task; `--wait-summaries` awaits it; `--no-summaries` skips; status line per §4.1.
   Backfilled summaries appear on the *next* outline (never mutate emitted output).
6. Injection hygiene: summarizer output is data. Strip newlines/control chars,
   clamp length, never allow it to alter structure (sid/type/counts stay
   deterministic). Provenance marker `≈` is added by the renderer, not the model.

Tests: mock OpenAI-compatible server (tests spin an aiohttp/stdlib stub) for batch,
cache-hit, circuit-breaker, and malformed-JSON paths; golden outline with mixed
`≈`/`|` labels; a live-marked test against the real Qwen server (skipped when
unreachable) asserting end-to-end label quality manually.

Done when: outline on fixture pages shows cached `≈` labels on second call with the
mock server; summarizer being down changes nothing but labels; a diff exceeding the
element-delta threshold invalidates and re-fills the affected summary.

---

### Phase 5 — Agent UX, hardening, packaging

**Goal:** other harnesses (and future phases' agents) can adopt the tool from its
docs alone.

Deliverables:
1. **`SKILL.md`** — host-agent usage guide modeled on OpenClaw's browser-automation
   skill: the operating loop (outline → expand relevant sections → act → read diff →
   only re-outline on navigation/confusion), ref durability rules, stale-ref and
   occlusion recovery, when to use screenshots, session hygiene. Include a worked
   transcript (§11). This file is written *for LLM consumption*.
2. `--help` audit: every verb's help ≤ 6 lines, examples included; root help lists
   the operating loop in 4 lines.
3. Error-message audit against §1.8 (grep every `raise`/error return; each names a
   recovery command).
4. Real-site smoke suite (manual-run script + checklist, 10 diverse sites incl. an
   SPA, a docs site, a shop, a login form): record outline token counts vs. full aria
   snapshot; fix the worst structural misparses; document known limitations in
   README.
5. Performance pass: outline p50 < 1.5s on fixture pages (excluding summarizer);
   discover.js on huge.html < 500ms; no per-element Playwright round trips anywhere
   (grep-able invariant: `locator(` allowed only in `locate.py`, `snapshot.py`,
   screenshot clipping).
6. Packaging: `pipx install .` works; `ebrowse doctor` guides Playwright browser
   install; README (install, quickstart, config reference, roadmap); version verb.

Done when: a fresh Claude Code session pointed only at README + SKILL.md completes a
3-site task run without human hints about the tool.

---

## 9. WebChallenger Adaptation Map

| WebChallenger source (agent.py unless noted) | ebrowse home | Adaptation notes |
|---|---|---|
| `split_section`, `should_split`, `merge_sections`, `element_split` (~L1671–1888) | `core/split.py` | Port the recursion/thresholds/sibling-merge logic, but operate on DomSnapshot JSON, not live Locators. Keep grouping-tag set + size constants config-adjacent. |
| `is_clickable`, `clickable_locators` (~L1109–1244) + paper appendix predicate | `core/clickable.py` + `discover.js` | Predicate evaluated in-page in one pass instead of per-locator round trips. Canonical sets live in Python, templated into JS. |
| `get_elem_locator`, `get_base_locator` (~L1390–1578) | `core/locate.py` | Same priority idea (id > testid > role+name > text > href) but driven from ElementDesc; add uniqueness verification + occlusion check. |
| `update_section` diff (Δ+/Δ−/Δ~) (~L3810) + paper Alg. 2 | `core/diff.py` | Generalize from "elements in one section" to full PageMem diff with fingerprint matching; keep the ≥3-delta re-summarize threshold as config. |
| `element_class_str` (~L1342) | `core/fingerprint.py` | Class normalization for fingerprints and descriptor matching. |
| `section_content_md`, `section_content` (~L6851–7064) | `core/render.py` | Markdown rendering with inline refs; drop the LLM-extraction hooks entirely (full content goes to the host model). |
| `summarize_section`, `summarize_page` (~L3517–3660) | `summarize/batch.py` | Was per-section VLM calls during construction; becomes one batched background call, cache-keyed, never blocking. |
| List-section screenshot summarization (UpdateSection list branch) | `summarize/batch.py` vision path | Keep: screenshots are cheaper than serializing 100 uniform cards. |
| `element_crop`, `section_screenshot` (~L4505–4618) | screenshot verb (`actions.py`) | bbox-clip screenshots; drop highlight/margin variants until needed. |
| `Element` (memory/agent_memory.py, ~60 fields) | `model.py ElementDesc/State` | Slim to durable-identity + volatile-state split; drop exploration/workflow fields (dropdown_elements, tab_sections, …) until v2/v3 need them. |
| `check_modal`, `check_dialog`, `handle_popup` (~L2431, 4772, 9027) | `actions.py` dialog handling | Simplify: Playwright dialog handler + dialog-role section detection in split. |
| Exploration (`explore_page_v3` etc.), workflows (`submit_form`, `dropdown_action`, `search`), table/list iteration, bookmarks | **not v1** | Roadmap §10; the v1 substrate (sections/diffs/refs) is designed so these layer on without core changes. |

Explicitly dropped: numbered-action-list interface (harness policy, not tool);
VLM-during-construction (all model calls lazy); Playwright sync API; benchmark env
coupling; screenshot-directory side effects.

---

## 10. Post-v1 Roadmap

Ordered; each item layers on the v1 substrate without changing frozen interfaces.

**v1.1 — Compound verbs (deterministic state machines).**
`fill-form <sid> --data '{field: value}'` (match fields by label/name/placeholder;
handle native selects, checkboxes, validation-error detection via diff; report
per-field outcomes + remaining errors in one result). `select <@ref> <text>` for
*custom* dropdowns: click → diff for revealed menu → match option text → click →
verify via diff; ambiguity returns the candidate list instead of guessing.
`search <@ref> <query> [--pick <text>]` with suggestion-popup handling. All are
internal act→diff loops in `actions.py`; zero LLM. WebChallenger references:
`submit_form`, `dropdown_action`/`click_check_dropdown`, `search`/
`choose_search_suggest`.

**v1.2 — List/table querying.**
`query <sid> [--where "text~=…"] [--cols …] [--cursor …] [--sort col]` — schema
inference from item structure (`list_item_structure`, `table_headers` as reference),
cursor pagination, sort-state detection. Optional `--llm-filter "<criterion>"`
delegating chunked relevance filtering to the sidecar (the one place extraction
returns: for filtering, not for reading).

**v1.3 — MCP server.**
`ebrowse mcp` serving the same daemon session over stdio MCP; tool schemas generated
from the CLI verb definitions so surfaces can't drift. Outline/expand/diff text goes
in tool results verbatim.

**v2 — Site memory.**
Passive: persist PageMems (per normalized URL + fingerprint template) in
`~/.cache/ebrowse/sites/<domain>.db`; on revisit, prefill summaries/refs and mark
known-but-hidden dropdown contents. Active: `ebrowse explore <origin> [--depth N
--budget M]` — the deterministic WebChallenger crawler (template matching to avoid
re-exploring uniform list pages; per-element click-and-record with revert) producing
bookmark hints appended to outlines (`known pages: "Bestsellers Report" →
/reports/...`). Open UX questions (auth walls, destructive clicks during exploration
— default to GET-navigation-only exploration) get their own mini-design before
implementation.

**v2.x — Evaluation harness.**
Scripted A/B on WebArena-lite: capable model + plain snapshot tool vs. ebrowse;
success rate + tokens/task. Reuses the paper's infra; doubles as a regression suite
and a publishable result.

**Later / opportunistic:** stealth/anti-detection backend parity (camoufox),
cross-origin iframe support via CDP, download handling, HAR/network observation verb,
`read` (article-mode extraction) for parity with agent-browser.

---

## 11. Example Session (anchor for UX decisions)

```text
$ ebrowse open https://shop.example.com
PAGE Example Shop — https://shop.example.com
s1 nav   9 links, 1 input  ~420t | "Shop, Categories, Deals — search box (@e2)"
s2 list  12 items          ~2.1kt | "Featured products"
s3 form  3 inputs          ~180t | "Newsletter signup"
s4 footer 22 links         ~600t | "About, Support, Legal"

$ ebrowse fill @e2 "espresso machine"
TYPE @e2 (input "Search") → partial change
~ @e2 value: "" → "espresso machine"
s5 list appeared  6 items  ~220t | "Search suggestions"

$ ebrowse expand s5
## s5 list — Search suggestions
1. [espresso machine (@e41)](→ /s?q=espresso+machine)
2. [espresso machine with grinder (@e42)](→ /s?q=espresso+machine+grinder)
…

$ ebrowse click @e41
CLICK @e41 (link "espresso machine") → navigation
PAGE Search: espresso machine — https://shop.example.com/s?q=espresso+machine
s1 nav   (unchanged)
s2 form  14 inputs  ~380t  ≈ Filter sidebar: brand, price range, rating (@e55: min price)
s3 list  24 items   ~4.8kt ≈ Results: product cards with name, price, rating, Add-to-cart
s4 nav   6 links    ~120t  | "Pages 1 2 3 Next"

$ ebrowse expand s3 --cursor 0
## s3 list — Results (items 1–20 of 24)
| # | product | price | rating | |
|---|---------|-------|--------|--|
| 1 | [Bella Pro 20-bar (@e61)](→ /p/1043) | $129.99 | 4.6★ (2,013) | [Add to cart (@e62)] |
…
… 4 more items — expand s3 --cursor 20
```

Total context consumed by this flow: ~1.2k tokens. The same flow via full aria
snapshots (4 snapshots of a commerce page) is typically 25–40k tokens.

---

## 12. Risks & Open Questions

| Risk | Stance |
|---|---|
| SPA quiescence heuristics wrong (diff fires early/late) | MutationObserver debounce is best-effort; cap + honest `no_change` caveat; tune constants from smoke suite; config escape hatch. |
| Section splitter quality on wild pages | The single most quality-sensitive code. Mitigate with real-site done-criteria in P1, golden fixtures, and the `max_sections` overflow valve. Expect iteration. |
| Descriptor matching too strict/loose (ref churn or misbinding) | Start strict (exact durable attrs); log match-rate telemetry in daemon log; loosen deliberately with tests. Misbinding is worse than churn. |
| Cross-origin iframes invisible | Accept for v1; surface presence in outline so the agent knows to screenshot. CDP route in roadmap. |
| Closed shadow DOM | Out of scope; document. |
| CDP-attach mode: user interacting concurrently | Serialize commands per session; document that attach mode is best-effort; diffs make external changes visible rather than corrupting state. |
| Summary injection (page text → model → label) | Provenance markers, length clamp, structure never model-controlled (§Phase 4.6); host harness owns trust policy. |
| llama.cpp server down/slow | Circuit breaker; tool is fully functional deterministic-only by design. |
| Daemon state vs. multiple CLI callers | Named sessions + per-session lock; last-writer-wins within a session is acceptable for v1. |

---

## 13. Glossary

- **DomSnapshot** — JSON tree from one discover.js pass; the only raw page data.
- **PageMem** — structured page representation (sections + elements); per-page.
- **Section / sid / fingerprint** — semantic page region; positional id; stable identity.
- **Ref (@eN)** — session-scoped durable element handle backed by an ElementDesc.
- **Outline** — the skimmable per-section TOC (§4.1).
- **Diff** — what changed after an action (§4.3); the default action output.
- **Summarizer** — optional OpenAI-compatible sidecar (local Qwen3.6-35B) producing
  `≈` labels and captions; never load-bearing.
