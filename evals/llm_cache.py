"""
A persistent LangChain LLM cache, so recorded evaluation runs can be replayed without calling the API.

Every chat-model call goes through LangChain's global cache when one is set. Recording stores each response keyed
by the exact prompt and model settings; replaying serves them back. A replay run points the client at an
unreachable address, so a cache miss shows up as a page failure instead of a silent paid call.
"""

import sqlite3
from pathlib import Path

from langchain_core.caches import RETURN_VAL_TYPE, BaseCache
from langchain_core.load import dumps, loads


class SQLiteLLMCache(BaseCache):
    """LangChain cache backed by a single SQLite file."""

    def __init__(self, path: Path) -> None:
        """
        Open (and create if needed) the cache file.

        :param path: The SQLite database path.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache (prompt TEXT, llm TEXT, value TEXT, PRIMARY KEY (prompt, llm))"
        )
        self._db.commit()

    def lookup(self, prompt: str, llm_string: str) -> RETURN_VAL_TYPE | None:
        """Return the cached generations for a prompt and model, if any."""
        row = self._db.execute(
            "SELECT value FROM llm_cache WHERE prompt = ? AND llm = ?", (prompt, llm_string)
        ).fetchone()
        return loads(row[0]) if row else None

    def update(self, prompt: str, llm_string: str, return_val: RETURN_VAL_TYPE) -> None:
        """Store the generations for a prompt and model."""
        self._db.execute(
            "INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?)", (prompt, llm_string, dumps(list(return_val)))
        )
        self._db.commit()

    def clear(self, **kwargs: object) -> None:
        """Remove every cached response."""
        self._db.execute("DELETE FROM llm_cache")
        self._db.commit()

    def size(self) -> int:
        """Number of cached responses."""
        return int(self._db.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0])
