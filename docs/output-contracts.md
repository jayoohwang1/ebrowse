# Output contracts (FROZEN)

The data model (`src/ebrowse/model.py`) and the output formats below are frozen
interfaces. Extend by adding optional fields or lines; never repurpose existing ones.
**Renderer changes require updating `tests/golden/` and this document in the same
commit.** Default output is compact plaintext for LLM consumption.

## Data model

`model.py` is the source of truth for field definitions (`ElementDesc`,
`ElementState`, `Element`, `Section`, `PageMem`, `SectionDiff`, `Diff`). All types are
`slots=True` dataclasses, JSON-serializable via `to_dict()/from_dict()`. The semantic
rules that are not visible from the field list:

`PageMem.truncated` is an optional, backward-compatible capture-completeness flag.
When true it produces the outline warning documented below; absence means false.

### Ref semantics (@eN)

- Refs are **session-scoped and monotonic**: `@e1, @e2, …` assigned on first sight,
  never reused for a different element. The session keeps a `RefRegistry`
  (descriptor → ref).
- On every observation, discovered elements are matched against the registry by exact
  `ElementDesc.match_key()` equality, tie-broken by nth occurrence on the page
  (`nth_hint` is a tiebreaker, deliberately *not* part of identity). Matched elements
  keep their refs across re-snapshots, DOM mutations, *and navigations* — a site's
  persistent chrome (header search box, nav links) keeps the same refs on every page.
- Acting on a ref that no longer resolves fails fast:
  `stale ref @e12 … — run 'ebrowse outline'` (exit 2).
- Every action verb also accepts a CSS selector as the target (escape hatch).
- Matching strictness is a deliberate choice: misbinding is worse than ref churn.
  A control whose visible text changes reads as remove+add, not a state change
  (`text_head` is part of identity). See [ADR 0003](adr/0003-strict-ref-matching.md).
- **`@iN` image refs are different**: page-scoped, NOT durable across observations.
  They exist for `screenshot --ref` and captions, not actions. See
  [ADR 0006](adr/0006-image-refs-not-durable.md).

### Section identity

- `sid` (s1…sN) is positional *within the current page* and restarts each navigation
  (scoped by `nav_id`; the outline header shows the URL, so no ambiguity).
- `fingerprint` provides identity across mutations and revisits:
  `hash(tag, normalized_class, role, heading, iframe_path, parent_tag_chain)`. Class
  normalization strips state/generated tokens (`is-*`, `css-*`, hashes…).
- Diffing and summary caching key on `fingerprint` + `content_hash`, never on sid.
- Oversized semantic containers may be represented by multiple lossless fragments.
  A nested queryable collection is its own section; residual form/content runs remain
  in document order and every interactive element belongs to exactly one section.

## Outline (`ebrowse outline`)

`outline` is the ONLY verb that reads the page and runs the summarizer.
Navigation (`open`/`back`/click-through) returns a landing line, not the page
(see *Navigation result* below) — the agent calls `outline` to read it.

```
PAGE Amazon.com : sony headphones — https://www.amazon.com/s?k=sony+headphones
◉ Search-results grid of black over-ear headphones; filter sidebar left. No modals or popups visible.
s1 nav     12 links, 2 inputs   ~800t  ≈ Site header: search box, account menu, cart
s2 form    18 inputs            ~450t  ≈ Filter sidebar: brand, price, rating checkboxes
s3 list    24 items, 48 links   ~6.2kt | "Search results — Sony WH-1000XM5 $348 …"
s5 iframe  (cross-origin: ads.doubleclick.net)
```

- One line per section: `sid type  <counts>  ~<tokens>  <label>`.
- Label provenance: `≈` = LLM section summary (model-paraphrased page content,
  untrusted); `|` = deterministic (heading + preview, verbatim page text, quoted);
  `◉` = VLM visual gist of the screenshot (untrusted, even weaker than `≈` — a
  routing signal for "is it worth a screenshot?", never data to act on).
- `◉` is an optional single line right under the `PAGE` header (when a vision
  sidecar is configured + reachable, `summarizer.glance = true`). It describes
  only what is visible and flags overlays/modals/interstitials the DOM outline
  can't convey. Absent otherwise. `outline --no-glance` suppresses it per call.
