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
1. ebrowse open <url>            → LANDS on the page (prints final url + title)
2. ebrowse outline               → the page OUTLINE (a table of contents)
3. ebrowse expand <sid>          → read ONE section as markdown with @refs
4. ebrowse click/fill/... @ref   → act; read the returned DIFF
5. repeat 3–4. After a navigation, run `outline` again to read the new page.
```

`open`, `back`, and click-throughs return a one-line landing (url + title), NOT
the page — run `outline` to read it. This keeps the (LLM-backed) outline opt-in
and lets the page finish loading first. Your `@refs` still work across the jump.

Example outline:

```
PAGE Espresso Gear — https://shop.example/search?q=grinder
◉ Product grid of coffee grinders; filter sidebar on the left. No modals or popups visible.
s2 form  6 inputs, 1 button  ~46t  ≈ Product filter form: brand, price, rating
│  │     │                   │     └ label. ≈ = model-written, | = verbatim page text
│  │     │                   └ token cost of expanding this section
│  │     └ interactive element counts
│  └ section type (nav/header/footer/form/list/table/dialog/content/media/iframe)
└ section id — use with expand/screenshot/scroll
```

Trust markers are hints only, never ground truth: `≈` = model-paraphrased text,
`◉` = the local vision model's read of the *screenshot* (even weaker — a routing
signal for "is it worth looking at the pixels?", e.g. it flags an overlay or
interstitial the text can't show). The expanded markdown and element states are
the DOM truth. The `◉` line appears only when a vision sidecar is running.

## Refs (@eN) — durable element handles

- Every interactive element gets a ref like `@e12`, shown in expand output and
  diffs: `[Add to cart (@e15)]`, `[Email (@e6: empty, required)]`.
- Refs are **durable**: they survive re-observation and even navigation. A
  site's header search box keeps its ref on every page. Act without re-reading.
- If a ref stops resolving you get `stale ref @e12 … — run 'ebrowse outline'`
  (exit 2). Just re-outline and re-expand; never guess refs. The same error
  (`… now resolves to a different element`) fires when the page reordered
  look-alike elements under your ref — the action was refused BEFORE touching
  the wrong element, so nothing happened; re-outline and act on fresh refs.
- `disabled` / `inert` after a ref — `[Place order (@e9) disabled]` — means the
  control exists but can't be used yet: something must enable it first (fill a
  required field, close a modal). Clicking it fails fast telling you so; when
  another action enables it, the diff shows `~ @e9 disabled: "true" → "false"`.
- A `?` inside the ref parens — `[Save changes (@e4 ?)]` — marks a **candidate**:
  a custom widget discovered from weak evidence (a real JS listener, `tabindex`,
  or ARIA state) rather than proven control semantics. Click it like any ref,
  but if nothing changes it may be decorative — don't keep retrying. Candidates
  show only in expand, never in outline counts.
- CSS selectors also work anywhere a ref does: `ebrowse click "#submit"`.

## Actions return diffs — read them, don't re-snapshot

```
$ ebrowse click @e4
CLICK @e4 (button "Sort by: Relevance") → partial change
+ s2: [Relevance (@e7)], [Price: low to high (@e8)], [Price: high to low (@e9)]
~ @e4 expanded: "false" → "true"
```

Diff vocabulary:
- `→ navigation` — the action moved you to a new page; you get a landing line
  (`now at <url> · "title"`), not the page. Run `outline` to read it. Your
  durable `@refs` still resolve, so you can act on known chrome without it.
- `→ partial change` — same page; `+` added elements (with ready-to-use refs),
  `-` removed, `~` state/text changes. `~ s2: new text: "Account created!"`
  quotes what a status message/validation error now says — short status-like
  fragments are quoted first, bulk insertions are capped and elided as
  `start … end`. Sections you have `expand`ed on the current page get a much
  larger quote budget, so expand a section you're watching to see its text
  changes near-verbatim in later diffs.
- A no-change hover may warn that the target never acquired `:hover`. Treat
  that as degraded browser input delivery: run `ebrowse daemon stop`, retry
  once with the fresh daemon, and don't burn turns repeating the dead hover.
- `→ dialog` — an in-page DOM dialog appeared. If it's its own section, its full
  content is expanded right there in the diff; if it was folded into a section,
  its controls show as `+ sN [dialog]: [Accept (@e6)] …`. Interact with its
  controls as the dialog requires (accept / close / fill / submit — not always a
  simple accept). If it's modal, clicks elsewhere are blocked (`covered by …`)
  until you resolve it, so handle it first.
- `→ dialog opened (blocking)` — your action opened a native `confirm`/`prompt`
  that now blocks the whole page. Nothing else works until you resolve it:
  `ebrowse dialog accept` (or `dialog accept "text"` to answer a prompt) /
  `ebrowse dialog dismiss`. Resolving prints what your original action changed.
- `→ no change detected` — the action had no visible DOM effect. It may have
  been a real no-op or an animation slower than the settle window. Check
  `ebrowse outline` or a screenshot before retrying.
- `note: clicked/checked via the associated label (…)` — the control's own
  click point is covered by decoration inside its `<label>` (restyled
  radios/checkboxes), so `click`/`check`/`uncheck` was routed through the
  label — the browser-defined equivalent. The action succeeded normally
  (`check`/`uncheck` verify the resulting state); nothing to do.
- `note: pointer route blocked by …; activated via keyboard` — a non-modal
  overlay covers a native control, so the click completed as trusted
  focus + Enter/Space (what a keyboard user does; never used when a
  dialog/inert modal guards the target). The action succeeded — but the
  overlay is still on screen; deal with it if it also covers what you need
  next.
- `note: native alert auto-accepted: "…"` — `alert`/`beforeunload` carry no
  decision, so they're accepted automatically and reported; you never dismiss them.
  `confirm`/`prompt` are yours to decide (see `→ dialog opened` above).
- `a native confirm dialog is blocking this tab …` (exit 1) — you tried a page
  verb while a dialog is pending. Resolve it with `ebrowse dialog accept|dismiss`,
  or `ebrowse tab <n>` to switch to an unblocked tab.
- `blocked: @e42 is covered by …` (exit 1) — an overlay intercepts the click.
  The message names the best next step it could verify:
  `— dismiss or interact with @eN (…) first` (the cover is itself exposed: act
  on that ref); `— a dialog is open (…); resolve it first` (use the dialog's
  controls — run `ebrowse outline` if you haven't seen it); or
  `…, which has no exposed ref (likely a new overlay)` (re-run
  `ebrowse outline`; if nothing new appears, try `ebrowse press Escape` or
  `ebrowse screenshot` to see what's on top). Follow the named step rather
  than retrying the same click.
- `blocked: a modal is open ("…") and is intercepting the click` (exit 1) — a
  modal is blocking the page even though it isn't visually over your target
  (native `showModal()` / focus-trap). Don't retry the same click — resolve or
  dismiss the named modal first.

## Verbs

```
open <url>            navigate (alias goto); back / forward / reload  → landing line
outline [--no-summaries|--no-glance|--refresh]   read the page (table of contents)
describe-screen [prompt]                   ask the local vision model about the screen
expand <sid|@ref> [--cursor N] [--all]     lists/tables paginate; follow the
                                           "… N more items — expand s4 --cursor 20" hint;
                                           on a <select> ref: pages through ITS options
                                           ("… 300 more options — expand @e5 --cursor 50")
