"""Response cache keyed on (backend, model_rev, prompt_hash, question_id,
sample_idx, seed).

This is the cost/idempotency pattern of any production batch-inference
pipeline: a run interrupted partway through, or re-launched to add more
samples, never re-pays for a completion it already has.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Optional


class ResponseCache:
    def __init__(self, db_path: Path | str) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                cache_key TEXT PRIMARY KEY,
                response TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def make_key(
        *,
        backend: str,
        model_rev: str,
        prompt: str,
        question_id: str,
        sample_idx: int,
        seed: int,
    ) -> str:
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        return "|".join(
            [backend, model_rev or "", prompt_hash, question_id, str(sample_idx), str(seed)]
        )

    def get(self, cache_key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT response FROM responses WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return row[0] if row else None

    def put(self, cache_key: str, response: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (cache_key, response) VALUES (?, ?)",
            (cache_key, response),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ResponseCache":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
