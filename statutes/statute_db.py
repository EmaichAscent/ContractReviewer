"""SQLite database for caching statute search results."""

import sqlite3
import os
from datetime import datetime, timedelta

import config


def get_db():
    """Get a database connection, creating tables if needed."""
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS statutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state TEXT NOT NULL,
            statute_number TEXT,
            title TEXT,
            summary TEXT,
            full_text TEXT,
            source_url TEXT,
            relevance TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(state, statute_number)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_statutes_state ON statutes(state)
    """)
    conn.commit()
    return conn


def get_statutes_for_state(state, max_age_days=90):
    """Get cached statutes for a state. Returns list of dicts or empty list if stale/missing."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    rows = conn.execute(
        "SELECT * FROM statutes WHERE state = ? AND fetched_at > ?",
        (state.upper(), cutoff)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_statutes(state, statutes_list):
    """Save a list of statute dicts to the database.

    Each dict should have: statute_number, title, summary, full_text, source_url, relevance
    """
    conn = get_db()
    for s in statutes_list:
        conn.execute("""
            INSERT OR REPLACE INTO statutes
            (state, statute_number, title, summary, full_text, source_url, relevance, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            state.upper(),
            s.get("statute_number", ""),
            s.get("title", ""),
            s.get("summary", ""),
            s.get("full_text", ""),
            s.get("source_url", ""),
            s.get("relevance", ""),
            datetime.now().isoformat(),
        ))
    conn.commit()
    conn.close()


def search_statutes(query):
    """Search cached statutes by keyword."""
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM statutes
           WHERE title LIKE ? OR summary LIKE ? OR full_text LIKE ? OR statute_number LIKE ?""",
        (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_states():
    """Get list of all states with cached statutes."""
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT state FROM statutes ORDER BY state").fetchall()
    conn.close()
    return [r["state"] for r in rows]
