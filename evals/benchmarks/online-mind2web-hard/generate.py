"""Generate the online-mind2web-hard benchmark from an Online-Mind2Web dataset
dump. Edit/re-run this generator when the dataset updates — task.toml files are
its output, not hand-maintained.

    python evals/benchmarks/online-mind2web-hard/generate.py ~/Downloads/Online_Mind2Web.json

Rewrites every task directory whose task_id shares a base id with a dataset
entry (revision suffixes like `_070826` replace the older directory), removes
directories for tasks no longer in the dataset, and prints a change summary.
Sites known to block local non-stealth Chromium (Cloudflare/Akamai challenges,
checked 2026-07-18) are tagged `blocked-site` instead of excluded, so runs can
skip them with tag selection while keeping the benchmark complete.
"""

from __future__ import annotations

import json
import shutil
import sys
import tomllib
from pathlib import Path

BENCH_DIR = Path(__file__).parent
LEVEL = "hard"
# Hosts that block local, non-stealth ebrowse Chromium — tagged `blocked-site`,
# not dropped, so a run can skip them (`--tag open`) while the benchmark stays
# complete. Two flavors:
#   - homepage stopped at a bot challenge (checked 2026-07-18): cars.com,
#     sourceforge.net, apartments.com.
#   - homepage loads but interior task pages wall the automation (observed in
#     the 2026-07-20 hard batch): cvs.com (Akamai "Access Denied" on all search
#     pages, run qwen-hard-afcebfed), healthline.com (CloudFront 403 on ~half of
#     visited pages, run qwen-hard-dcd26e66). These pass a homepage-only reach
#     check, so they must be listed here explicitly.
#   - dillards.com: browsing works, but the site geolocates this environment as
#     international and refuses the purchase/gift-card flows the tasks require
#     (run qwen-hard-199be0b5), so the tasks cannot succeed from here.
BLOCKED_HOSTS = {
    "www.cars.com",
    "sourceforge.net",
    "www.apartments.com",
    "www.cvs.com",
    "www.healthline.com",
    "www.dillards.com",
}
# Hosts deliberately dropped from the benchmark (not bot walls — a decision to
# exclude the site). bestbuy.com: aggressive HTTP/2 protocol errors on product
# pages block the tasks unpredictably (see runs qwen-hard-4464a842, -fc53ddd3).
EXCLUDED_HOSTS = {"www.bestbuy.com"}
# Sites exercised by the 2026-07-19 smoke batch (real page + usable outline).
SMOKE_CHECKED_IDS_FILE = BENCH_DIR.parent / "online-mind2web-hard-smoke"


def toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # valid TOML basic string


def main(dataset_path: str) -> int:
    tasks = [
        t
        for t in json.load(open(dataset_path))
        if t["level"] == LEVEL and t["website"].split("/")[2] not in EXCLUDED_HOSTS
    ]
    smoke_bases = {
        p.name.split("_")[0]
        for p in SMOKE_CHECKED_IDS_FILE.iterdir()
        if (p / "task.toml").is_file()
    }
    existing = {p.name: p for p in BENCH_DIR.iterdir() if (p / "task.toml").is_file()}
    by_base = {p.split("_")[0]: p for p in existing}
    wanted: set[str] = set()
    added = replaced = updated = 0

    for t in tasks:
        tid, base = t["task_id"], t["task_id"].split("_")[0]
        wanted.add(tid)
        host = t["website"].split("/")[2]
        tags = ["online-mind2web", LEVEL]
        # tag selection is AND-only, so blocked/open are complementary positive
        # tags: --tag open runs the reachable 73, --tag blocked-site the rest
        tags.append("blocked-site" if host in BLOCKED_HOSTS else "open")
        if base in smoke_bases:
            tags.append("smoke")
        prior = by_base.get(base)
        if prior and prior != tid:
            shutil.rmtree(BENCH_DIR / prior)
            print(f"replaced {prior} -> {tid}")
            replaced += 1
        elif prior:
            old = tomllib.load((BENCH_DIR / tid / "task.toml").open("rb"))["task"]
            if old["prompt"] != t["confirmed_task"] or old["url"] != t["website"]:
                updated += 1
        else:
            added += 1
        d = BENCH_DIR / tid
        d.mkdir(exist_ok=True)
        (d / "task.toml").write_text(
            "[task]\n"
            f"prompt = {toml_str(t['confirmed_task'])}\n"
            f"url = {toml_str(t['website'])}\n"
            f"tags = [{', '.join(toml_str(x) for x in tags)}]\n",
            encoding="utf-8",
        )

    all_ds_bases = {t["task_id"].split("_")[0] for t in json.load(open(dataset_path))}
    for name, p in existing.items():
        if name not in wanted and by_base.get(name.split("_")[0]) == name:
            base_still_wanted = any(w.split("_")[0] == name.split("_")[0] for w in wanted)
            if not base_still_wanted:
                shutil.rmtree(p)
                why = "excluded host" if name.split("_")[0] in all_ds_bases else "no longer in dataset"
                print(f"removed {name} ({why})")

    print(f"{len(tasks)} {LEVEL} tasks: {added} added, {replaced} replaced, {updated} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
