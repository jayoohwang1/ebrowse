---
name: ebrowse
description: Token-efficient browser control. Use when browsing or automating web pages — navigating, reading pages, filling forms, clicking through flows. Pages render as skimmable section outlines; expand only what you need; every action returns a diff of what changed.
---

# Browsing with ebrowse

`ebrowse` is a CLI. Run it via shell. One background daemon owns the browser;
state (page, refs, logins) persists between commands. Everything costs what you
choose to read: outlines are ~50–700 tokens, a full page is never dumped unless
you ask.

## The operating loop

```
1. ebrowse open <url>            → prints the page OUTLINE (a table of contents)
2. ebrowse expand <sid>          → read ONE section as markdown with @refs
3. ebrowse click/fill/... @ref   → act; read the returned DIFF
4. repeat 2–3. Re-outline only after confusion — navigation prints it for you.
```

Example outline line:

```
s2 form  6 inputs, 1 button  ~46t  ≈ Product filter form: brand, price, rating
│  │     │                   │     └ label. ≈ = model-written, | = verbatim page text
│  │     │                   └ token cost of expanding this section
│  │     └ interactive element counts
│  └ section type (nav/header/footer/form/list/table/dialog/content/media/iframe)
└ section id — use with expand/screenshot/scroll
```

Trust `≈` labels as hints only — they are model-paraphrased page content. The
expanded markdown and element states are ground truth from the DOM.

## Refs (@eN) — durable element handles

- Every interactive element gets a ref like `@e12`, shown in expand output and
  diffs: `[Add to cart (@e15)]`, `[Email (@e6: empty, required)]`.
- Refs are **durable**: they survive re-observation and even navigation. A
  site's header search box keeps its ref on every page. Act without re-reading.
- If a ref stops resolving you get `stale ref @e12 … — run 'ebrowse outline'`
  (exit 2). Just re-outline and re-expand; never guess refs.
- CSS selectors also work anywhere a ref does: `ebrowse click "#submit"`.

## Actions return diffs — read them, don't re-snapshot

```
$ ebrowse click @e4
CLICK @e4 (button "Sort by: Relevance") → partial change
+ s2: [Relevance (@e7)], [Price: low to high (@e8)], [Price: high to low (@e9)]
~ @e4 expanded: "false" → "true"
```

Diff vocabulary:
- `→ navigation` — you're on a new page; the fresh outline follows immediately
  (sections marked `(unchanged)` are the same chrome as the previous page).
- `→ partial change` — same page; `+` added elements (with ready-to-use refs),
  `-` removed, `~` state/text changes. `~ s2: new text: "Account created!"`
  quotes what a status message/validation error now says.
- `→ dialog` — a modal appeared; deal with it first.
- `→ no change detected` — the action had no visible DOM effect. It may have
  been a real no-op or an animation slower than the settle window. Check
  `ebrowse outline` or a screenshot before retrying.
- `note: native confirm auto-accepted: "…"` — alerts/confirms are handled
  automatically and reported; you never need to dismiss them.
- `blocked: @e42 is covered by <dialog "Cookie consent">` (exit 1) — an overlay
  intercepts the click. Interact with the covering element first.

## Verbs

```
open <url>            navigate (alias goto); back / forward / reload
outline [--wait-summaries|--no-summaries|--refresh]
expand <sid|@ref> [--cursor N] [--all]     lists/tables paginate; follow the
                                           "… N more items — expand s4 --cursor 20" hint
click <t> [--double|--right|--new-tab]     t = @ref or CSS selector
fill <t> <text>       clear + type          type <t> <text> [--enter]
press <keys>          e.g. Enter, Control+a, Escape
check/uncheck <t>     select <t> <label>    native <select> AND custom dropdowns
                                            (opens, matches option text, clicks)
scroll down|up [--pages N] | scroll <sid|@ref>
upload <t> <files>    eval <js>             get text|value|attr|html|title|url [t]
fill-form <sid> --data '{"Field": "value", "Agree": true}'   many fields, one diff
search <query> [--in @ref] [--pick <text>] [--no-submit]     find box, type, submit
query <sid> [--filter <regex>] [--cols a,b] [--limit N]      filter list/table rows
screenshot [--section s3|--ref @e5|--full] [-o path]
tabs / tab <n>        close [--all]         daemon status|stop      doctor
```

Global flags: `--session NAME` (independent browser per name), `--json`,
`--timeout MS`.

## Recipes

**Fill a form:** one command — `ebrowse fill-form s2 --data '{"Email":
"a@b.c", "Country": "Canada", "Account type": "Business", "I agree": true}'`.
Keys match field labels/placeholders (exact > prefix > substring); strings fill
text fields and native selects, booleans check boxes, radio values match the
option label. Per-field ✓/✗ lines report what happened; unmatched keys list
the available fields. Then `click` the submit button — the diff quotes
success/validation text. Passwords display masked (`•••`); never echoed.

**Search a site:** `ebrowse search "espresso machine"` finds the search box,
types, and submits; `--pick "text"` clicks the matching suggestion instead;
`--in @ref` disambiguates when there are several boxes; `--no-submit` to peek
at suggestions first.

**Custom dropdowns:** `ebrowse select @e4 "Price Low to High"` works on
button-style dropdowns too — it opens them, matches the revealed option text,
and clicks it. No-match errors list every revealed option.

**Long lists/tables:** `ebrowse query s4 --filter "under.*100|\$[0-9]?[0-9]\."`
shows only matching rows (with refs, ready to click); `--cols "name,price"`
projects table columns. For sequential reading, `expand` pages with `--cursor`.
Never `--all` blindly — the outline row shows item count and token cost.

**Cookie banners / modals:** they appear as `dialog` sections or as the
covering element in a blocked-click error. Expand, click the accept/close
button, continue.

**Sites blocking the headless browser** ("Access Denied" on open): attach to a
real Chrome instead — start it with `--remote-debugging-port=9222`, then
`ebrowse connect 9222`.

**Images:** expand output shows `![alt](@i3)` for big images; alt-less ones
get VLM captions `![≈caption](@i3)` when the sidecar is up. See one with
`ebrowse screenshot --ref @i3`. `@i` refs are per-observation (not durable).

**When lost:** `ebrowse outline` (cheap), or `ebrowse screenshot` and look.
`ebrowse doctor` diagnoses environment problems; daemon log:
`~/.cache/ebrowse/daemon.log`.

## What NOT to do

- Don't re-run `outline` after every action — the diff already told you what
  changed. Outline costs are small but nonzero.
- Don't parse refs from old turns after a `stale ref` error; re-expand.
- Don't use `eval` for things a verb does — verbs produce diffs, eval output
  is on you to interpret.
- Don't expand every section "to be safe". Outline labels + counts + token
  sizes exist so you can choose.
