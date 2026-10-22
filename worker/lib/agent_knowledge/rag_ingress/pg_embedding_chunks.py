"""Bounded PG embeddings with durable, content-addressed chunk receipts.

The cache contains vectors and digests only. Source text stays in its authority
store. A session is published only after every chunk has an embedding.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
from typing import Iterator

CHUNK_BYTES = 8192
CHUNKED_PROFILE = "lbrain-memory-gemini-embedding-2-bounded-mean-v2"
INVALID_TEXT_NUL = "invalid_text_nul"
TERMINAL_INPUT_REASONS = frozenset({"secret_egress_policy", INVALID_TEXT_NUL})


class InvalidProjectionText(ValueError):
    pass


class EmbeddingChunksPending(Exception):
    """A bounded cycle completed useful work; resume from the vector cache."""


def assert_pg_text_safe(*values: str) -> None:
    if any("\x00" in value for value in values):
        raise InvalidProjectionText(INVALID_TEXT_NUL)


def iter_embedding_chunks(text: str) -> Iterator[tuple[str, int]]:
    # Slice characters first so we never allocate a second whole-body byte copy.
    # Each character is at most four UTF-8 bytes; split the small byte buffer at
    # a character boundary and retain every byte, including whitespace.
    for offset in range(0, len(text), CHUNK_BYTES):
        data = text[offset:offset + CHUNK_BYTES].encode("utf-8")
        while data:
            end = min(CHUNK_BYTES, len(data))
            while end < len(data) and data[end] & 0xC0 == 0x80:
                end -= 1
            yield data[:end].decode("utf-8"), end
            data = data[end:]


class BoundedPgEmbedding:
    def __init__(self, provider, *, cache_path: str, endpoint_scope: str = "",
                 cycle_budget: int = 192, session_budget: int = 64,
                 cache_entries: int = 16384) -> None:
        if min(cycle_budget, session_budget, cache_entries) < 1:
            raise ValueError("PG embedding budgets must be positive")
        self.provider = provider
        self.remaining = cycle_budget
        self.session_budget = session_budget
        self.cache_entries = cache_entries
        self.scope = json.dumps([CHUNKED_PROFILE, provider.model, provider.size,
                                 endpoint_scope], separators=(",", ":"))
        self.path = Path(cache_path)
        self.db = None

    def _connect(self):
        if self.db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Private mode at creation, rather than a chmod-after-write window.
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            self.db = sqlite3.connect(self.path, timeout=15)
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("CREATE TABLE IF NOT EXISTS vectors ("
                            "digest TEXT PRIMARY KEY, vector BLOB NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS progress ("
                            "digest TEXT PRIMARY KEY, completed INTEGER NOT NULL, total BLOB NOT NULL)")
            self.db.commit()
        return self.db

    def _validated(self, vector):
        values = list(vector)
        if (len(values) != self.provider.size
                or not all(math.isfinite(v) for v in values)
                or not any(v != 0 for v in values)):
            raise ValueError("PG chunk embedding has invalid dimensions or norm")
        return values

    def embed(self, text: str, *, content_hash: str) -> list[float]:
        # Validate even cache hits; an invalid source must never be published.
        assert_pg_text_safe(text)
        requested = 0
        db = self._connect()
        progress_key = hashlib.sha256((self.scope + "\n" + content_hash).encode()).hexdigest()
        previous = db.execute("SELECT completed, total FROM progress WHERE digest=?",
                              (progress_key,)).fetchone()
        completed = int(previous[0]) if previous else 0
        total = (list(struct.unpack("<" + "d" * self.provider.size, previous[1]))
                 if previous else [0.0] * self.provider.size)
        for index_in_body, (chunk, weight) in enumerate(iter_embedding_chunks(text)):
            if index_in_body < completed:
                continue
            digest = hashlib.sha256((self.scope + "\n" + chunk).encode("utf-8")).hexdigest()
            row = db.execute("SELECT vector FROM vectors WHERE digest=?", (digest,)).fetchone()
            if row is None:
                if self.remaining <= 0 or requested >= self.session_budget:
                    raise EmbeddingChunksPending()
                self.remaining -= 1
                requested += 1
                vector = self._validated(self.provider.embed(chunk))
                # Fixed little-endian float32 receipts are portable and compact.
                blob = struct.pack("<" + "f" * len(vector), *vector)
                db.execute("INSERT OR IGNORE INTO vectors VALUES (?, ?)", (digest, blob))
                # FIFO eviction bounds persistent storage. No raw source is saved.
                db.execute("DELETE FROM vectors WHERE rowid IN (SELECT rowid FROM vectors "
                           "ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (self.cache_entries,))
            else:
                vector = self._validated(struct.unpack("<" + "f" * self.provider.size, row[0]))
            for index, value in enumerate(vector):
                total[index] += value * weight
            completed += 1
            # Aggregate and next position commit atomically with the vector.
            # Resume does not depend on old vector receipts surviving eviction.
            db.execute("INSERT OR REPLACE INTO progress VALUES (?, ?, ?)",
                       (progress_key, completed, struct.pack("<" + "d" * len(total), *total)))
            db.execute("DELETE FROM progress WHERE rowid IN (SELECT rowid FROM progress "
                       "ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (self.cache_entries,))
            db.commit()
        if not completed:
            raise ValueError("PG embedding input is empty")
        norm = math.sqrt(sum(value * value for value in total))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("PG aggregate embedding has invalid norm")
        return [value / norm for value in total]

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
