# ebrowse — Extended Roadmap (post-v1)

v1 (DESIGN.md phases 0–5) is complete. This document is the living plan for
what comes next. Same rules as DESIGN.md: §1 principles and §4 output formats
remain binding; each phase ends with a working tool; **every phase includes
real-site validation on Online-Mind2Web hosts** (see AGENTS.md) — fixture tests
prove correctness, real sites prove the heuristics.

Phases are ordered by (agent value ÷ implementation risk). R1–R3 are
independent of each other; R4+ build on earlier pieces.

---

## R1 — Compound verbs (deterministic state machines)

*The biggest remaining token/step win: interactions whose intermediate states
are partial page changes shouldn't cost the host agent a full decide cycle
each.* Zero LLM anywhere. WebChallenger references: `submit_form`,
`dropdown_action`/`click_check_dropdown`, `search`/`choose_search_suggest`.

**`select <@ref|css> <option-text>` (upgraded).** Native `<select>` unchanged.
Custom dropdowns become a state machine: click trigger → quiesce → diff for
revealed elements → match option by text (exact > prefix > substring, on
name/text_head) → click match → verify via diff. Ambiguity returns the
candidate list (exit 2), never guesses. If nothing revealed, report honestly.

**`fill-form <sid> --data '<json>'`.** Caller supplies `{field: value}`;
field keys match against element accessible names/placeholders/labels
(case-insensitive; exact > prefix > substring). Values: strings fill
text/textarea/native-select (by option label); booleans check/uncheck;
radio groups match value against the option's label. Per-field execution with
per-field outcome lines, ONE final observation + diff, unmatched keys error
listing the available field names. Custom dropdown fields delegate to the
select machine.

**`search <query> [--in <@ref|css>] [--pick <text>] [--no-submit]`.** Find the
search box (explicit `--in`, else role=searchbox / input[type=search] /
placeholder~="search", ambiguity → candidates); fill; quiesce; if suggestions
appeared and `--pick` matches one, click it; else press Enter. Result is the
usual navigation/partial diff plus a `search:` line saying what was done.

Output contract addition (FROZEN once shipped): compound verbs print one
`STEP`-line per internal action before the final diff, e.g.
```
FILL-FORM s2 (6 fields)
  ✓ Email address = "jay@example.com"
  ✓ Country = "Canada"            (native select)
  ✗ Phone — no matching field (have: Full name, Email address, …)
CLICK Create account → partial change
~ s2: new text: "Account created!"
```

Tests: fixtures (form.html, dropdown.html incl. ambiguity/no-match paths) +
real sites: cars.com make/model dropdowns, bestbuy.com search w/ suggestions,
recreation.gov search, traderjoes.com store selector.
Done when: the form.html task takes 1 command instead of 6, and all four real
site flows complete without falling back to atomic verbs.

## R2 — List/table querying

`query <sid> [--filter <substr>] [--cols a,b,c] [--cursor N] [--limit N]
[--sort <col>]` over list/table sections:
- **Schema inference**: per-item fields from repeated structure (table headers;
  card sub-parts classified as title/link/price/rating/badge by cheap
  patterns). Reference: `list_item_structure`, `table_headers`.
- Deterministic `--filter` is substring/regex over item text; `--sort` uses
  detected sort state for tables (`get_table_sort` reference) or client-side
  sort of parsed values.
- Output: compact table of matching items with refs, plus
  `matched 12 of 342 items (shown 12)`.
- `--llm-filter "<criterion>"` (optional, sidecar): chunked relevance filtering
  for semantic criteria — the one place extraction returns.
Real-site tests: bestbuy results grid, cars.com listings, drugs.com A–Z index,
akc.org breed list.
Done when: "find the cheapest espresso machine under $100" needs query + one
click on list.html AND on a bestbuy results page.

## R3 — MCP server

