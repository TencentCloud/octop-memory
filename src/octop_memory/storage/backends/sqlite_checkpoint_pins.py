"""Durable host references to SQLite root checkpoints."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path


def pin_sqlite_checkpoints(
    db_path: Path, checkpoints: Iterable[tuple[str, str]], *, expires_at: float | None = None, strict: bool = True
) -> list[tuple[str, str]]:
    """Protect existing (thread ID, root checkpoint ID) pairs in one transaction.

    An omitted expiry creates a permanent pin. A temporary pin can extend
    an existing lease, but cannot shorten it or replace a permanent pin.
    Return missing pairs when ``strict`` is false. In strict mode a missing
    pair raises and rolls back the entire batch.
    """
    pairs = tuple(dict.fromkeys(checkpoints))
    if not pairs:
        return []
    missing: list[tuple[str, str]] = []
    uri = db_path.expanduser().resolve(strict=True).as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hm_checkpoint_pins ("
            "thread_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL, expires_at REAL, "
            "PRIMARY KEY (thread_id, checkpoint_id))"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS hm_checkpoint_delete_pins AFTER DELETE ON checkpoints "
            "WHEN OLD.checkpoint_ns = '' BEGIN "
            "DELETE FROM hm_checkpoint_pins WHERE thread_id = OLD.thread_id AND checkpoint_id = OLD.checkpoint_id; END"
        )
        conn.execute("DELETE FROM hm_checkpoint_pins WHERE expires_at <= ?", (time.time(),))
        for thread_id, checkpoint_id in pairs:
            changed = conn.execute(
                "INSERT INTO hm_checkpoint_pins (thread_id, checkpoint_id, expires_at) "
                "SELECT thread_id, checkpoint_id, ? FROM checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id = ? "
                "ON CONFLICT (thread_id, checkpoint_id) DO UPDATE SET expires_at = "
                "CASE WHEN hm_checkpoint_pins.expires_at IS NULL OR excluded.expires_at IS NULL THEN NULL "
                "ELSE MAX(hm_checkpoint_pins.expires_at, excluded.expires_at) END",
                (expires_at, thread_id, checkpoint_id),
            ).rowcount
            if changed != 1:
                if strict:
                    raise ValueError(f"Cannot pin missing root checkpoint {checkpoint_id!r} for thread {thread_id!r}")
                missing.append((thread_id, checkpoint_id))
    return missing


def pinned_checkpoint_ids(cur: sqlite3.Cursor, thread_id: str) -> set[str]:
    """Read active pins in the same transaction that plans checkpoint deletion."""
    if not cur.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='hm_checkpoint_pins'").fetchone():
        return set()
    return {
        cid
        for (cid,) in cur.execute(
            "SELECT checkpoint_id FROM hm_checkpoint_pins WHERE thread_id = ? "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (thread_id, time.time()),
        )
    }
