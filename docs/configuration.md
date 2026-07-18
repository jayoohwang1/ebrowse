# Configuration

Config file: `~/.config/ebrowse/config.toml` (respects `XDG_CONFIG_HOME`).
Precedence: built-in defaults < config.toml < `EBROWSE_*` env vars.
Every key has a default; unknown keys warn, never fail. Loader: `src/ebrowse/config.py`.

Env override mapping is generic: `EBROWSE_<SECTION>_<KEY>`, e.g.
`EBROWSE_SUMMARIZER_BASE_URL`, `EBROWSE_BROWSER_HEADLESS=false`. Lists are
comma-separated; booleans accept `1/true/yes/on`.

```toml
[daemon]
idle_shutdown_minutes = 30      # daemon exits after this long without commands

[browser]
mode = "launch"                 # "launch" | "cdp"
headless = true
cdp_url = ""                    # used when mode = "cdp" or set via `ebrowse connect`
profile_dir = ""                # default: ~/.cache/ebrowse/profiles/<session>
viewport = [1280, 1280]

[summarizer]
enabled = true                  # degrades to deterministic labels if unreachable
base_url = "http://127.0.0.1:5001/v1"   # any OpenAI-compatible server
model = "default"               # llama.cpp ignores; set for multi-model servers
api_key = ""
vision = true                   # image captions on expand + visual glance (needs a multimodal model)
glance = true                   # auto ◉ visual-gist line on the outline (needs vision + reachable model)
max_input_tokens = 100000       # input budget per page summarization call
timeout_s = 60                  # client-wide default; describe-screen overrides it
sync_timeout_s = 30             # hard deadline for the synchronous outline enrichment (summaries + glance)
describe_max_tokens = 4096      # ceiling for a manual `describe-screen` answer
describe_timeout_s = 180        # `describe-screen` deadline (patient; long detailed answers)
# extra_body: merged verbatim into every /chat/completions request. Model/
# provider knobs live here as config, not code (see below). Default: {}.
[summarizer.extra_body]
# chat_template_kwargs = { enable_thinking = false }   # llama.cpp + Qwen: reasoning off

[observe]
quiescence_ms = 300             # post-action DOM-quiet debounce
quiescence_max_ms = 3000        # hard cap on the settle wait
preview_chars = 120             # deterministic label preview length
list_page_size = 20             # expand/query pagination window
max_sections = 60               # soft outline-size target; safe merges only
max_section_tokens = 16384      # ordinary expansion ceiling; collection page budget

[security]
allowed_domains = []            # empty = all; subdomains of listed domains allowed

[debug]
log = ""                        # JSONL debug-event log path; "" = off (default, zero overhead)
```

## Debug event log

`[debug] log` (env: `EBROWSE_DEBUG_LOG`) enables the tier-1 structured event
channel: every daemon request appends its internal events (phase timings,
snapshot/diff/locate facts, anomalies) as JSONL to the given path — one object
per line, shape `{request_id, module, event, level, fields, ts, mono}`. A
literal `{session}` in the path is replaced with the session name. Off by
default: no file is created and instrumentation costs nothing. The per-call env
var `EBROWSE_REQUEST_ID` (read by the CLI when building a request) lets a
harness choose the request id echoed in the response and stamped on the events.
See docs/architecture.md ("Debug event channel") and ADR 0013.

## Filesystem locations

| Path | Purpose |
|---|---|
| `$XDG_RUNTIME_DIR/ebrowse.sock` (fallback `~/.cache/ebrowse/`) | daemon unix socket |
| `~/.cache/ebrowse/daemon.log` | daemon log (rotated, 5 MB × 2) |
| `~/.cache/ebrowse/daemon.pid` | daemon pidfile |
| `~/.cache/ebrowse/profiles/<session>/` | persistent browser profiles (logins survive restarts) |
| `~/.cache/ebrowse/summaries.db` | sqlite summary + caption + screen-gist cache (pruned past 50k rows) |

## Summarizer behavior

- **Synchronous, on the `outline` verb only.** `outline` fills both the `≈`
  section summaries and the `◉` visual glance before it returns, under a hard
  `sync_timeout_s` deadline (text + glance run concurrently). Navigation and
  actions never run the summarizer — they return a landing line / diff, so the
  sidecar's latency is paid only when the agent explicitly reads the page.
- **One batched call per page** for text: per-section digests in, strict JSON
  `{sid: one-line summary}` out. Results are cached by section `content_hash`, so
  changed content is structurally a cache miss — there is no separate invalidation.
- **Visual glance (`◉`)**: one VLM call on a viewport screenshot, cached by a
  key derived from the page's DOM structure (`screens` table), so revisiting the
  same page state is an instant cache hit (no screenshot, no VLM). Set
  `glance = false` to drop the auto line while keeping `describe-screen`.
  Cost note: glance sends a ~1.6k-token image per newly-seen page. That is free
  on a local sidecar (the intended setup) but real money against a *paid*
  multimodal API — set `glance = false` there, or point `base_url` at a local model.
- **Never load-bearing.** On timeout/failure the outline renders deterministic
  labels and no `◉` line (a status note says so). A genuine timeout counts toward
  the circuit breaker: 3 consecutive failures disable the summarizer for 10
  minutes; a dead/slow server costs nothing after the breaker opens.
- **`describe-screen`** is the patient, agent-initiated path: a free-form visual
  query with its own generous `describe_max_tokens` / `describe_timeout_s`
  (bounded above by the daemon's per-verb ceiling and the client transport
  timeout — raise those in tandem if you push `describe_timeout_s` past ~200s).
- Injection hygiene: text summaries are length-clamped (140 chars),
  control-stripped, and `(@eN)` tokens removed; visual gists are control-stripped
  and newline-collapsed. The `≈` / `◉` provenance markers are added by the
  renderer — model output only ever fills label text, never structure.

### Provider-specific knobs (`extra_body`)

ebrowse hard-codes no per-provider logic — the summarizer only speaks the
OpenAI `/chat/completions` shape. Anything a specific backend needs goes in
`[summarizer.extra_body]` as config data, merged verbatim into each request
(after ebrowse's own fields, so it can override them). Leave it empty unless you
know your backend supports the field — some servers reject unknown body keys.

Reasoning models are the common case: section labeling needs no reasoning, and
the hidden thinking both slows the call and can blow the output budget. Recipes:

| Backend | `extra_body` |
|---|---|
| llama.cpp / Qwen (reasoning off) | `chat_template_kwargs = { enable_thinking = false }` |
| OpenAI reasoning models | `reasoning_effort = "low"` |

Env form (JSON): `EBROWSE_SUMMARIZER_EXTRA_BODY='{"chat_template_kwargs":{"enable_thinking":false}}'`.
