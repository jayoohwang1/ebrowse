# ebrowse

Token-efficient browser control for AI agents. A daemon-backed CLI that shows web
pages as **skimmable section outlines**, lets you **expand only the sections you
need** (as markdown with stable element refs), and answers every action with a
**diff of what changed** instead of a full page snapshot.

> **Status: v0.1.0.** Navigation, observation, actions-with-diffs, compound
> verbs, query, an MCP server, and optional LLM section labels/captions all
> work end-to-end, validated on real sites — see [CHANGELOG.md](CHANGELOG.md).
> Planned work is tracked in
> [GitHub issues](https://github.com/jayoohwang1/ebrowse/issues).
>
> Measured on real pages: outlines are 1–9% of the token cost of a full aria
> snapshot on large pages (recreation.gov: 725 vs 7,924 tokens).

## Install (dev)

```bash
make setup          # uv sync + playwright chromium
make test           # pure tests (fast, no browser)
make test-browser   # + headless-chromium and e2e CLI tests
```

## Usage

The first command autostarts a background daemon that owns the browser; state
(page, refs, logins) persists between CLI calls.

```bash
$ ebrowse open shop.example.com          # navigate; lands on the page
opened http://shop.example.com/  ·  "Espresso Gear — Fixture Shop"
run 'ebrowse outline' to read the page

$ ebrowse outline                        # read the page (table of contents)
PAGE Espresso Gear — Fixture Shop — http://shop.example.com/
s1 header  4 links, 1 input, 1 button  ~42t  | "Fixture Shop Products Deals Help Search"
s2 form    6 inputs, 1 button          ~46t  | "Filters — Bella Breville Gaggia 4★ & up"
s4 list    32 items, 32 links          ~1.0kt | "Bella Espresso Machine Pro $19.99 ..."

$ ebrowse expand s2                      # one section as markdown with refs
## s2 form — Filters
[ ] Bella (@e7) [ ] Breville (@e8) ...
[Min (@e10: empty)] [Max (@e11: empty)] [Apply (@e13)]

$ ebrowse expand s4 --cursor 20          # long lists paginate
$ ebrowse screenshot --section s4        # clipped PNG of one section
$ ebrowse describe-screen "any overlay?" # cheap visual gist from a local VLM
$ ebrowse get value @e10                 # small getters: text/value/attr/title/url
$ ebrowse back                           # history nav; lands (run outline to read)
$ ebrowse tabs && ebrowse tab 1          # tab management
$ ebrowse close                          # close this session's browser
```

Navigation (`open`/`back`/click-throughs) returns a one-line landing, not the
page — run `outline` to read it (your `@refs` survive the jump).

**Working verbs:** `open/goto, back, forward, reload, outline, describe-screen,
expand, screenshot, get, tabs, tab, dialog, connect, close, daemon status|stop, doctor`;
actions `click, fill, type, press, check, uncheck, select, scroll, upload,
eval`; compound verbs `fill-form, search` (and `select` handles custom
dropdowns) that collapse multi-step interactions into one command with one
diff. Every action prints a **diff of what changed**, e.g.:

```
$ ebrowse click @e4
CLICK @e4 (button "Sort by: Relevance") → partial change
+ s2: [Relevance (@e7)], [Price: low to high (@e8)], [Price: high to low (@e9)]
~ @e4 expanded: "false" → "true"

$ ebrowse click @e15
CLICK @e15 (button "Create account") → partial change
~ s2: new text: "Account created! Check your email."
```

Safety rails: clicks covered by an overlay/dialog fail fast naming the covering
element; native `alert`/`beforeunload` are auto-accepted and reported as notes,
while `confirm`/`prompt` are left for you to resolve with `ebrowse dialog
accept|dismiss` (they block the page until you do); actions whose effect can't be
seen in the DOM honestly report "no change detected".

Reading a page costs what you choose to read: skim the outline (~50–700
tokens), expand only relevant sections. Element refs (`@e7`) are durable — they
survive re-observation and even page navigations (a site's header search box
keeps its ref on every page), so you can act without re-reading.

### Sessions & browser modes

- `--session NAME` on any verb gives you an independent browser (default: `default`).
- Default mode launches a persistent headless Chromium (profile kept in
  `~/.cache/ebrowse/profiles/<session>` — logins survive restarts).
- `ebrowse connect 9222` attaches to a running Chrome started with
  `--remote-debugging-port=9222` instead (your real profile/logins; sites that
  block headless browsers work there too).

### Troubleshooting

`ebrowse doctor` checks python/chromium/socket/config/summarizer and prints fix
hints. Daemon logs: `~/.cache/ebrowse/daemon.log`.

## MCP server

`ebrowse mcp` speaks Model Context Protocol over stdio — point any MCP host at
it (command: `ebrowse`, args: `["mcp"]`). Seven tools (`browse_open`,
`browse_outline`, `browse_describe`, `browse_expand`, `browse_act`,
`browse_query`, `browse_screenshot`) return the same landing/outline/diff text as
the CLI; screenshots come back as images. The MCP process and the CLI share the
same daemon, so you can mix both against one browser.

## Dev harness (no daemon)

```bash
uv run python -m ebrowse.dev <url> outline
uv run python -m ebrowse.dev <url> expand s2
uv run python -m ebrowse.dev <url> stats          # token accounting vs aria snapshot
uv run python -m ebrowse.dev <url> capture out.json
```

## Configuration

`~/.config/ebrowse/config.toml`, overridable via `EBROWSE_<SECTION>_<KEY>` env
vars (e.g. `EBROWSE_BROWSER_HEADLESS=false`). See
[docs/configuration.md](docs/configuration.md) for all keys.
The optional summarizer points at any OpenAI-compatible server (default:
`http://127.0.0.1:5001/v1`, e.g. a local llama.cpp). When available, `outline`
upgrades section labels from verbatim page text (`|`) to model-written one-liners
(`≈`) and — with a multimodal model — adds a `◉` visual gist of the screenshot
(and enables `describe-screen`). Both are filled synchronously when you run
`outline`, cached in sqlite (cache hits are instant). Everything works without
it; a dead server costs nothing after the circuit breaker opens.

```
$ ebrowse outline
PAGE Espresso Gear — Fixture Shop — http://shop.example.com/
◉ A product grid of espresso machines with a filter sidebar. No modals or popups visible.
s2 form  6 inputs, 1 button  ~46t  ≈ Product filtering form: brand, price, rating
s4 list  32 items, 32 links  ~1.0kt ≈ Espresso gear products with prices and ratings
```

## Documentation

- [SKILL.md](SKILL.md) — **how agents should drive the tool** (operating loop, diff vocabulary, recipes).
- [AGENTS.md](AGENTS.md) — contributor guide (principles, layout, conventions).
- [docs/architecture.md](docs/architecture.md) — components, flows, accepted tradeoffs.
- [docs/output-contracts.md](docs/output-contracts.md) — the frozen output formats.
- [docs/configuration.md](docs/configuration.md) — every config key.
- [docs/adr/](docs/adr/README.md) — records of non-obvious design decisions.
- [CHANGELOG.md](CHANGELOG.md) — what shipped, per release.