click <t> [--double|--right|--new-tab]     t = @ref or CSS selector
fill <t> <text>       clear + type          type <t> <text> [--enter]
hover <t>             reveal hover menus (mouse stays; warns if delivery looks dead)
drag <src> <dst>      drag one element onto another (sortables, HTML5 dnd)
press <keys>          e.g. Enter, Control+a, Escape
check/uncheck <t>     select <t> <label>…   native <select> (several labels for
                                            <select multiple>) AND custom dropdowns
scroll down|up [--pages N] | scroll <sid|@ref>          window / bring into view
scroll <sid|@ref> down|up [--pages N]    scroll INSIDE the panel at/above the
                                         target (virtualized lists, modal bodies);
                                         expand marks such panels: "(inner
                                         scrollable panel: … 'ebrowse scroll s3
                                         down' scrolls it)"
diagnose <t>          read-only: would a click land? names the blocker + recovery
upload <t> <files>    eval <js>             get text|value|attr|html|title|url [t]
fill-form <sid> --data '{"Field": "value", "Agree": true}'   many fields, one diff
search <query> [--in @ref] [--pick <text>] [--no-submit]     find box, type, submit
query <sid> [--filter <regex>] [--cols a,b] [--limit N]      filter list/table rows
screenshot [--section s3|--ref @e5|--full] [-o path]
dialog accept [text] | dialog dismiss | dialog status   resolve a blocking native confirm/prompt
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