- Summaries + glance are filled **synchronously** before the outline returns
  (concurrently, under `summarizer.sync_timeout_s` — which bounds each sidecar
  call; the summaries path may fire one JSON-only reprompt, so its worst case is
  ~2× that). Cache hits are free; a slow/dead sidecar (or a cache-layer error)
  degrades to deterministic labels + no `◉` line, with a status note
  (`summaries: 2/4 (sidecar slow or incomplete) · glance: sidecar slow or unavailable`).
  There is no async "backfill running" phase.
- `~Nt` is the token estimate of expanding that section (`len(rendered)//4`) — the
  outline renderer and expand renderer are coupled on purpose so the estimate is
  exactly what the default `expand` would cost. For a list/table this is the default
  paginated window, not the explicitly requested `--all` output.
- Ordinary sections are partitioned near `observe.max_section_tokens` (default
  16,384). `observe.max_sections` is a soft outline-size target: the outline may
  exceed it rather than hide controls, merge collections, or violate the expansion
  budget.
- If DOM discovery reaches its node cap, the final outline line warns:
  `NOTE snapshot truncated at the DOM node limit — use 'ebrowse screenshot --full'
  to inspect potentially omitted content`.
- Cross-origin iframes are listed but not entered.
- `--no-summaries` skips the `≈` labels; `--no-glance` skips the `◉` line.
- `--preview` (opt-in) appends a short verbatim preview after each summary line,
  keeping both markers: `s1 nav  12 links  ~800t  ≈ Site header …  | "Deliver to …"`.
  The default line is unchanged; only summary-bearing lines gain the `| "…"` tail.
  Preview width is `observe.combined_preview_chars` (default 60). Costs ~+50-80%
  outline tokens — for when a section's literal text may answer without `expand`.

## Expand (`ebrowse expand s2`, `--cursor N`, `--all`)

Markdown rendering of one section's full content with inline refs — not an
accessibility tree.

```
## s2 form — Filter sidebar
### Brand
[ ] Sony (@e31) [ ] Bose (@e32) [x] JBL (@e33)
### Price
[min (@e34: empty)] [max (@e35: empty)] [Go (@e36)]
```

- Links: `[text (@ref)](→ /path)` — href path-only for same-origin, whole for external.
- Inputs: `[label (@ref: "value")]` / `empty`, `, required` when set; checkboxes
  `[x]`/`[ ]`; native selects `[label (@ref) ▾ "US" of 24 options]` (options inlined
  when ≤ 15; the total is the REAL option count even when capture stops at 350
  options; `, multiple` marks `<select multiple>`, whose current selections join
  as `"A, B"`). `expand @ref` on a select pages through the full option list
  (see below).
- **Inner scroll containers**: when a section holds a real scroll container
  (overflow auto/scroll with hidden content), the expand header carries
  `(inner scrollable panel: y=0 of 1104px — 'ebrowse scroll s3 down' scrolls
  it)`. `scroll <sid|@ref> down|up` scrolls inside that container and reports
  `container div#results scroll y=600/660` (` — at the bottom/top` at the
  edges); newly mounted lazy/virtualized rows show in the action diff.
- **Effective state**: disabled controls keep their refs and are marked —
  `[Place order (@e9) disabled]`, `[Street (@e4: empty, disabled)]` — including
  fieldset-inherited disabling; elements under `[inert]` are marked ` inert`.
  Acting on a disabled/inert ref fails fast naming the state (exit 1).
- **Candidates**: `[Save changes (@e4 ?)]` — the `?` inside the ref parens marks a
  weak-evidence discovery (a real event listener found by the CDP sweep, an
  explicit `tabindex`, or role-less ARIA state) rather than a proven control.
  Candidates are expand-only: they never appear in outline element counts, and
  their evidence never authorizes proxy activation (ElementState.candidate holds
  the provenance: `listener` | `focusable` | `aria-state`).
- Images: `![alt](@i3)` or `![≈caption](@i3)` (VLM caption, cached).
- List/table sections paginate (up to 20 items by default, also bounded by
  `observe.max_section_tokens`):
  `… 104 more items — expand s3 --cursor 20`. Tables render as markdown tables with
  a `#` index column; row indices are stable so `--cursor` composes with `query`.
  `--all` explicitly bypasses the page budget.

## Expand a select (`ebrowse expand @e5`, `--cursor N`, `--all`)

`expand` on a ref that is a native `<select>` lists ITS options (50 per page),
not the enclosing section — the way to browse past the 15-option inline limit.