`ebrowse mcp` serving stdio MCP against the same daemon session. Tool schemas
generated from the argparse definitions (single source, surfaces can't drift):
tools `browse_open/outline/expand/act/query/screenshot` where `act` multiplexes
action verbs (keeps the tool list small for schema-token economy). Text
outputs verbatim from the §4 renderers. Screenshot returns MCP image content.
Done when: Claude Code with only the MCP server configured completes the
fixture form task; README documents both Bash-CLI and MCP integration.

## R4 — VLM captions & @img refs

- Images get stable refs (`@img3`) in expand output; `screenshot --ref @img3`.
- Captions via the multimodal sidecar for images ≥80×80 CSS px in *expanded*
  sections only (never during outline), cached by src-hash
  (`captions` table already exists). Rendered as `![≈caption](@img3)`.
- List-section screenshot summarization (WebChallenger trick) when a list's
  text digest exceeds budget: one clipped screenshot → one-line summary.
Real-site tests: traderjoes product tiles, adoptapet pet cards (alt-less
images are the target case).

## R5 — Passive site memory

Persist per-origin: `~/.cache/ebrowse/sites/<host>.db` storing
(fingerprint, content_hash → summary) plus (URL-template → known PageMem
skeleton) and element behaviors observed during normal use (dropdown contents
after first open, link destinations after first click).
- On revisit: summaries prefill instantly (already works via content-hash
  cache — this adds *cross-session* URL/template awareness), outline gains
  `known: …` hints for elements whose behavior was recorded
  (`[Sort ▾ (@e4) — opens: Relevance | Price | Rating]`).
- Template matching by fingerprint multiset (reference: `pages_match`,
  `matching_list_page`) so /product/123 and /product/456 share knowledge.
- No crawling; memory accrues from use. Strictly additive to outputs.
Done when: second visit to a bestbuy product page (different product) shows
dropdown contents before any click.

## R6 — Explore crawler (active site memory)

`ebrowse explore <origin> [--depth N --pages M --budget-min T]` — the
WebChallenger offline exploration, adapted: GET-navigation only by default
(click exploration limited to elements whose diff stays same-page and
reverts — dropdowns, tabs, accordions; no form submits, no destructive verbs),
template dedupe, resumable, writes the R5 store + a bookmarks list that
`outline` surfaces as `known pages:` hints on task-relevant matches.
Needs its own mini-design (auth walls, robots/rate etiquette, budget UX)
before implementation — treat this section as a placeholder, not a spec.

## R7 — Evaluation harness

- **Token A/B**: scripted task set (10 Online-Mind2Web tasks) driven by a
  capable model twice — ebrowse vs. plain aria-snapshot tool — measuring
  success, steps, total tokens. Reuses paper infra where possible.
- **WebArena-lite** run for a directly comparable number to WebChallenger's
  58.8% (requires the simulation servers; optional).
- Regression mode: nightly-able script asserting outline quality metrics
  (section counts, ratio bounds) across the smoke-site list, catching site
  redesigns and splitter regressions.

## R8+ — Opportunistic / as-needed

- **Stealth tier**: camoufox or patchright backend for sites that block even
  full-chromium headless (apartments.com); config `browser.engine`.
- **Cross-origin iframes via CDP** (OOPIF walk) when a task actually needs it.
- **Downloads & uploads**: download event capture → `saved <path>` notes.
- **Network/console observation verbs** (`ebrowse console`, `ebrowse requests
  --filter api`) for debugging-style tasks.
- **`read` verb**: article-mode extraction (agent-browser parity) for
  read-only research tasks — cheaper than expand for prose pages.
- **Parallel sessions ergonomics**: `--session` already works; add
  `ebrowse sessions` overview and per-session config overrides.
- **Windows/macOS support pass** (socket path, profile locations).

---

## Standing engineering rules for all roadmap work

1. Fixture test first, then **validate on ≥2 real Online-Mind2Web sites**
   before calling a phase done; add findings to IMPLEMENTATION_LOG.md.
2. New output formats get golden tests + a §4-style spec block in this file
   *before* implementation freezes them.
3. Compound behavior must degrade to atomic verbs with an actionable error —
   never a silent wrong guess.
4. `scripts/smoke_real_sites.py` grows a check per phase (e.g. R1: known
   dropdown on a known site opens and matches).
