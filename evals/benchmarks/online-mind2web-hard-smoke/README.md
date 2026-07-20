# Online-Mind2Web hard smoke

Twenty hard-difficulty tasks copied verbatim from
`Online_Mind2Web_(updated).json`. This is a trace-quality smoke run, not an
officially scored benchmark: tasks intentionally have no evaluator.

The sites were checked on 2026-07-18 with local, non-stealth ebrowse Chromium.
Each selected homepage produced a real page title and usable DOM outline.
Cars.com and SourceForge stopped at Cloudflare challenges; Apartments.com
returned Akamai Access Denied, so tasks on those sites were excluded.

Run the full overnight slice with the provider/model configured in
`experiments/.env`:

```bash
uv run ebrowse-eval run evals/benchmarks/online-mind2web-hard-smoke \
  --tool ebrowse --worktree --jobs 2 \
  --runs-dir runs/online-mind2web-hard-smoke \
  --name qwen-hard-smoke
```

Run a quick seeded sample first with `--sample 1 --seed 20260718`.