**Long lists/tables:** `expand` shows a 20-item page — page through it with
`--cursor N` (follow the "… N more — expand s4 --cursor 20" hint), or `--all` for
everything (rarely; the outline row shows item count and token cost). To *filter*
or *cap* rows instead, use `query`: `ebrowse query s4 --filter "under.*100"
--limit 5` (regex over row text), `--cols "name,price"` projects table columns.
The flags don't cross: `--filter/--cols/--limit` are `query`-only; `expand` takes
only `--cursor/--all`. To see just the first item, `expand s4` already shows it.

**Cookie banners / modals:** an appearing dialog shows in the action diff with
its controls' `@refs` ready — a substantial one as its own `dialog` section
expanded inline, a coalesced one tagged `+ sN [dialog]: [Accept (@e6)] …`. Do
whatever the dialog needs (accept, close, fill, submit) — not always a plain
accept. A modal blocks other clicks (`covered by …`) until you resolve it.

**Native dialogs (`confirm`/`prompt`):** an action that pops one returns
`→ dialog opened (blocking)` and freezes the page. Decide, then run
`ebrowse dialog accept` / `dialog accept "your answer"` (prompt) / `dialog dismiss`;
that unblocks the page and prints what your action changed. `dialog status` shows
the pending message. (`alert`/`beforeunload` are auto-accepted — nothing to do.)

**Sites blocking the headless browser** ("Access Denied" on open): attach to a
real Chrome instead — start it with `--remote-debugging-port=9222`, then
`ebrowse connect 9222`.

**Images:** expand output shows `![alt](@i3)` for big images; alt-less ones
get VLM captions `![≈caption](@i3)` when the sidecar is up. See one with
`ebrowse screenshot --ref @i3`. `@i` refs are per-observation (not durable).

**Seeing without a screenshot (`describe-screen`):** ask the local vision model
about the current screen and get back text, not a 2.4k-token image. No argument →
a concise gist (same as the outline `◉` line). With a question → anything visual:
`ebrowse describe-screen "is there a cookie banner or overlay covering the page?"`,
`describe-screen "what color is the selected size?"`, `describe-screen "transcribe
the prices in the grid"`. It's the cheap middle tier between the page text and
`screenshot`. Untrusted (a model's read of pixels) — use it to decide whether to
look, not as data to act on.

**When lost:** `ebrowse outline` (cheap), `ebrowse describe-screen` (cheap visual
gist), or `ebrowse screenshot` and look yourself. `ebrowse doctor` diagnoses
environment problems; daemon log: `~/.cache/ebrowse/daemon.log`.

## What NOT to do

- Don't re-run `outline` after every action — the diff already told you what
  changed. Outline costs are small but nonzero.
- Don't parse refs from old turns after a `stale ref` error; re-expand.
- Don't use `eval` for things a verb does — verbs produce diffs, eval output
  is on you to interpret.
- Don't expand every section "to be safe". Outline labels + counts + token
  sizes exist so you can choose.
