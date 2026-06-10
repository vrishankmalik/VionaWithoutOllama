"""SQLite enrichment store — patents, patent_discrepancies, and labeling tables.

Separate DB file from the HTTP cache so enrichment data persists independently.
"""
from __future__ import annotations

import os
import sqlite3
import time
import threading
from typing import Any, Optional

from app.config import CACHE_DIR

_DB_PATH = os.path.join(CACHE_DIR, "enrichment.db")
os.makedirs(CACHE_DIR, exist_ok=True)

_DDL = """
CREATE TABLE IF NOT EXISTS patents (
    din            TEXT NOT NULL,
    patent_number  TEXT NOT NULL,
    filing_date    TEXT,
    grant_date     TEXT,
    expiry_date    TEXT,
    detail_url     TEXT,
    fetched_at     REAL NOT NULL,
    PRIMARY KEY (din, patent_number)
);

CREATE TABLE IF NOT EXISTS patent_discrepancies (
    din            TEXT NOT NULL,
    patent_number  TEXT NOT NULL,
    field          TEXT NOT NULL,
    website_value  TEXT,
    zip_value      TEXT,
    logged_at      REAL NOT NULL,
    PRIMARY KEY (din, patent_number, field)
);

CREATE TABLE IF NOT EXISTS labeling (
    din                             TEXT PRIMARY KEY,
    drug_code                       INTEGER,
    pdf_url                         TEXT,
    active_ingredient               TEXT,
    active_ingredient_page          INTEGER,
    nonmedicinal_ingredients        TEXT,
    nonmedicinal_ingredients_page   INTEGER,
    pack_size                       TEXT,
    pack_size_page                  INTEGER,
    pack_style                      TEXT,
    pack_style_page                 INTEGER,
    color                           TEXT,
    color_page                      INTEGER,
    shape                           TEXT,
    shape_page                      INTEGER,
    size_mm                         TEXT,
    size_mm_page                    INTEGER,
    weight                          TEXT,
    weight_page                     INTEGER,
    ph                              TEXT,
    ph_page                         INTEGER,
    needs_ocr                       INTEGER NOT NULL DEFAULT 0,
    has_unverified                  INTEGER NOT NULL DEFAULT 0,
    fetched_at                      REAL NOT NULL
);
"""

_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()  # reentrant — write operations call get_conn() while holding the lock


_LABELING_MIGRATIONS = (
    # nonmedicinal_ingredients replaces excipients_core/coating/preservatives
    "nonmedicinal_ingredients TEXT",
    "nonmedicinal_ingredients_page INTEGER",
    # columns present in older schemas that new code still writes
    "color TEXT",
    "color_page INTEGER",
    "pack_style TEXT",
    "pack_style_page INTEGER",
    "size_mm TEXT",
    "size_mm_page INTEGER",
    "weight TEXT",
    "weight_page INTEGER",
    "ph TEXT",
    "ph_page INTEGER",
    "has_unverified INTEGER NOT NULL DEFAULT 0",
)


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    # Add any columns introduced after the table was first created.
    for col_def in _LABELING_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE labeling ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists — ignore
    conn.commit()
    return conn


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _open()
    return _conn


# ── patents ───────────────────────────────────────────────────────────────────

def upsert_patent(
    din: str,
    patent_number: str,
    filing_date: Optional[str],
    grant_date: Optional[str],
    expiry_date: Optional[str],
    detail_url: Optional[str] = None,
) -> None:
    with _lock:
        get_conn().execute(
            """INSERT OR REPLACE INTO patents
               (din, patent_number, filing_date, grant_date, expiry_date, detail_url, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (din, patent_number, filing_date, grant_date, expiry_date, detail_url, time.time()),
        )
        get_conn().commit()


def get_patents_for_din(din: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM patents WHERE din = ? ORDER BY expiry_date DESC NULLS LAST",
        (din,),
    ).fetchall()
    return [dict(r) for r in rows]


def is_patent_stale(din: str, ttl: float) -> bool:
    """Return True if the patent rows for this DIN are absent or older than ttl seconds."""
    row = get_conn().execute(
        "SELECT MAX(fetched_at) AS fetched_at FROM patents WHERE din = ?", (din,)
    ).fetchone()
    if row is None or row["fetched_at"] is None:
        return True
    return (time.time() - row["fetched_at"]) > ttl


def log_discrepancy(
    din: str,
    patent_number: str,
    field: str,
    website_value: Optional[str],
    zip_value: Optional[str],
) -> None:
    with _lock:
        get_conn().execute(
            """INSERT OR REPLACE INTO patent_discrepancies
               (din, patent_number, field, website_value, zip_value, logged_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (din, patent_number, field, website_value, zip_value, time.time()),
        )
        get_conn().commit()


def get_discrepancies() -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM patent_discrepancies ORDER BY logged_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ── labeling ──────────────────────────────────────────────────────────────────

_LABELING_COLS = (
    "din", "drug_code", "pdf_url",
    "active_ingredient", "active_ingredient_page",
    "nonmedicinal_ingredients", "nonmedicinal_ingredients_page",
    "pack_size", "pack_size_page",
    "pack_style", "pack_style_page",
    "color", "color_page",
    "shape", "shape_page",
    "size_mm", "size_mm_page",
    "weight", "weight_page",
    "ph", "ph_page",
    "needs_ocr", "has_unverified", "fetched_at",
)


def upsert_labeling(din: str, fields: dict[str, Any]) -> None:
    row: dict[str, Any] = {"din": din, "fetched_at": time.time()}
    row.update(fields)
    # Only keep known columns
    filtered = {k: row[k] for k in _LABELING_COLS if k in row}
    cols = ", ".join(filtered.keys())
    placeholders = ", ".join("?" * len(filtered))
    with _lock:
        get_conn().execute(
            f"INSERT OR REPLACE INTO labeling ({cols}) VALUES ({placeholders})",
            list(filtered.values()),
        )
        get_conn().commit()


def get_labeling_for_din(din: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT * FROM labeling WHERE din = ?", (din,)
    ).fetchone()
    return dict(row) if row else None


def is_labeling_stale(din: str, ttl: float) -> bool:
    """Return True if the labeling record is absent or was fetched more than ttl seconds ago."""
    row = get_conn().execute(
        "SELECT fetched_at FROM labeling WHERE din = ?", (din,)
    ).fetchone()
    if row is None:
        return True
    return (time.time() - row["fetched_at"]) > ttl


def reset_patents_table() -> int:
    """Drop and recreate the patents and patent_discrepancies tables. Returns rows deleted."""
    with _lock:
        conn = get_conn()
        count = conn.execute("SELECT COUNT(*) FROM patents").fetchone()[0]
        conn.execute("DROP TABLE IF EXISTS patents")
        conn.execute("DROP TABLE IF EXISTS patent_discrepancies")
        conn.executescript(_DDL)
        conn.commit()
        return count


def reset_labeling_table() -> int:
    """Drop and recreate the labeling table. Returns number of rows deleted."""
    with _lock:
        conn = get_conn()
        count = conn.execute("SELECT COUNT(*) FROM labeling").fetchone()[0]
        conn.execute("DROP TABLE IF EXISTS labeling")
        conn.executescript(_DDL)
        for col_def in _LABELING_MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE labeling ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
        return count


def reset_for_testing(db_path: Optional[str] = None) -> None:
    """Replace the module-level connection with a fresh in-memory DB (tests only)."""
    global _conn, _DB_PATH
    if db_path is not None:
        _DB_PATH = db_path
    with _lock:
        _conn = None
