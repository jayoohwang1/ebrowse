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

## Outline (`ebrowse outline`)

```
PAGE Amazon.com : sony headphones — https://www.amazon.com/s?k=sony+headphones
s1 nav     12 links, 2 inputs   ~800t  ≈ Site header: search box, account menu, cart
s2 form    18 inputs            ~450t  ≈ Filter sidebar: brand, price, rating checkboxes
s3 list    24 items, 48 links   ~6.2kt | "Search results — Sony WH-1000XM5 $348 …"
s5 iframe  (cross-origin: ads.doubleclick.net)
summaries: 3/4 cached · backfill running (rerun outline to see them)
```

- One line per section: `sid type  <counts>  ~<tokens>  <label>`.
- Label provenance: `≈` = LLM summary (model-paraphrased page content, untrusted);
  `|` = deterministic (heading + preview, verbatim page text, quoted).
- `~Nt` is the token estimate of expanding that section (`len(rendered)//4`) — the
  outline renderer and expand renderer are coupled on purpose so the estimate is
  exactly what `expand` would cost.
- Cross-origin iframes are listed but not entered.
- Final status line only when the summarizer is enabled and not fully cached.

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
  when ≤ 15).
- Images: `![alt](@i3)` or `![≈caption](@i3)` (VLM caption, cached).
- List/table sections paginate (default 20 items):
  `… 104 more items — expand s3 --cursor 20`. Tables render as markdown tables with
  a `#` index column; row indices are stable so `--cursor` composes with `query`.

## Action result (every action verb)

```
CLICK @e42 (button "Add to Cart") → partial change
s7 dialog  3 links, 1 button  ~200t  | "Added to cart — Sprite Stasis Ball"  [appeared]
~ @e12 value: "0" → "1"
```

- First line: `VERB target (resolved description) → outcome` where outcome ∈
  `navigation | partial change | dialog | no change detected`.
- `navigation` prints the full new outline; sections fingerprint-matched to the
  previous page are marked `(unchanged)`.
- `partial change` prints only the diff, ordered appeared → disappeared → changed:
  `+ sid: [added elements with refs]`, `- sid: N element(s) removed (names)`,
  `~ @ref field: "old" → "new"`, `~ sid: new text: "status/validation message"`.
- `no change detected` carries the honest caveat (may be a real no-op, or the effect
  is outside the DOM / slower than the settle window).
- Notes always surface: `note: native confirm auto-accepted: "…"`, new-tab adoption.
- Occluded clicks fail *before* acting:
  `blocked: @e42 is covered by <dialog "Cookie consent"> — interact with that first` (exit 1).

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
- Unknown `--cols` exit 2 listing the real column names.

## Errors and exit codes

Errors are a single line on stderr, prefixed `error:`, always naming the recovery
action. Exit codes: **0** success, **1** action failed, **2** bad usage / stale ref,
**3** daemon or browser failure.
