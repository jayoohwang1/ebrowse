# Online-Mind2Web hard (full subset)

The hard-difficulty Online-Mind2Web tasks (75 of the 79 in the dataset; the 4
bestbuy.com tasks are dropped — see `EXCLUDED_HOSTS`), generated from the
dataset dump by [`generate.py`](generate.py) — edit/re-run the generator on a
new dataset drop instead of hand-editing task.toml files. Dataset revisions
reissue task ids with a date suffix (e.g. `_070826`); the generator replaces the
older directory. 66 of the 75 are `open` (reachable); 9 are `blocked-site`.

Tags:
- `smoke` — the task's base id was part of the 20-task
  [hard-smoke](../online-mind2web-hard-smoke/README.md) batch (site verified
  reachable 2026-07-18).
- `blocked-site` — the site blocks local non-stealth Chromium; expect
  policy/challenge failures rather than agent signal. Either the homepage stops
  at a bot challenge (cars.com, sourceforge.net, apartments.com; checked
  2026-07-18) or the homepage loads but interior task pages wall the automation
  (cvs.com Akamai "Access Denied", healthline.com CloudFront 403; observed in
  the 2026-07-20 batch — these pass a homepage-only reach check). dillards.com
  is also here: browsing works but it geoblocks the purchase flows the tasks need.
- `open` — homepage verified reachable with local non-stealth ebrowse Chromium
  (smoke sites 2026-07-18, all remaining hosts 2026-07-20: real title + usable
  outline; porsche/samsung/uniqlo redirect to regional storefronts, covered by
  the task-redirects policy).

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
