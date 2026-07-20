"""Trace persistence: append-only JSONL writer/reader + content-addressed blobs.

Blob refs are "sha256:<hex>"; files live at blobs/<hex[:2]>/<hex><suffix>.
Content addressing dedupes identical payloads across steps for free (no-op
actions produce identical DomSnapshots often).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ebrowse_evals.trace.records import (
    Anomaly,
    Record,
    RunEnd,
    RunMeta,
    Step,
    record_from_dict,
)

EVENTS_FILE = "events.jsonl"
BLOBS_DIR = "blobs"


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, data: bytes, suffix: str = "") -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest[:2] / f"{digest}{suffix}"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            tmp.rename(path)  # atomic: readers never see partial blobs
        return f"sha256:{digest}"

    def path(self, ref: str) -> Path:
        digest = ref.removeprefix("sha256:")
        d = self.root / digest[:2]
        matches = list(d.glob(f"{digest}*")) if d.is_dir() else []
        if not matches:
            raise FileNotFoundError(f"blob {ref} not in {self.root}")
        return matches[0]

    def get(self, ref: str) -> bytes:
        return self.path(ref).read_bytes()


class TraceWriter:
    """Appends records to a run directory. Stamps ts/mono when the caller
    didn't (fixture generators pass fixed values for determinism)."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(run_dir / BLOBS_DIR)
        self._events = run_dir / EVENTS_FILE

    def write(self, record: Record) -> None:
        if record.ts is None:
            record.ts = time.time()
        if record.mono is None:
            record.mono = time.monotonic()
        with self._events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def put_blob(self, data: bytes, suffix: str = "") -> str:
        return self.blobs.put(data, suffix)


class TraceReader:
    """Reads a run directory. Skips unknown record types and malformed
    trailing lines (a crashed run still yields a readable trace)."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.blobs = BlobStore(run_dir / BLOBS_DIR)
        events = run_dir / EVENTS_FILE
        if not events.is_file():
            raise FileNotFoundError(f"no {EVENTS_FILE} in {run_dir}")

    def raw(self) -> Iterator[dict[str, Any]]:
        with (self.run_dir / EVENTS_FILE).open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def records(self) -> Iterator[Record]:
        for d in self.raw():
            rec = record_from_dict(d)
            if rec is not None:
                yield rec

    # -- convenience views used by the viewer / inspection CLI ------------

    def meta(self) -> RunMeta | None:
        return next((r for r in self.records() if isinstance(r, RunMeta)), None)

    def end(self) -> RunEnd | None:
        return next((r for r in self.records() if isinstance(r, RunEnd)), None)

    def steps(self) -> list[Step]:
        return [r for r in self.records() if isinstance(r, Step)]

    def anomalies(self) -> list[Anomaly]:
        return [r for r in self.records() if isinstance(r, Anomaly)]

    def for_step(self, step: int) -> list[Record]:
        return [r for r in self.records() if r.step == step]

    def validate(self) -> list[str]:
        """Structural checks; returns problems (empty = valid)."""
        problems: list[str] = []
        records = list(self.records())
        metas = [r for r in records if isinstance(r, RunMeta)]
        if len(metas) != 1:
            problems.append(f"expected exactly one run_meta, found {len(metas)}")
        elif records[0] is not metas[0]:
            problems.append("run_meta is not the first record")
        step_ids = [r.step for r in records if isinstance(r, Step)]
        if None in step_ids:
            problems.append("step record without a step id")
        ids = [s for s in step_ids if s is not None]
        if ids != sorted(set(ids)):
            problems.append(f"step ids not strictly increasing: {ids}")
        for r in records:
            for ref in (
                getattr(r, "screenshot", None),
                getattr(r, "dom_snapshot", None),
                getattr(r, "text_ref", None),
                getattr(r, "content_ref", None),
            ):
                if ref is not None:
                    try:
                        self.blobs.path(ref)
                    except FileNotFoundError:
                        problems.append(f"step {r.step}: missing blob {ref}")
        return problems
