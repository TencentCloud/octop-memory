"""SQLite checkpoint migration and conservative dependency retention.

SQL stays in the backend layer. The application owns the maintenance window,
backup and invocation. No automatic retention is installed by this module.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from itertools import batched, islice

from langgraph.checkpoint.serde.base import SerializerProtocol

from octop_memory.storage.backends.sqlite_checkpoint import FORMAT, CompactSqliteSaver, unpack_envelope


def _can_replay_channel(name: str) -> bool:
    # LangGraph uses these names for scheduling and task bookkeeping, not
    # DeltaChannel data. Consumed branch triggers routinely disappear from
    # channel_values while remaining in channel_versions.
    return not name.startswith("branch:to:") and name not in {
        "__start__",
        "__pregel_tasks",
        "__error__",
        "__interrupt__",
        "__resume__",
        "__return__",
        "__no_writes__",
    }


def retain_checkpoint_dependencies(
    cur: sqlite3.Cursor, serde: SerializerProtocol, thread: str, candidates: list[str]
) -> tuple[list[str], list[str]]:
    """Return deletable IDs and surviving IDs whose parent can be detached.

    A missing channel value can depend on older writes or a stored seed,
    even when the channel is missing from channel_versions. Follow real
    parent links and keep the entire path to each required seed. Every
    ancestor kept for replay must itself remain restorable.
    """
    if not candidates:
        return [], []
    candidate_ids = set(candidates)
    writes: dict[str, set[str]] = defaultdict(set)
    sends: set[str] = set()
    for cid, channel in cur.execute(
        "SELECT DISTINCT checkpoint_id, channel FROM writes WHERE thread_id = ? AND checkpoint_ns = ''", (thread,)
    ):
        if channel == "__pregel_tasks":
            sends.add(cid)
        if _can_replay_channel(channel):
            writes[cid].add(channel)

    channels_through: dict[str, set[str]] = {}
    parents: dict[str, str | None] = {}
    needs_parent: set[str] = set()
    for cid, parent, kind, payload in cur.execute(
        "SELECT checkpoint_id, parent_checkpoint_id, type, checkpoint FROM checkpoints "
        "WHERE thread_id = ? AND checkpoint_ns = '' ORDER BY checkpoint_id",
        (thread,),
    ):
        if parent is not None and parent not in parents:
            raise ValueError("Missing or out-of-order checkpoint ancestor")
        checkpoint = serde.loads_typed((kind, payload))
        values = {name for name in checkpoint["channel_values"] if _can_replay_channel(name)}
        inherited = channels_through[parent] if parent is not None else set()
        version = checkpoint["v"]
        # Older readers can reconstruct pending sends from the parent's
        # writes. An unknown newer format may have additional dependencies.
        if inherited - values or (version < 4 and parent in sends) or version > 4:
            needs_parent.add(cid)
        parents[cid] = parent
        channels_through[cid] = inherited | values | writes.get(cid, set())

    keep = parents.keys() - candidate_ids
    for cid in reversed(parents):
        parent = parents[cid]
        if parent is not None and cid in keep and cid in needs_parent:
            keep.add(parent)
    victims = [cid for cid in candidates if cid not in keep]
    detach = [cid for cid in keep if parents[cid] is not None and parents[cid] not in keep]
    return victims, detach


def delete_checkpoint_candidates(
    cur: sqlite3.Cursor, thread: str, victims: list[str], detach: list[str], *, dry_run: bool
) -> int:
    """Detach only independent boundaries and remove rows in the caller's transaction."""
    if not dry_run:
        cur.executemany(
            "UPDATE checkpoints SET parent_checkpoint_id = NULL "
            "WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id = ?",
            [(thread, cid) for cid in detach],
        )
    writes = 0
    for batch in batched(victims, 500):
        placeholders = ",".join("?" for _ in batch)
        where = f"WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id IN ({placeholders})"
        params = (thread, *batch)
        if dry_run:
            writes += cur.execute(f"SELECT COUNT(*) FROM writes {where}", params).fetchone()[0]
        else:
            writes += cur.execute(f"DELETE FROM writes {where}", params).rowcount
            cur.execute(f"DELETE FROM checkpoints {where}", params)
    return writes


@dataclass
class MigrationStats:
    """Payload metrics are separate from the physical file size after VACUUM."""

    scanned: int = 0
    rewritten: int = 0
    payload_before: int = 0
    payload_after: int = 0
    checkpoints_deleted: int = 0
    writes_deleted: int = 0
    blobs_deleted: int = 0
    skipped_threads: dict[str, str] = field(default_factory=dict)
    dry_run: bool = True


