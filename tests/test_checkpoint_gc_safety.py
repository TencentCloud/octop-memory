"""Retention window and SQLite backend guards."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from octop_memory.pipeline.lifecycle.checkpoint_gc import (
    _get_checkpointer_conn,
    _subgraph_newer_than_parent,
    _thread_ns_pairs,
    _victim_checkpoint_ids,
)


class _Memory:
    def __init__(self, *, saver: Any) -> None:
        self._checkpointer = saver

    def _ensure_checkpointer(self) -> None:
        return None


def _checkpoints_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)")
    return conn


def _add_cp(conn: sqlite3.Connection, thread_id: str, ns: str, cid: str) -> None:
    conn.execute("INSERT INTO checkpoints VALUES (?, ?, ?)", (thread_id, ns, cid))
    conn.commit()


class TestSubgraphNewerThanParent:
    def test_no_parent_row_is_treated_as_unsafe(self) -> None:
        """No parent snapshot means we cannot reason about ordering, so the
        conservative answer keeps the subgraph rows.
        """
        conn = _checkpoints_conn()
        _add_cp(conn, "t1", "tools:1", "cp-5")
        assert _subgraph_newer_than_parent(conn, "t1") is True

    def test_subgraph_ahead_of_parent(self) -> None:
        conn = _checkpoints_conn()
        _add_cp(conn, "t1", "", "cp-3")
        _add_cp(conn, "t1", "tools:1", "cp-9")
        assert _subgraph_newer_than_parent(conn, "t1") is True

    def test_subgraph_behind_parent(self) -> None:
        conn = _checkpoints_conn()
        _add_cp(conn, "t1", "", "cp-9")
        _add_cp(conn, "t1", "tools:1", "cp-3")
        assert _subgraph_newer_than_parent(conn, "t1") is False

    def test_other_threads_do_not_leak_in(self) -> None:
        conn = _checkpoints_conn()
        _add_cp(conn, "t1", "", "cp-5")
        _add_cp(conn, "t2", "tools:1", "cp-9")
        assert _subgraph_newer_than_parent(conn, "t1") is False


class TestThreadNsPairs:
    def test_scoped_to_one_thread(self) -> None:
        conn = _checkpoints_conn()
        _add_cp(conn, "t1", "", "cp-1")
        _add_cp(conn, "t1", "tools:1", "cp-2")
        _add_cp(conn, "t2", "", "cp-3")

        assert sorted(_thread_ns_pairs(conn, thread_id="t1")) == [("t1", ""), ("t1", "tools:1")]

    def test_all_streams(self) -> None:
        conn = _checkpoints_conn()
        _add_cp(conn, "t1", "", "cp-1")
        _add_cp(conn, "t2", "", "cp-2")
        assert sorted(_thread_ns_pairs(conn, thread_id=None)) == [("t1", ""), ("t2", "")]


class TestVictimSelection:
    def test_empty_stream_has_no_victims(self) -> None:
        conn = _checkpoints_conn()
        assert _victim_checkpoint_ids(conn, "t1", "", keep_last=1, cutoff_id=None) == []

    def test_latest_checkpoint_always_survives(self) -> None:
        """Even a cutoff far in the future must not erase the only usable state."""
        conn = _checkpoints_conn()
        for cid in ("cp-1", "cp-2", "cp-3"):
            _add_cp(conn, "t1", "", cid)

        victims = _victim_checkpoint_ids(conn, "t1", "", keep_last=None, cutoff_id="cp-9")
        assert "cp-3" not in victims
        assert sorted(victims) == ["cp-1", "cp-2"]

    def test_keep_last_and_cutoff_union(self) -> None:
        conn = _checkpoints_conn()
        for cid in ("cp-1", "cp-2", "cp-3", "cp-4"):
            _add_cp(conn, "t1", "", cid)

        # keep_last spares cp-4; cutoff spares cp-2 and above.
        victims = _victim_checkpoint_ids(conn, "t1", "", keep_last=1, cutoff_id="cp-2")
        assert victims == ["cp-1"]


class TestCheckpointerConnRequirement:
    def test_non_sqlite_checkpointer_raises_instead_of_silently_skipping(self) -> None:
        class _NotASaver:
            conn = "definitely not a sqlite3.Connection"

        with pytest.raises(RuntimeError, match="requires the SQLite checkpointer"):
            _get_checkpointer_conn(_Memory(saver=_NotASaver()))  # type: ignore[arg-type]

    def test_missing_conn_attribute_raises(self) -> None:
        class _NoConn:
            pass

        with pytest.raises(RuntimeError, match="PostgreSQL checkpoint pruning is not yet supported"):
            _get_checkpointer_conn(_Memory(saver=_NoConn()))  # type: ignore[arg-type]
