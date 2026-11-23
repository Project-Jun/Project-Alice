"""SQLite cache for AI explanations.

Key = provider, model, prompt version and the file's git blob SHA-1, so an
unchanged file is never sent twice, and the SHA matches GitHub's tree API.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from .base import Explanation, data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS explanations (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    kind TEXT NOT NULL,
    sha TEXT NOT NULL,
    body TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (provider, model, prompt_version, kind, sha)
)
"""


def blob_sha(data: bytes) -> str:
    """SHA-1 of the content the way git hashes a blob (``git hash-object``)."""
    h = hashlib.sha1(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


class ExplanationCache:
    """Thread-safe cache file; it is only created on first use."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else data_dir() / "cache.sqlite"
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
            self._conn.execute(_SCHEMA)
            self._conn.commit()
        return self._conn

    def get(self, provider: str, model: str, prompt_version: str, sha: str, kind: str = "file") -> Explanation | None:
        with self._lock:
            row = self._db().execute(
                "SELECT body FROM explanations"
                " WHERE provider = ? AND model = ? AND prompt_version = ? AND kind = ? AND sha = ?",
                (provider, model, prompt_version, kind, sha),
            ).fetchone()
        if row is None:
            return None
        try:
            return Explanation.from_dict(json.loads(row[0]))
        except (ValueError, AttributeError):
            return None

    def get_many(self, provider: str, model: str, prompt_version: str, shas, kind: str = "file") -> dict:
        """{sha: Explanation} for the given SHAs that are in the cache (one query per 500 SHAs)."""
        wanted = list(dict.fromkeys(shas))
        found: dict[str, Explanation] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            marks = ",".join("?" * len(chunk))
            with self._lock:
                rows = self._db().execute(
                    "SELECT sha, body FROM explanations"
                    f" WHERE provider = ? AND model = ? AND prompt_version = ? AND kind = ? AND sha IN ({marks})",
                    (provider, model, prompt_version, kind, *chunk),
                ).fetchall()
            for sha, body in rows:
                try:
                    found[sha] = Explanation.from_dict(json.loads(body))
                except (ValueError, AttributeError):
                    continue
        return found

    def put(self, provider: str, model: str, prompt_version: str, sha: str, explanation: Explanation,
            kind: str = "file") -> None:
        body = json.dumps(explanation.to_dict(), ensure_ascii=False)
        with self._lock:
            db = self._db()
            db.execute(
                "INSERT OR REPLACE INTO explanations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (provider, model, prompt_version, kind, sha, body, time.time()),
            )
            db.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
