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
vision = true                   # image captions on expand (needs a multimodal model)
max_input_tokens = 100000       # input budget per page summarization call
timeout_s = 60
# extra_body: merged verbatim into every /chat/completions request. Model/
# provider knobs live here as config, not code (see below). Default: {}.
[summarizer.extra_body]
# chat_template_kwargs = { enable_thinking = false }   # llama.cpp + Qwen: reasoning off

[observe]
quiescence_ms = 300             # post-action DOM-quiet debounce
quiescence_max_ms = 3000        # hard cap on the settle wait
preview_chars = 120             # deterministic label preview length
list_page_size = 20             # expand/query pagination window
max_sections = 60               # outline overflow valve; tail sections merge

[security]
allowed_domains = []            # empty = all; subdomains of listed domains allowed
```

## Filesystem locations

| Path | Purpose |
|---|---|
| `$XDG_RUNTIME_DIR/ebrowse.sock` (fallback `~/.cache/ebrowse/`) | daemon unix socket |
| `~/.cache/ebrowse/daemon.log` | daemon log (rotated, 5 MB × 2) |
| `~/.cache/ebrowse/daemon.pid` | daemon pidfile |
| `~/.cache/ebrowse/profiles/<session>/` | persistent browser profiles (logins survive restarts) |
| `~/.cache/ebrowse/summaries.db` | sqlite summary + caption cache (pruned past 50k rows) |

## Summarizer behavior

- **One batched call per page**: per-section digests in, strict JSON
  `{sid: one-line summary}` out. Results are cached by section `content_hash`, so
  changed content is structurally a cache miss — there is no separate invalidation.
- Backfill runs as a background task outside the session lock; backfilled summaries
  appear on the *next* outline (emitted output is never mutated).
- Circuit breaker: 3 consecutive failures disable the summarizer for 10 minutes
  (outline shows `summaries: unavailable`). A dead server costs nothing after the
  breaker opens.
- Injection hygiene: model output is length-clamped (140 chars), control-stripped,
  and `(@eN)` tokens are removed; the `≈` provenance marker is added by the renderer.

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
