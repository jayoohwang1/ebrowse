# Online-Mind2Web hard (full subset)

All 79 hard-difficulty tasks, generated from the Online-Mind2Web dataset dump by
[`generate.py`](generate.py) — edit/re-run the generator on a new dataset drop
instead of hand-editing task.toml files. Dataset revisions reissue task ids with
a date suffix (e.g. `_070826`); the generator replaces the older directory.

Tags:
- `smoke` — the task's base id was part of the 20-task
  [hard-smoke](../online-mind2web-hard-smoke/README.md) batch (site verified
  reachable 2026-07-18).
- `blocked-site` — homepage stopped local non-stealth Chromium at a bot
  challenge (cars.com, sourceforge.net, apartments.com); expect policy/challenge
  failures rather than agent signal.

Same browser-only Pi policy and trace-oriented (unscored) setup as the smoke
benchmark — see its [README](../online-mind2web-hard-smoke/README.md) for the
policy details.

Overnight run, skipping bot-blocked sites, with the provider/model from
`experiments/.env`:

```bash
uv run ebrowse-eval run evals/benchmarks/online-mind2web-hard \
  --tag open --tool ebrowse --worktree --jobs 2 \
  --runs-dir runs/online-mind2web-hard --name qwen-hard
```

`--tag open` selects the 73 tasks on reachable sites (tag selection is
AND-only, so blocked/open are complementary positive tags).

Afterwards, annotate the batch for triage:

```bash
uv run ebrowse-eval annotate runs/online-mind2web-hard/qwen-hard-* 
uv run ebrowse-eval issues runs/online-mind2web-hard/<run-dir>
```
