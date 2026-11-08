"""
A persistent embedding cache, the embeddings counterpart of :mod:`evals.llm_cache`.

LangChain's LLM cache doesn't cover embeddings, so this wraps the embedding client: vectors are stored per
(model, text) in the same SQLite file, and in strict (replay) mode a miss raises instead of calling the API.
"""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from langchain_core.embeddings import Embeddings

from evals.llm_cache import CacheMissError


class CachedEmbeddings(Embeddings):
    """Embeddings that are looked up in, and recorded to, an SQLite file."""

    def __init__(self, inner: Embeddings, model: str, path: Path, *, strict: bool = False) -> None:
        """
        Wrap an embedding client.

        :param inner: The real client, used on misses (unless strict).
        :param model: The model name, part of the cache key.
        :param path: The SQLite database path.
        :param strict: Raise :class:`CacheMissError` on a miss instead of calling the API.
        """
        self.inner, self.model, self.strict = inner, model, strict
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS embedding_cache (key TEXT PRIMARY KEY, vector TEXT)")
        self._db.commit()

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\0{text}".encode()).hexdigest()

    def _lookup(self, texts: list[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        with self._lock:
            for text in texts:
                row = self._db.execute(
                    "SELECT vector FROM embedding_cache WHERE key = ?", (self._key(text),)
                ).fetchone()
                if row:
                    found[text] = json.loads(row[0])
        return found

    def _store(self, vectors: dict[str, list[float]]) -> None:
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO embedding_cache VALUES (?, ?)",
                [(self._key(t), json.dumps(v)) for t, v in vectors.items()],
            )
            self._db.commit()

    def _missing(self, texts: list[str], found: dict[str, list[float]]) -> list[str]:
        missing = list(dict.fromkeys(t for t in texts if t not in found))
        if missing and self.strict:
            raise CacheMissError("No recorded embedding for this text; run without --replay to record it.")
        return missing

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """
        Embed texts, from the cache where possible.

        :param texts: The texts.
        :return: One vector per text.
        """
        found = self._lookup(texts)
        if missing := self._missing(texts, found):
            new = dict(zip(missing, await self.inner.aembed_documents(missing), strict=True))
            self._store(new)
            found |= new
        return [found[t] for t in texts]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """
        Embed texts, from the cache where possible.

        :param texts: The texts.
        :return: One vector per text.
        """
        found = self._lookup(texts)
        if missing := self._missing(texts, found):
            new = dict(zip(missing, self.inner.embed_documents(missing), strict=True))
            self._store(new)
            found |= new
        return [found[t] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """
        Embed one text.

        :param text: The text.
        :return: Its vector.
        """
        return self.embed_documents([text])[0]
