"""Persistent summary cache: sqlite, keyed by section content hash.

Survives daemon restarts; shared by all sessions. Writes are tiny and rare
(one batch per newly-seen page), so synchronous sqlite is fine on the loop.
"""

from __future__ import annotations

import sqlite3
import time

from ebrowse.config import cache_dir

_PRUNE_ABOVE = 50_000
_PRUNE_TO = 40_000


class SummaryCache:
    def __init__(self, path: str | None = None) -> None:
        self._db = sqlite3.connect(path or str(cache_dir() / "summaries.db"))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS summaries ("
            "  content_hash TEXT PRIMARY KEY,"
            "  summary TEXT NOT NULL,"
            "  created_at REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS captions ("
            "  image_key TEXT PRIMARY KEY,"
            "  caption TEXT NOT NULL,"
            "  created_at REAL NOT NULL)"
        )
        self._db.commit()

    def get_many(self, hashes: list[str]) -> dict[str, str]:
        if not hashes:
            return {}
        marks = ",".join("?" * len(hashes))
        rows = self._db.execute(
            f"SELECT content_hash, summary FROM summaries WHERE content_hash IN ({marks})",
            hashes,
        ).fetchall()
        return dict(rows)

    def put_many(self, items: dict[str, str]) -> None:
        now = time.time()
        self._db.executemany(
            "INSERT OR REPLACE INTO summaries VALUES (?, ?, ?)",
            [(h, s, now) for h, s in items.items()],
        )
        self._db.commit()
        self._maybe_prune("summaries")

    def get_caption(self, image_key: str) -> str | None:
        row = self._db.execute(
            "SELECT caption FROM captions WHERE image_key = ?", (image_key,)
        ).fetchone()
        return row[0] if row else None

    def put_caption(self, image_key: str, caption: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO captions VALUES (?, ?, ?)", (image_key, caption, time.time())
        )
        self._db.commit()
        self._maybe_prune("captions")

    def _maybe_prune(self, table: str) -> None:
        (n,) = self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
        if n > _PRUNE_ABOVE:
            self._db.execute(
                f"DELETE FROM {table} WHERE rowid IN ("  # noqa: S608
                f"  SELECT rowid FROM {table} ORDER BY created_at ASC LIMIT ?)",
                (n - _PRUNE_TO,),
            )
            self._db.commit()

    def close(self) -> None:
        self._db.close()