```
SELECT Country (@e5) ▾ "Country 1" — 400 options
1. Country 1
2. Country 2
…
50. Country 50
… 300 more options — expand @e5 --cursor 50
```

- Header mirrors the inline form: label, ref, current selection(s), REAL total,
  `, multiple` when applicable. Later pages start with
  `(options 301–350 of 400)`.
- Capture stops at 350 options; past that the tail is honestly absent:
  `(options beyond 350 were not captured — 'ebrowse select @e5 "<label>"' still
  matches any option by its text)`. `select` always matches against the live
  DOM, captured or not.
- `expand @ref` on any non-select element still expands its section.

## Expand ax view (`ebrowse expand s2 --ax`)

An opt-in, deterministic accessibility-tree rendering of one section, rather
than the default markdown expand. The default expand is unchanged, and the
outline's `~Nt` estimate remains coupled to that default markdown output.
`expand @ref --ax` resolves the ref to its enclosing section, including native
selects (so `--ax` overrides select option paging). No summarizer or captions
participate.

```
## s2 form — Filter sidebar (ax)
- form "Filter sidebar"
  - heading "Brand" [level=3]
  - checkbox "Sony" (@e31) [unchecked]
  - checkbox "JBL" (@e33) [checked]
  - textbox "min price" (@e34) [value=""]
  - button "Go" (@e36)
  - text: "Prices update automatically."
```

- The header is the markdown expand header plus ` (ax)`; an inner-scrollable
  panel note is unchanged. A cross-origin iframe returns the same one-line
  notice as markdown expand, with no ax body.
- Nodes are `- role "name" (@eN) [state, state]`; name, ref, and states are
  omitted when empty (no `""` or `[]`). An explicit `role` wins; otherwise a
  data-driven HTML-AAM mapping applies: `a[href]` link; `button` button;
  text/email/url/tel/no-type inputs and `textarea` textbox; search input
  searchbox; checkbox/radio/range/number inputs checkbox/radio/slider/spinbutton;
  `select` combobox (listbox when `multiple` or `size>1`), `option` option;
  `h1`–`h6` heading; `ul`/`ol` list; `li` listitem; `table` table; `tr` row;
  `td` cell; `th` columnheader; `img` img; `nav` navigation; `main` main;
  `header` banner; `footer` contentinfo; `aside` complementary; `form` form;
  `article` article; named `section` region; `fieldset`/`details` group;
  `summary` button; `dialog` dialog; `hr` separator; `progress` progressbar;
  `figure` figure; `p` paragraph; and `blockquote` blockquote.
- An unmapped/generic container with no explicit role, ref, or accessible name
  is pruned and its children promote to its depth; its own text still surfaces
  as `text:`. Name is resolved `nm`, else own text, clipped to 80 chars.
- Refs are matched by RawSection node identity to `section.elements`; weak
  candidates render `(@eN ?)` and image refs render `(@iN)`.
- States appear only when set/applicable: checked/unchecked (checkbox, radio,
  switch); disabled; expanded/collapsed when `aria-expanded` is present; pressed;
  selected; required; inert; `value="…"` (60 chars; passwords `value="•••"`); heading
  `level=N`; and native selects `value="US" of 24 options` (`, multiple` when
  applicable).
- Own text not used as the name is a child `- text: "…"`, clipped to
  `observe.preview_chars`; consecutive text at one depth is space-joined before
  clipping. There are no blank body lines or trailing whitespace.
- List/table paging uses the markdown expand window (`observe.list_page_size`)
  with unchanged item indices and tail `… N more items — expand s3 --ax --cursor 20`.
  Output is bounded by `observe.max_section_tokens`, ending at a node boundary
  with `… (truncated at token budget — use --cursor or --all)`; `--all` bypasses
  page size as it does for markdown.

## Navigation result (`open`, `back`, `forward`, `reload`, `tab`, navigating actions)

Navigation does NOT dump the page. It returns a terse landing line naming the
next action; the agent runs `outline` to read the page. This keeps observation
(and the synchronous summarizer) opt-in, and lets the page finish loading before
it's read. Durable `@refs` stay live across the navigation (the page is rebuilt
internally), so an agent can act on a known persistent-chrome ref without
re-outlining.

```
opened https://example.com/login  ·  "Sign in — Example"
run 'ebrowse outline' to read the page
```