def migrate_checkpoints(
    saver: CompactSqliteSaver,
    *,
    apply: bool = False,
    expand: bool = False,
    batch_size: int = 100,
    through_rowid: int | None = None,
    progress: Callable[[MigrationStats], None] | None = None,
) -> MigrationStats:
    """Rewrite checkpoint payloads in restartable batches; preserve IDs/writes.

    Offline jobs stop all users; the live host uses compatible readers, an
    invocation gate and a fixed through_rowid. Re-running skips converted rows.
    Each batch reserves the writer before reading. A failed batch rolls back
    including its new content. No source mutation for dry runs.
    """
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    stats = MigrationStats(dry_run=not apply)
    last_row = 0
    while True:
        if apply:
            saver.conn.execute("BEGIN IMMEDIATE")
        finished = False
        try:
            # Acquire the write reservation BEFORE reading rows. A live writer
            # cannot replace/delete a row between this read and our UPDATE.
            rows = saver.conn.execute(
                "SELECT rowid, type, checkpoint FROM checkpoints "
                "WHERE rowid > ? AND (? IS NULL OR rowid <= ?) ORDER BY rowid LIMIT ?",
                (last_row, through_rowid, through_rowid, batch_size),
            ).fetchall()
            for rowid, kind, payload in rows:
                stats.scanned += 1
                stats.payload_before += len(payload)
                checkpoint = saver.codec.loads_typed((kind, payload))
                blobs: dict[str, tuple[str, bytes]] = {}
                if expand and kind == FORMAT:
                    next_kind, next_payload = saver.codec.dumps_typed(checkpoint)
                elif not expand and kind != FORMAT:
                    next_kind, next_payload, blobs = saver.codec.encode(checkpoint)
                    if not blobs:
                        next_kind, next_payload = kind, payload
                else:
                    next_kind, next_payload = kind, payload
                changed = (next_kind, next_payload) != (kind, payload)
                stats.payload_after += len(next_payload)
                if changed:
                    stats.rewritten += 1
                if apply and changed:
                    saver.conn.executemany(
                        "INSERT OR IGNORE INTO hm_checkpoint_blobs (digest, type, value) VALUES (?, ?, ?)",
                        [(digest, *item) for digest, item in blobs.items()],
                    )
                    # Read actual stored content before replacing the old row.
                    saver.codec.cache.clear()
                    saver.codec.cache_size = 0
                    if saver.codec.loads_typed((next_kind, next_payload)) != checkpoint:
                        raise ValueError(f"Checkpoint round-trip mismatch at rowid {rowid}")
                    saver.conn.execute(
                        "UPDATE checkpoints SET type = ?, checkpoint = ? WHERE rowid = ?",
                        (next_kind, next_payload, rowid),
                    )
            if apply:
                saver.conn.commit()
            finished = True
        finally:
            if not finished:
                if apply:
                    saver.conn.rollback()
                saver.codec.cache.clear()
                saver.codec.cache_size = 0
        if not rows:
            break
        last_row = rows[-1][0]
        if progress is not None:
            progress(stats)
    return stats


def prune_completed_threads(
    saver: CompactSqliteSaver,
    stats: MigrationStats,
    *,
    completed_threads: tuple[str, ...],
    keep_last: int,
    protected_ids: tuple[str, ...] = (),
    apply: bool = False,
) -> None:
    """Prune only caller-approved completed threads with provable full seeds.

    Keep newest N plus explicit external references and their dependency
    closure. An ancestor chain can end early only at a snapshot containing ALL
    versioned channels, with no pending writes/tasks. Unknown/custom delta
    chains and any subgraphs are preserved, not reconstructed heuristically.
    """
    if keep_last < 1:
        raise ValueError("keep_last must be >= 1")
    for thread in completed_threads:
        if apply:
            saver.conn.execute("BEGIN IMMEDIATE")
        finished = False
        try:
            _prune_thread(saver, stats, thread, keep_last, set(protected_ids), apply)
            if apply:
                saver.conn.commit()
            finished = True
        finally:
            if apply and not finished:
                saver.conn.rollback()


@dataclass(frozen=True, slots=True)
class _RetentionNode:
    parent: str | None
    full_seed: bool


