import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Optional

from app.config import CACHE_DIR, CACHE_TTL


def _ensure_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            expires_at REAL NOT NULL
        )"""
    )
    conn.commit()
    return conn


_db_path = os.path.join(CACHE_DIR, "cache.db")
os.makedirs(CACHE_DIR, exist_ok=True)
_conn = _ensure_db(_db_path)
_lock = __import__("threading").Lock()


def _cache_key(source: str, query: str) -> str:
    raw = f"{source}:{query}"
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_get(source: str, query: str) -> Optional[Any]:
    key = _cache_key(source, query)
    with _lock:
        row = _conn.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    value, expires_at = row
    if time.time() > expires_at:
        return None
    return json.loads(value)


def cache_set(source: str, query: str, data: Any, ttl: int = CACHE_TTL) -> None:
    key = _cache_key(source, query)
    expires_at = time.time() + ttl
    serialized = json.dumps(data)
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, serialized, expires_at),
        )
        _conn.commit()


def cache_clear(source: Optional[str] = None) -> int:
    """Clear expired entries (or all if source is None and force=True)."""
    with _lock:
        cursor = _conn.execute(
            "DELETE FROM cache WHERE expires_at < ?", (time.time(),)
        )
        _conn.commit()
    return cursor.rowcount


def cache_clear_all() -> int:
    """Delete every entry from the HTTP cache regardless of TTL."""
    with _lock:
        cursor = _conn.execute("DELETE FROM cache")
        _conn.commit()
    return cursor.rowcount