- Verb prefix is `opened` / `reloaded` / `back to` / `forward to` / `switched to
  tab N:`. A navigating **action** keeps its `VERB target → navigation` header,
  then `now at <url>  ·  "<title>"` and the same hint.
- Surfaced `note:` lines (new-tab adoption, auto-accepted native `alert`) follow.

## Action result (every action verb)

```
CLICK @e42 (button "Add to Cart") → partial change
s7 dialog  3 links, 1 button  ~200t  | "Added to cart — Sprite Stasis Ball"  [appeared]
## s7 dialog — Added to cart
Sprite Stasis Ball added. [View cart (@e51)](→ /cart) [Checkout (@e52)](→ /checkout)
~ @e12 value: "0" → "1"
```

- First line: `VERB target (resolved description) → outcome` where outcome ∈
  `navigation | partial change | dialog | no change detected`.
- `navigation` returns the landing line above (not a full outline).
- `partial change` / `dialog` prints only the diff, ordered appeared →
  disappeared → changed: `+ sid: [added elements with refs]`,
  `- sid: N element(s) removed (names)`, `~ @ref field: "old" → "new"`,
  `~ sid: new text: "status/validation message"`.
- **`new text` quoting rules** (deterministic; word-level diff of the section's
  text): a replaced fragment carries one unchanged word of context per side
  (a `20` → `30` count tick quotes as `Showing 30 results.`, not a bare `30`);
  fragments ≤ 100 chars — status messages, validation errors, result
  counts — are quoted *before* longer bulk insertions, document order within
  each tier; each fragment is capped at `budget // min(n_fragments, 3)` chars
  (floor 120) so bulk can't crowd out the rest, and an over-cap fragment is
  elided as `start … end` (summary info often sits at an end of a bulk
  insertion), never a bare prefix; up to 5 fragments joined by ` … ` within a
  500-char budget. A section the agent has **`expand`ed on the current page**
  gets an 8000-char budget instead — it is actively reading that section, so
  its text diffs are quoted near-verbatim until the next navigation.
- An **appeared `dialog` section is expanded inline** (its full `expand`
  markdown, with `@refs`) right below its `[appeared]` line — a modal is almost
  always the forced next interaction, and this is deterministic DOM, not a guess.
  Over ~4000t the expansion is compacted rather than dropped: every interactive
  control is kept with its `@ref`, prose is truncated, and a
  `… (large dialog … expand sN for the rest)` line points at the full text.
- A modal that the splitter **coalesced into a content section** (rather than
  splitting out) shows as a changed section whose added controls sit under a
  `role=dialog` subtree. The renderer detects this: the outcome is reported as
  `→ dialog` (not `→ partial change`) and the line is tagged
  `+ s1 [dialog]: [Accept (@e6)], [Reject (@e7)]`, so a coalesced dialog carries
  the same signal as a standalone one.
- Tracked element state in `~ @ref field:` lines: `value`, `checked` (native and
  `aria-checked` on role checkbox/radio/switch), `expanded`, `disabled`,
  `pressed` (`aria-pressed`), `selected` (`aria-selected`).
- `no change detected` carries the honest caveat (may be a real no-op, or the effect
  is outside the DOM / slower than the settle window). Outcome evidence beyond the
  DOM is reported as notes: `download started: "file.pdf"`, `the document reloaded
  (same URL) — page state may have reset`, and — only on `no change detected` —
  `URL fragment changed: now at …#anchor` or `scroll position moved y=A → B`.
- Notes always surface: `note: native alert auto-accepted: "…"`, new-tab adoption.
- If an adopted tab closes, ebrowse falls back to the most recently active live
  tab and foregrounds it; tab listings keep exactly one `*` active marker.
- A hover that Playwright reports as dispatched but whose live target is not
  `:hover` adds an input-delivery warning only when the action also produced no
  DOM change: restart with `ebrowse daemon stop` before retrying.