def _prune_thread(
    saver: CompactSqliteSaver, stats: MigrationStats, thread: str, keep_last: int, protected: set[str], apply: bool
) -> None:
    if saver.conn.execute(
        "SELECT 1 FROM checkpoints WHERE thread_id = ? AND checkpoint_ns != '' LIMIT 1",
        (thread,),
    ).fetchone():
        stats.skipped_threads[thread] = "Subgraph dependencies require host validation"
        return
    writes = {
        row[0] for row in saver.conn.execute("SELECT DISTINCT checkpoint_id FROM writes WHERE thread_id = ?", (thread,))
    }
    # Keep only dependency metadata, not a full decoded copy of every body.
    # Cursor iteration bounds payload memory even for large inline old rows.
    by_id: dict[str, _RetentionNode] = {}
    cursor = saver.conn.execute(
        "SELECT checkpoint_id, parent_checkpoint_id, type, checkpoint "
        "FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' ORDER BY checkpoint_id DESC",
        (thread,),
    )
    try:
        for cid, parent, kind, payload in cursor:
            cp = saver.codec.loads_typed((kind, payload))
            values = cp["channel_values"]
            pending_tasks = bool(cid in writes or cp.get("pending_sends") or values.get("__pregel_tasks"))
            if not by_id and pending_tasks:
                stats.skipped_threads[thread] = "Latest checkpoint has pending writes/tasks"
                return
            by_id[cid] = _RetentionNode(parent, set(cp["channel_versions"]).issubset(values) and not pending_tasks)
            del cp, values
    finally:
        cursor.close()
    if not by_id:
        stats.skipped_threads[thread] = "No checkpoints"
        return
    keep = set(islice(by_id, keep_last)) | (protected & by_id.keys())
    roots: set[str] = set()
    pending = list(keep)
    visited: set[str] = set()
    while pending:
        cid = pending.pop()
        if cid in visited:
            # A cycle cannot be proved resumable. Inspect the parent walk
            # separately below before doing any mutations.
            continue
        visited.add(cid)
        node = by_id[cid]
        parent = node.parent
        if node.full_seed:
            roots.add(cid)
        elif parent:
            if parent not in by_id:
                stats.skipped_threads[thread] = "Missing ancestor of a retained delta checkpoint"
                return
            keep.add(parent)
            pending.append(parent)
        else:
            stats.skipped_threads[thread] = "Retained root is missing versioned channel values"
            return
    # Every retained node must reach a validated root; cyclic chains fail closed.
    resolved = set(roots)
    for start in keep:
        seen: set[str] = set()
        cid = start
        while cid not in resolved:
            if cid in seen:
                stats.skipped_threads[thread] = "Cyclic checkpoint ancestry"
                return
            seen.add(cid)
            parent = by_id[cid].parent
            assert parent is not None  # Non-root ancestry was validated above.
            cid = parent
        resolved.update(seen)
    victims = by_id.keys() - keep
    with closing(saver.conn.cursor()) as cur:
        try:
            deletable, detach = retain_checkpoint_dependencies(cur, saver.serde, thread, sorted(victims))
        except ValueError as exc:
            stats.skipped_threads[thread] = str(exc)
            return
        if not deletable:
            stats.skipped_threads[thread] = "Retention or delta dependencies protect all checkpoints"
            return
        stats.checkpoints_deleted += len(deletable)
        stats.writes_deleted += delete_checkpoint_candidates(cur, thread, deletable, detach, dry_run=not apply)


def collect_unused_blobs(saver: CompactSqliteSaver) -> int:
    """Offline mark/sweep after a verified migration; no cached live readers."""
    with saver.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS hm_live_checkpoint_blobs (digest TEXT PRIMARY KEY)")
        cur.execute("DELETE FROM hm_live_checkpoint_blobs")
        for kind, payload in saver.conn.execute("SELECT type, checkpoint FROM checkpoints"):
            # Validate all checkpoints and content before deleting any blobs.
            saver.codec.loads_typed((kind, payload))
            if kind == FORMAT:
                _, refs, _ = unpack_envelope(payload)
                cur.executemany(
                    "INSERT OR IGNORE INTO hm_live_checkpoint_blobs VALUES (?)", [(v,) for v in refs.values()]
                )
        cur.execute("DELETE FROM hm_checkpoint_blobs WHERE digest NOT IN (SELECT digest FROM hm_live_checkpoint_blobs)")
        deleted = cur.rowcount
    saver.codec.cache.clear()
    saver.codec.cache_size = 0
    return deleted
