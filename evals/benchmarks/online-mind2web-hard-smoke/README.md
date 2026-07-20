# Online-Mind2Web hard smoke

Twenty hard-difficulty tasks copied verbatim from
`Online_Mind2Web_(updated).json`. This is a trace-quality smoke run, not an
officially scored benchmark: tasks intentionally have no evaluator.

The benchmark uses the browser-only Pi policy: one shell-free custom `ebrowse`
tool, the standard verb allowlist (`eval` and host/file escape verbs blocked), a
200-call limit, and redirect-aware task navigation. Before Pi starts, the harness
opens the trusted task URL, records a bounded public main-frame redirect chain,
then restarts with the observed domain scope frozen. A policy block is expected
experiment signal rather than a harness failure. Add a known login hostname with
`--allow-domain` only when a task legitimately needs one beyond startup redirects.

The sites were checked on 2026-07-18 with local, non-stealth ebrowse Chromium.
Each selected homepage produced a real page title and usable DOM outline.
Cars.com and SourceForge stopped at Cloudflare challenges; Apartments.com
returned Akamai Access Denied, so tasks on those sites were excluded.

The browser-only policy was validated on 2026-07-19 against GOV.UK and Best Buy,
including parallel local-Qwen runs, redirect metadata, and frozen-scope startup.

Run the full overnight slice with the provider/model configured in
`experiments/.env`:

```bash
uv run ebrowse-eval run evals/benchmarks/online-mind2web-hard-smoke \
  --tool ebrowse --worktree --jobs 2 \
  --runs-dir runs/online-mind2web-hard-smoke \
  --name qwen-hard-smoke
```

Run a quick seeded sample first with `--sample 1 --seed 20260718`.

The recorded setup is intentionally trace-oriented rather than official
Online-Mind2Web scoring: exact prompts, all Pi messages/results, per-call browser
state, screenshots, DOM snapshots, and policy errors are retained. See
[`pi-browser-policy.md`](../../docs/pi-browser-policy.md) for the complete boundary.