- Occluded clicks fail *before* acting:
  `blocked: @e42 is covered by <dialog "Cookie consent"> — interact with that first` (exit 1).
  A modal that blocks the page *without* covering the target (native `showModal()`
  / `aria-modal` + `inert`, where there's no hit-testable overlay) can't be
  pre-empted safely, so the click is attempted; when it fails, the error names the
  culprit: `blocked: a modal is open (dialog "…") and is intercepting the click —
  interact with it or dismiss it first` (exit 1). If instead such a click merely
  no-ops, the same modal is named in a `note:` on the `no change detected` result.

### Native dialog opened (blocking)

`alert`/`beforeunload` are auto-accepted (a `note:` records it). A `confirm`/`prompt`
is a *decision*, so it is left open for the agent and blocks the page. The opening
action returns this instead of a diff (see ADR 0007):

```
CLICK @e8 (button "Delete item") → dialog opened (blocking)
native confirm: "Really delete this item?"
page actions are blocked until you resolve it — 'ebrowse dialog accept [text]' or 'ebrowse dialog dismiss'
```

- A `prompt` line also shows its default: `native prompt: "New name:" (default: "Untitled")`.
- While a dialog is pending, every page-touching verb fails fast (exit 1) with
  `a native <type> dialog is blocking this tab: "…" — resolve it with 'ebrowse dialog
  accept' or 'ebrowse dialog dismiss' (or 'ebrowse tab <n>' to switch tabs)…`. Only
  `dialog`, `tabs`, `tab`, `connect`, `close` run.
- `dialog accept [text]` / `dialog dismiss` resolve it, then print the opening action's
  normal diff (`accepted confirm dialog\n<diff>`); `dialog status` reports the pending
  dialog without resolving it. Note: this is distinct from the `dialog` outcome above
  (an in-page DOM modal that appeared as a section).

### Compound verbs (`fill-form`, `search`, custom-dropdown `select`)

Multi-line action header — one indented ✓/✗ line per internal step, the outcome arrow
always on the FIRST line, then ONE diff covering all steps:

```
FILL-FORM s2 (6 fields) → partial change
  ✓ Email = "jay@example.com"
  ✓ Country = "Canada" (native select)
  ✗ Phone — no matching field (have: Full name, Email, …)
~ s2: new text: "Account created!"
```

## Query (`ebrowse query s4 --filter <re> [--cols a,b] [--cursor N] [--limit N]`)

```
QUERY s4 filter="Cold Brew" — matched 2 of 24 items
| # | product | price |
| 3 | [Cold Brew Coffee Maker (@e61)](→ /p/1043) | $19.99 |
```

- Filter is regex (bad regex falls back to literal substring), case-insensitive,
  matched against each item's *plain text*, never the rendered markdown.
- Item indices are the original list positions (consistent with `expand --cursor`).
- Without `--limit`, query uses the configured item-count and token budgets. An
  explicit `--limit` opts into that many matching rows even when the result is large.
- Unknown `--cols` exit 2 listing the real column names.

## Diagnose (`ebrowse diagnose <target>`)

Read-only actionability report: a Playwright trial click (no dispatch; may
scroll the target into view) plus the blocker-diagnosis classification.

```
DIAGNOSE @e5 (span "Buy plan A")
actionability: BLOCKED — blocked: @e5 is covered by div#promo-banner "Summer
sale! …" — dismiss or interact with @e7 (div "Summer sale! …") first
```

- Line 2 is `actionability: PASS — …` or `actionability: BLOCKED — <the same
  message a blocked click would raise>`. A hit on label decoration reports PASS
  (actions route via the associated label).
- Optional `state:` line lists effective-state facts (disabled `<fieldset>`,
  `pointer-events: none`, inert region, an open dialog elsewhere).

## Describe-screen (`ebrowse describe-screen [prompt]`)

A free-form visual query answered by the local VLM over a viewport screenshot —
the routing tier between the page text and a full `screenshot` (which costs the
main agent ~2.4k image tokens; this costs only the returned text). Output is one
`◉`-prefixed line/block, untrusted (never act on it as fact).

```
◉ Two black over-ear headphones in product photos; orange search bar; yellow "Add to cart" buttons. No modals visible.
```

- No `prompt` → the concise default gist (the same text as the outline `◉` line,
  sharing its per-page cache — so it's instant if the outline already ran).
- With a `prompt` → any visual question, from "is there an overlay?" to
  "transcribe every price" to "describe every detail"; answer length is bounded
  by `summarizer.describe_max_tokens`. Custom-prompt answers are not cached.
- `--refresh` ignores the cached gist. Requires `summarizer.vision`; a clear
  error names the fix when vision isn't configured or the sidecar is down.

## Errors and exit codes

Errors are a single line on stderr, prefixed `error:`, always naming the recovery
action. Exit codes: **0** success, **1** action failed, **2** bad usage / stale ref,
**3** daemon or browser failure.
