"""Prune checkpoint history while preserving the state of every survivor.

The retention window keeps the latest checkpoint and any checkpoint matching
the count or age limit. It also keeps the checkpoints and writes required to
replay that state. A retained checkpoint keeps its ID and writes.
Only an independent boundary can be detached from a deleted ancestor.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from octop_memory.storage.backends.checkpoint_retention import (
    checkpoint_ends_with_final_reply,
    checkpoint_has_pending_work,
    select_victim_ids,
)
from octop_memory.storage.driver_errors import REPORTABLE_ERRORS

_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langgraph.channels.delta import DeltaChannel

    from octop_memory.core import Memory
    from octop_memory.storage.backends.postgres import PostgresMemoryBackend

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
    messages_channel: DeltaChannel[list[Any]] | None = None,
) -> CheckpointGcStats:
    """Delete checkpoints outside the retention window and dependency closure.

    The newest root checkpoint is always kept on SQLite and PostgreSQL.
    Other checkpoints match the window if they are among the newest
    ``keep_last`` or at most ``keep_days`` old. Ancestors needed for replay
    can make the resulting history longer than this window.

    Threads with subgraphs keep their cross-stream dependencies. An explicit
    ``drop_subgraph_streams`` can remove subgraphs when the only surviving
    parent is a completed checkpoint with no pending work. The force flag
    overrides those subgraph guards. Dry runs use the same plan as deletion.
    With ``skip_pending``, leave threads with work pending at their head intact.

    Hosts can pass their graph's ``messages_channel`` to snapshot messages at
    a retained boundary whose only replay dependency is that channel. The
    channel must use the same reducer as the stored threads. This applies
    to root-only threads with a supported SQLite or PostgreSQL saver.
    """
    if keep_last is None and keep_days is None:
        raise ValueError("prune_checkpoints requires keep_last and/or keep_days")
    if keep_last is not None and keep_last < 1:
        raise ValueError("keep_last must be >= 1")
    if keep_days is not None and keep_days < 0:
        raise ValueError("keep_days must be >= 0")

    stats = CheckpointGcStats(started_at=datetime.now(UTC), dry_run=dry_run)
    cutoff_id = None
    if keep_days is not None:
        cutoff_id = _cutoff_checkpoint_id((now or datetime.now(UTC)) - timedelta(days=keep_days))
    force_drop = drop_subgraph_streams and force_drop_subgraphs
    options: dict[str, Any] = {
        "keep_last": keep_last,
        "cutoff_id": cutoff_id,
        "drop_subgraph_streams": drop_subgraph_streams,
        "force_drop": force_drop,
        "skip_pending": skip_pending,
        "dry_run": dry_run,
        "messages_channel": messages_channel,
    }
    if backend := _postgres_backend(memory):
        from octop_memory.storage.backends.postgres_checkpoint_retention import prune_postgres

        for streams, dropped, checkpoints, writes, skipped in prune_postgres(backend, thread_id=thread_id, **options):
            stats.threads_scanned += 1
            stats.streams_scanned += streams
            stats.streams_dropped += dropped
            stats.checkpoints_deleted += checkpoints
            stats.writes_deleted += writes
            stats.parent_prunes_skipped += int(skipped)
    else:
        _prune_sqlite(memory, stats, thread_id=thread_id, **options)

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


def _prune_sqlite(
    memory: Memory,
    stats: CheckpointGcStats,
    *,
    thread_id: str | None,
    keep_last: int | None,
    cutoff_id: str | None,
    drop_subgraph_streams: bool,
    force_drop: bool,
    skip_pending: bool,
    dry_run: bool,
    messages_channel: DeltaChannel[list[Any]] | None,
) -> None:
    from octop_memory.storage.backends.sqlite_checkpoint import CompactSqliteSaver
    from octop_memory.storage.backends.sqlite_checkpoint_maintenance import (
        delete_checkpoint_candidates,
        plan_checkpoint_pruning,
        prepare_checkpoint_message_snapshots,
        write_checkpoint_message_snapshots,
    )

    conn = _get_checkpointer_conn(memory)
    saver = memory._checkpointer

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
                can_snapshot = messages_channel is not None and isinstance(saver, CompactSqliteSaver) and not subgraphs
                plan = plan_checkpoint_pruning(cur, saver.serde, tid, candidates, snapshot_messages=can_snapshot)
                if (
                    subgraphs
                    and not force_drop
                    and not _safe_to_drop_subgraphs(conn, saver.serde, tid, deleted_parents=len(plan.victims))
                ):
                    stats.parent_prunes_skipped += 1
                    continue
                snapshots = (
                    prepare_checkpoint_message_snapshots(cur, saver, tid, plan.message_snapshot_ids, messages_channel)
                    if plan.message_snapshot_ids and messages_channel is not None
                    else []
                )
            except REPORTABLE_ERRORS:
                stats.parent_prunes_skipped += 1
                _LOG.warning("prune_checkpoints: cannot verify ancestry for thread=%s", tid, exc_info=True)
                continue

            if snapshots and not dry_run:
                write_checkpoint_message_snapshots(cur, tid, snapshots)
            stats.writes_deleted += delete_checkpoint_candidates(cur, tid, plan.victims, plan.detach, dry_run=dry_run)
            stats.checkpoints_deleted += len(plan.victims)
            for ns in subgraphs:
                checkpoints, writes = _delete_stream(conn, tid, ns, dry_run=dry_run)
                stats.checkpoints_deleted += checkpoints
                stats.writes_deleted += writes
                stats.streams_dropped += int(bool(checkpoints or writes))


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
    return checkpoint_ends_with_final_reply(latest[1])


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
    if checkpoint_has_pending_work(checkpoint):
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


def _postgres_backend(memory: Memory) -> PostgresMemoryBackend | None:
    """Find a PostgreSQL backend without opening its agent checkpointer."""
    backend = getattr(memory, "_backend", None)
    if backend is None:
        return None
    try:
        from octop_memory.storage.backends.postgres import PostgresMemoryBackend
    except ImportError:
        return None

    return backend if isinstance(backend, PostgresMemoryBackend) else None


def _get_checkpointer_conn(memory: Memory) -> sqlite3.Connection:
    """Return the raw connection for the SQLite retention path."""
    memory._ensure_checkpointer()
    saver = memory._checkpointer
    # getattr on an untyped saver yields Any; narrow explicitly so the
    # declared sqlite3.Connection return type is actually enforced rather
    # than silently satisfied by Any.
    conn = getattr(saver, "conn", None)
    if not isinstance(conn, sqlite3.Connection):
        raise RuntimeError(
            "Checkpoint pruning requires the SQLite checkpointer (langgraph SqliteSaver); "
            f"got {type(saver).__name__!r} with no usable 'conn' attribute"
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
    return select_victim_ids([r[0] for r in rows], keep_last=keep_last, cutoff_id=cutoff_id)


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
