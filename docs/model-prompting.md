# Model Prompting Notes

Empirical, dated findings on how specific summarizer/vision models behave with
specific prompts and parameters — the lab notebook behind the sidecar defaults.

Determinism-first (architecture principle 1) means everything here is *tuning*,
never load-bearing: each default these notes inform degrades gracefully when the
sidecar is absent or a different model is swapped in. Model-specific knobs live
in config, not code — see [configuration.md](configuration.md) `summarizer.extra_body`.
This doc records *why* a given value is set.

## Conventions

- Organized **by model**; newest experiment first within each model.
- Always record the exact model id, server, date, and measured numbers
  (tokens in/out, latency, `finish_reason`) so results stay reproducible and
  cross-model comparisons stay honest.
- When an experiment changes a default, link the config key or file it landed in.

### Logging template

```
### <YYYY-MM-DD> — <short title>
- **Setup:** endpoint, params, input (page/image + size)
- **Measured:** tokens in/out, latency, finish_reason
- **Finding:** what we learned
- **Landed:** config/code change, or "reference only"
```

---

## `unsloth/Qwen3.6-35B-A3B-MTP`

Local llama.cpp server, OpenAI-compatible, `http://127.0.0.1:5001/v1`. Multimodal
**reasoning** model; the default summarizer + vision backend. Headline: reasoning
is pure overhead for the mechanical labeling/vision-gist tasks ebrowse uses it
for — disable it.

### 2026-07-08 — Reasoning control & text-summary budget

- **Setup:** `/chat/completions`, section-labeling JSON prompt; thinking on vs off.
- **Measured:**
  - Reasoning overhead is large and near-constant: a 2-section prompt emitted
    ~7,100 chars (~1,900 tokens) of `reasoning_content` before ~50 tokens of JSON.
    At `max_tokens` 60/240 the whole budget is consumed by thinking →
    `finish_reason: length`, empty `content` (this was the `0/N` summary bug).
  - `chat_template_kwargs: {enable_thinking: false}` → reasoning 0, clean JSON in
    ~80 tokens. ✅ The only switch that works in this build.
  - `reasoning_budget: 0` (top-level *or* inside `chat_template_kwargs`) → ignored. ❌
  - `/no_think` in the prompt → ignored; thinks *more* (1,224 chars). ❌
  - End-to-end outline, live 23-section Amazon page: reasoning **on** 29.3s,
    13/23 sections labeled; reasoning **off** 4.3s, 23/23. ~7× faster, full coverage.
  - Isolated fixture (2-section page, warm server): 11.9s vs 0.6s (~20×).
- **Finding:** disable thinking for the summarizer via
  `chat_template_kwargs.enable_thinking=false`; nothing else in this build works.
- **Landed:** `summarizer.extra_body` passthrough + per-provider recipes
  ([configuration.md](configuration.md)); deterministic safety net (output-token
  floor for reasoning overhead + tolerant JSON parse that salvages complete rows
  from a truncated array) so a partial response degrades to partial coverage
  rather than 0/N. See CHANGELOG Unreleased.

### 2026-07-08 — Vision "visual gist" prompts (screenshot → text summary)

Idea: a cheap local VLM summary as a routing tier between page text and the full
screenshot, so the main agent can decide whether the pixels are worth ingesting.

- **Setup:** 1850×966 PNG (maximized CDP Chrome, Amazon search results),
  thinking off, `temperature 0`.
- **Measured:**
  - **Image input cost: ~1,742 tokens** (image+text `prompt_tokens` 1757 minus
    text-only 15). Cf. ~2,380 for the same image to Claude hi-res (Opus 4.8).
  - Latency 2.0–3.5s per summary.
  - Prompt angle → output tokens: gist/1-line **26** · layout/regions **95** ·
    salient/attention **208** · visual-only-not-in-DOM **160** ·
    actionable-state **42** · refined default **77**.
- **Finding:**
  - Cheap routing tier: the *main agent* reads ~40–80 summary tokens instead of
    the ~2,380-token raw screenshot — a 30–60× cut — with the ~1,742-token
    perception cost absorbed by the free local model.
  - Highest-value angle is "what the page TEXT can't convey" (product photos,
    colors, spatial layout, what looks clickable).
  - **Hallucination risk:** open "what stands out?" prompts drift into
    *typical-but-absent* elements ("cookie notice — not shown, but typical";
    possibly-invented swatch colors). A hard constraint — *"describe only what you
    can literally see; never guess at elements that are typical but not shown"* —
    measurably removes the drift and correctly reports "no popups visible".
  - Per principle 1, any shipped visual gist must carry its own untrusted-VLM
    provenance marker and be a routing signal only, never extracted data.
- **Landed:** reference only (feature not built). Recommended default prompt:

  > You are the visual sense of a browsing agent that already has the page's
  > TEXT. In ≤45 words, tell it only what the TEXT cannot: imagery/product
  > photos, colors, charts, spatial layout, and any prominent banner/popup/modal
  > actually visible right now. Describe ONLY what you can literally see; never
  > guess at elements that are typical but not shown.

### 2026-07-08 — Visual gist shipped: synchronous `◉` + `describe-screen`

Follow-up to the prompt study above — the feature was built (ADR 0008). Re-tested
the shipped prompt on live pages (Best Buy, Trader Joe's, example.com) with the
same local stack (Qwen3.6-35B, thinking off, viewport 1280-wide).

- **Measured (viewport screenshot ~1,617 image tokens in):** gist out **14–33
  tok**, the shipped anti-speculation default **66–121 tok**, an overlay-only
  prompt **3–7 tok**. Latency ~2–3s. First synchronous `outline` ≈ a few seconds
  (text batch + glance concurrent); cached revisit **0.07s** (no VLM call).
- **Findings on real pages:**
  - No drift observed with the hard "only what is visible" clause. Trader Joe's
    correctly surfaced *both* a hero overlay and a cookie banner; the overlay-only
    prompt returned just `cookie banner`.
  - **Headline case:** Best Buy served a country-selection interstitial
    (Canada/US, no products). The DOM outline looked like a sparse picker with no
    hint it's a wall; the gist said so in one line. This "the outline is lying to
    you" signal is the strongest justification for default-on.
  - example.com gist: *"…centered heading 'Example Domain'…a 'Learn more' link; no
    overlays, modals, popups, or interstitials are present."* — grounded, flags
    the empty-overlay state, ~35 tok.
- **Shipped prompt** (`summarize/batch.py:_GLANCE_PROMPT`) leans on "1-2
  sentences", overlay/modal/interstitial flagging, and the anti-speculation
  clause. Output ceiling 500 tok (headroom; concise prompt emits ~1-2 sentences).
  `describe-screen` custom prompts use a 4096-tok ceiling for exhaustive detail.
- **Token-economy note:** the `◉` *output* is the one visual cost the MAIN agent
  pays (per outline); the ~1.6k image tokens are sidecar-side. So the default
  stays terse; verbosity is opt-in via `describe-screen`.
