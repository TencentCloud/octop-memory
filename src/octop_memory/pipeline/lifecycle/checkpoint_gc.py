"""Prune SQLite checkpoint history while preserving the state of every survivor.

The retention window keeps the latest checkpoint and any checkpoint matching
the count or age limit. It also keeps the checkpoints and writes required to
replay that state. A retained checkpoint's ID, payload and writes stay intact.
Only an independent boundary can be detached from a deleted ancestor.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from octop_memory.storage.driver_errors import REPORTABLE_ERRORS

_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from octop_memory.core import Memory

DEFAULT_KEEP_LAST_CHECKPOINTS = 1
"""Default per-stream checkpoint count to retain when the CLI doesn't override it.

Older checkpoints required by retained snapshots are kept as well.
"""


@dataclass
class CheckpointGcStats:
    """Row counts for one :func:`prune_checkpoints` call. ``dry_run`` reports what *would* be deleted."""

    threads_scanned: int = 0
    streams_scanned: int = 0
    streams_dropped: int = 0
    checkpoints_deleted: int = 0
    writes_deleted: int = 0
    parents_sealed: int = 0  # Compatibility with callers that report the old sealing count.
    parent_prunes_skipped: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    dry_run: bool = False


def prune_checkpoints(
    memory: Memory,
    *,
    keep_last: int | None = None,
    keep_days: int | None = None,
    thread_id: str | None = None,
    drop_subgraph_streams: bool = False,
    force_drop_subgraphs: bool = False,
    skip_pending: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> CheckpointGcStats:
    """Delete checkpoints outside the retention window and dependency closure.

    Retention is SQLite-only. The newest parent checkpoint is always kept.
    Other checkpoints match the window if they are among the newest
    ``keep_last`` or at most ``keep_days`` old. Ancestors needed for replay
    can make the resulting history longer than this window.

    Threads with subgraphs keep their cross-stream dependencies. An explicit
    ``drop_subgraph_streams`` can remove subgraphs when the only surviving
    parent is a completed checkpoint with no pending work. The force flag
    overrides those subgraph guards. Dry runs use the same plan as deletion.
    With ``skip_pending``, leave threads with work pending at their head intact.
    """
    if keep_last is None and keep_days is None:
        raise ValueError("prune_checkpoints requires keep_last and/or keep_days")
    if keep_last is not None and keep_last < 1:
        raise ValueError("keep_last must be >= 1")
    if keep_days is not None and keep_days < 0:
        raise ValueError("keep_days must be >= 0")

    from octop_memory.storage.backends.sqlite_checkpoint_maintenance import (
        delete_checkpoint_candidates,
        retain_checkpoint_dependencies,
    )

    conn = _get_checkpointer_conn(memory)
    saver = memory._checkpointer
    stats = CheckpointGcStats(started_at=datetime.now(UTC), dry_run=dry_run)
    cutoff_id = None
    if keep_days is not None:
        cutoff_id = _cutoff_checkpoint_id((now or datetime.now(UTC)) - timedelta(days=keep_days))
    force_drop = drop_subgraph_streams and force_drop_subgraphs

    with saver.cursor(transaction=False):
        threads = dict.fromkeys(tid for tid, _ in _thread_ns_pairs(conn, thread_id=thread_id))
    stats.threads_scanned = len(threads)
    for tid in threads:
        # Use the saver lock and a write transaction for the complete plan
        # and mutation. Another saver cannot extend a chain mid-prune.
        with saver.cursor(transaction=not dry_run) as cur:
            streams = _thread_ns_pairs(conn, thread_id=tid)
            stats.streams_scanned += len(streams)
            subgraphs = [ns for _, ns in streams if ns]
            if subgraphs and not drop_subgraph_streams:
                stats.parent_prunes_skipped += 1
                continue
            candidates = _victim_checkpoint_ids(conn, tid, "", keep_last=keep_last, cutoff_id=cutoff_id)
            try:
                if skip_pending and candidates:
                    latest = _latest_root_checkpoint(conn, saver.serde, tid)
                    if latest is not None and _has_pending_work(conn, tid, *latest):
                        stats.parent_prunes_skipped += 1
                        continue
                victims, detach = retain_checkpoint_dependencies(cur, saver.serde, tid, candidates)
                if (
                    subgraphs
                    and not force_drop
                    and not _safe_to_drop_subgraphs(conn, saver.serde, tid, deleted_parents=len(victims))
                ):
                    stats.parent_prunes_skipped += 1
                    continue
            except REPORTABLE_ERRORS:
                stats.parent_prunes_skipped += 1
                _LOG.warning("prune_checkpoints: cannot verify ancestry for thread=%s", tid, exc_info=True)
                continue

            stats.writes_deleted += delete_checkpoint_candidates(cur, tid, victims, detach, dry_run=dry_run)
            stats.checkpoints_deleted += len(victims)
            for ns in subgraphs:
                checkpoints, writes = _delete_stream(conn, tid, ns, dry_run=dry_run)
                stats.checkpoints_deleted += checkpoints
                stats.writes_deleted += writes
                stats.streams_dropped += int(bool(checkpoints or writes))

    stats.finished_at = datetime.now(UTC)
    level = logging.DEBUG if skip_pending and not stats.checkpoints_deleted else logging.INFO
    _LOG.log(
        level,
        "prune_checkpoints done streams=%s streams_dropped=%s checkpoints_deleted=%s "
        "writes_deleted=%s skipped=%s dry_run=%s",
        stats.streams_scanned,
        stats.streams_dropped,
        stats.checkpoints_deleted,
        stats.writes_deleted,
        stats.parent_prunes_skipped,
        dry_run,
    )
    return stats


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _delete_stream(
    conn: sqlite3.Connection,
    thread_id: str,
    checkpoint_ns: str,
    *,
    dry_run: bool,
) -> tuple[int, int]:
    """Delete every checkpoint/write in one stream. Returns (checkpoints, writes)."""
    if dry_run:
        cp = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ?",
            (thread_id, checkpoint_ns),
        ).fetchone()
        wr = conn.execute(
            "SELECT COUNT(*) FROM writes WHERE thread_id = ? AND checkpoint_ns = ?",
            (thread_id, checkpoint_ns),
        ).fetchone()
        return (cp[0] if cp else 0, wr[0] if wr else 0)
    wr_cur = conn.execute(
        "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = ?",
        (thread_id, checkpoint_ns),
    )
    cp_cur = conn.execute(
        "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ?",
        (thread_id, checkpoint_ns),
    )
    writes = wr_cur.rowcount if wr_cur.rowcount and wr_cur.rowcount > 0 else 0
    checkpoints = cp_cur.rowcount if cp_cur.rowcount and cp_cur.rowcount > 0 else 0
    return checkpoints, writes


def _safe_to_drop_subgraphs(conn: sqlite3.Connection, serde: Any, thread_id: str, *, deleted_parents: int) -> bool:
    """Require a single surviving final parent with no work to resume."""
    if _subgraph_newer_than_parent(conn, thread_id):
        return False
    if (
        conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ''", (thread_id,)
        ).fetchone()[0]
        - deleted_parents
        != 1
    ):
        return False
    latest = _latest_root_checkpoint(conn, serde, thread_id)
    if latest is None or _has_pending_work(conn, thread_id, *latest):
        return False
    messages = latest[1]["channel_values"].get("messages")
    return (
        isinstance(messages, list)
        and bool(messages)
        and _message_is_ai(messages[-1])
        and not _message_has_tool_calls(messages[-1])
    )


def _latest_root_checkpoint(conn: sqlite3.Connection, serde: Any, thread_id: str) -> tuple[str, dict[str, Any]] | None:
    row = conn.execute(
        "SELECT checkpoint_id, type, checkpoint FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' "
        "ORDER BY checkpoint_id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if row is None:
        return None
    cid, kind, payload = row
    return cid, serde.loads_typed((kind, payload))


def _has_pending_work(conn: sqlite3.Connection, thread_id: str, checkpoint_id: str, checkpoint: dict[str, Any]) -> bool:
    values = checkpoint["channel_values"]
    if (
        any(name.startswith("branch:to:") for name in values)
        or "__start__" in values
        or values.get("__pregel_tasks")
        or values.get("__interrupt__")
        or checkpoint.get("pending_sends")
    ):
        return True
    return bool(
        conn.execute(
            "SELECT 1 FROM writes WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id = ? LIMIT 1",
            (thread_id, checkpoint_id),
        ).fetchone()
    )


def _subgraph_newer_than_parent(conn: sqlite3.Connection, thread_id: str) -> bool:
    """True when any non-parent checkpoint_id sorts after the latest parent."""
    parent = conn.execute(
        "SELECT MAX(checkpoint_id) FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ?",
        (thread_id, ""),
    ).fetchone()
    parent_id = parent[0] if parent else None
    if not parent_id:
        return True
    newer = conn.execute(
        "SELECT 1 FROM checkpoints WHERE thread_id = ? AND checkpoint_ns != '' AND checkpoint_id > ? LIMIT 1",
        (thread_id, parent_id),
    ).fetchone()
    return newer is not None


def _message_has_tool_calls(msg: object) -> bool:
    if isinstance(msg, dict):
        return bool(msg.get("tool_calls"))
    return bool(getattr(msg, "tool_calls", None))


def _message_is_ai(msg: object) -> bool:
    if isinstance(msg, dict):
        return str(msg.get("type") or "") in {"ai", "AIMessage", "AIMessageChunk"}
    name = type(msg).__name__
    if name in {"AIMessage", "AIMessageChunk"}:
        return True
    return getattr(msg, "type", None) == "ai"


def _postgres_memory(memory: Any) -> bool:
    """True when ``memory`` stores on Postgres.

    Idle maintenance calls pruning for every loaded agent. Opening the
    checkpointer there builds a connection pool, so Postgres has to be
    rejected before ``_ensure_checkpointer`` (TencentCloud/Octop#1795).
    """
    backend = getattr(memory, "_backend", None)
    if backend is None:
        return False
    try:
        from octop_memory.storage.backends.postgres import PostgresMemoryBackend
    except ImportError:
        return False

    return isinstance(backend, PostgresMemoryBackend)


def _get_checkpointer_conn(memory: Memory) -> sqlite3.Connection:
    """Return the raw sqlite3 connection backing ``memory``'s checkpointer.

    Raises if the checkpointer isn't LangGraph's SQLite ``SqliteSaver``
    (e.g. no ``langgraph`` extra installed, or PostgreSQL backend) —
    silently no-op'ing would hide the fact that nothing got pruned.

    PostgreSQL is rejected before ``_ensure_checkpointer``. That call builds
    a connection pool, and the hourly maintenance tick would otherwise open
    one per loaded agent even though Postgres pruning always fails.
    """
    if _postgres_memory(memory):
        raise RuntimeError(
            "Checkpoint pruning requires the SQLite checkpointer (langgraph SqliteSaver); "
            "got 'PostgresMemoryBackend' with no usable 'conn' attribute "
            "(PostgreSQL checkpoint pruning is not yet supported)"
        )
    memory._ensure_checkpointer()
    saver = memory._checkpointer
    # getattr on an untyped saver yields Any; narrow explicitly so the
    # declared sqlite3.Connection return type is actually enforced rather
    # than silently satisfied by Any.
    conn = getattr(saver, "conn", None)
    if not isinstance(conn, sqlite3.Connection):
        raise RuntimeError(
            "Checkpoint pruning requires the SQLite checkpointer (langgraph SqliteSaver); "
            f"got {type(saver).__name__!r} with no usable 'conn' attribute "
            "(PostgreSQL checkpoint pruning is not yet supported)"
        )
    return conn


def _thread_ns_pairs(conn: sqlite3.Connection, *, thread_id: str | None) -> list[tuple[str, str]]:
    """Distinct ``(thread_id, checkpoint_ns)`` streams present in the checkpoints table."""
    if thread_id is not None:
        rows = conn.execute(
            "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
        return [(thread_id, r[0]) for r in rows]
    rows = conn.execute("SELECT DISTINCT thread_id, checkpoint_ns FROM checkpoints").fetchall()
    return [(r[0], r[1]) for r in rows]


def _victim_checkpoint_ids(
    conn: sqlite3.Connection,
    thread_id: str,
    checkpoint_ns: str,
    *,
    keep_last: int | None,
    cutoff_id: str | None,
) -> list[str]:
    """Checkpoint ids in this stream that fail both retention checks (candidates for deletion)."""
    rows = conn.execute(
        "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? ORDER BY checkpoint_id DESC",
        (thread_id, checkpoint_ns),
    ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return []

    # Safety net: never prune the single latest checkpoint of an active
    # stream, even if it somehow fails both checks (e.g. keep_last=0
    # would be rejected earlier, but a huge keep_days on a stale clock
    # shouldn't be able to erase the only usable state either).
    survivors: set[str] = {ids[0]}
    if keep_last is not None:
        survivors.update(ids[:keep_last])
    if cutoff_id is not None:
        survivors.update(cid for cid in ids if cid >= cutoff_id)

    return [cid for cid in ids if cid not in survivors]


def _cutoff_checkpoint_id(cutoff: datetime) -> str:
    """Smallest possible checkpoint_id (UUIDv6 string) for the given instant.

    Any real checkpoint_id that string-sorts below this value was
    created strictly before ``cutoff``. Mirrors the bit layout of
    ``langgraph.checkpoint.base.id.uuid6`` with ``clock_seq=0, node=0``
    zeroed out, which yields a lower bound: a real checkpoint minted at
    exactly ``cutoff`` will have non-zero low bits and therefore sort
    at or above this synthetic id.
    """
    nanoseconds = int(cutoff.timestamp() * 1_000_000_000)
    # 0x01b21dd213814000 = 100-ns intervals between the UUID epoch
    # (1582-10-15) and the Unix epoch (1970-01-01).
    timestamp = nanoseconds // 100 + 0x01B21DD213814000
    time_high_and_time_mid = (timestamp >> 12) & 0xFFFFFFFFFFFF
    time_low_and_version = timestamp & 0x0FFF
    uuid_int = time_high_and_time_mid << 80
    uuid_int |= time_low_and_version << 64
    # RFC4122 variant bits + version=6 nibble; clock_seq/node bits are
    # left at 0 (the lower-bound property this function relies on).
    uuid_int &= ~(0xC000 << 48)
    uuid_int |= 0x8000 << 48
    uuid_int &= ~(0xF000 << 64)
    uuid_int |= 6 << 76
    return str(uuid.UUID(int=uuid_int))


__all__ = [
    "DEFAULT_KEEP_LAST_CHECKPOINTS",
    "CheckpointGcStats",
    "prune_checkpoints",
]
