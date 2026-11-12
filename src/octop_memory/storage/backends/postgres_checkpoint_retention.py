"""Checkpoint retention for LangGraph's native PostgreSQL tables."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, BinaryIO

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.errors import LockNotAvailable
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from octop_memory.storage.backends.checkpoint_retention import (
    CheckpointPrunePlan,
    checkpoint_ends_with_final_reply,
    checkpoint_has_pending_work,
    plan_checkpoint_dependencies,
    select_victim_ids,
)
from octop_memory.storage.backends.postgres_pool import acquire_pool
from octop_memory.storage.driver_errors import REPORTABLE_ERRORS

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.channels.delta import DeltaChannel
    from langgraph.checkpoint.base import CheckpointTuple, DeltaChannelHistory
    from psycopg import Connection, Cursor

    from octop_memory.storage.backends.postgres import PostgresMemoryBackend

_LOG = logging.getLogger(__name__)
_SNAPSHOT_MARKER = "_octop_memory_messages_snapshot"
_TABLES = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")
type _Cursor = Cursor[tuple[Any, ...]]


@contextmanager
def _connection(backend: PostgresMemoryBackend) -> Iterator[Connection[dict[str, Any]]]:
    # Share LangGraph's connection budget without initializing DDL or retaining
    # a new pool lease for every idle agent.
    lease = acquire_pool(backend._dsn, purpose="checkpoint", connect_kwargs=backend._connect_kwargs)
    try:
        with lease.connection() as conn:
            yield conn
    finally:
        lease.close()


def backup_checkpoint_tables(backend: PostgresMemoryBackend, output: BinaryIO) -> None:
    """Write a transactional SQL data backup for a matching initialized schema."""
    with _connection(backend) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute("SET LOCAL client_encoding TO 'UTF8'")
        output.write(b"\\set ON_ERROR_STOP on\nBEGIN;\nSET LOCAL client_encoding TO 'UTF8';\n")
        for table in _TABLES:
            # Names are fixed by the saver. COPY text escapes record separators.
            output.write(f"COPY {table} FROM stdin;\n".encode())
            with cur.copy(f"COPY {table} TO STDOUT") as copy:
                for block in copy:
                    output.write(block)
            output.write(b"\\.\n")
        output.write(b"COMMIT;\n")


def _config(thread: str, cid: str | None = None) -> RunnableConfig:
    cfg = {"thread_id": thread, "checkpoint_ns": ""}
    if cid is not None:
        cfg["checkpoint_id"] = cid
    return {"configurable": cfg}


def _history(reader: PostgresSaver, config: RunnableConfig, channels: Sequence[str]) -> dict[str, DeltaChannelHistory]:
    # Memory's existing replay resolves native blobs via get_tuple, including
    # older blobs whose channel has no inline JSON marker.
    return dict(BaseCheckpointSaver.get_delta_channel_history(reader, config=config, channels=channels))


def read_delta_history(
    saver: PostgresSaver, config: RunnableConfig, channels: Sequence[str]
) -> dict[str, DeltaChannelHistory]:
    """Read the whole chain in one MVCC snapshot, including a concurrent cut."""
    pool = saver.conn
    if not isinstance(pool, ConnectionPool):
        raise TypeError("PostgreSQL replay requires a pooled checkpoint saver")
    with pool.connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        reader = PostgresSaver(conn, serde=saver.serde)
        target = reader.get_tuple(config) if "messages" in channels else None
        if target and target.parent_config is None and target.checkpoint.get(_SNAPSHOT_MARKER):
            # A caller can read the boundary tuple just before it is detached
            # and ask for replay just after. The stored seed is its exact state.
            rest = [name for name in channels if name != "messages"]
            history = _history(reader, config, rest)
            history["messages"] = {"seed": target.checkpoint["channel_values"]["messages"], "writes": []}
            return history
        return _history(reader, config, channels)


def _has_pending(cur: _Cursor, thread: str, latest: CheckpointTuple) -> bool:
    return (
        checkpoint_has_pending_work(latest.checkpoint)
        or cur.execute(
            "SELECT 1 FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_ns = '' "
            "AND checkpoint_id = %s LIMIT 1",
            (thread, latest.checkpoint["id"]),
        ).fetchone()
        is not None
    )


def _plan(cur: _Cursor, thread: str, candidates: list[str], *, snapshot_messages: bool) -> CheckpointPrunePlan:
    if not candidates:
        return CheckpointPrunePlan([], [], [])
    writes = cur.execute(
        "SELECT DISTINCT checkpoint_id, channel FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_ns = ''",
        (thread,),
    ).fetchall()
    # Native PostgresSaver joins by channel version, including old rows
    # without an inline marker. Read blob keys, never blob bodies, for planning.
    cur.execute(
        """
        SELECT c.checkpoint_id, c.parent_checkpoint_id, c.checkpoint,
               ARRAY(SELECT b.channel FROM jsonb_each_text(c.checkpoint->'channel_versions') v
                     JOIN checkpoint_blobs b ON b.thread_id = c.thread_id AND b.checkpoint_ns = c.checkpoint_ns
                       AND b.channel = v.key AND b.version = v.value WHERE b.type != 'empty')
        FROM checkpoints c
        WHERE c.thread_id = %s AND c.checkpoint_ns = '' ORDER BY c.checkpoint_id
        """,
        (thread,),
    )
    rows = (
        (cid, parent, cp["v"], set(cp.get("channel_values") or {}) | set(blobs), "messages" in cp["channel_versions"])
        for cid, parent, cp, blobs in cur
    )
    return plan_checkpoint_dependencies(rows, writes, candidates, snapshot_messages=snapshot_messages)


def _replay_messages(reader: PostgresSaver, thread: str, cid: str, channel: DeltaChannel[list[Any]]) -> Any:
    from langgraph._internal._typing import MISSING
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    history = _history(reader, _config(thread, cid), ["messages"])["messages"]
    messages = channel.from_checkpoint(history.get("seed", MISSING))
    messages.replay_writes(history["writes"])
    return _DeltaSnapshot(messages.get())


def _prepare_snapshots(
    cur: _Cursor, reader: PostgresSaver, thread: str, plan: CheckpointPrunePlan, channel: DeltaChannel[list[Any]]
) -> list[tuple[str, str, bytes]]:
    snapshots = []
    for cid in plan.message_snapshot_ids:
        row = cur.execute(
            "SELECT checkpoint->'channel_versions'->>'messages' FROM checkpoints "
            "WHERE thread_id = %s AND checkpoint_ns = '' AND checkpoint_id = %s",
            (thread, cid),
        ).fetchone()
        if row is None or row[0] is None:
            raise ValueError("Missing PostgreSQL message snapshot version")
        version = row[0]
        seed = _replay_messages(reader, thread, cid, channel)
        # A native blob is shared by all rows with the same channel version.
        # Verify their state before filling that blob and preserve the version.
        peers = cur.execute(
            "SELECT checkpoint_id, (checkpoint->'channel_values') ? 'messages' FROM checkpoints "
            "WHERE thread_id = %s AND checkpoint_ns = '' "
            "AND checkpoint->'channel_versions'->>'messages' = %s AND checkpoint_id <> ALL(%s) "
            "AND checkpoint_id <> %s",
            (thread, version, plan.victims, cid),
        ).fetchall()
        for peer, inline in peers:
            if inline or _replay_messages(reader, thread, peer, channel) != seed:
                raise ValueError("PostgreSQL checkpoints share a message version with different states")
        kind, blob = reader.serde.dumps_typed(seed)
        snapshots.append((version, kind, blob))
    return snapshots


def _apply(cur: _Cursor, thread: str, plan: CheckpointPrunePlan, snapshots: list[tuple[str, str, bytes]]) -> int:
    for version, kind, blob in snapshots:
        cur.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (%s, '', 'messages', %s, %s, %s) "
            "ON CONFLICT (thread_id, checkpoint_ns, channel, version) DO UPDATE "
            "SET type = EXCLUDED.type, blob = EXCLUDED.blob "
            "WHERE checkpoint_blobs.type = 'empty' OR "
            "(checkpoint_blobs.type, checkpoint_blobs.blob) = (EXCLUDED.type, EXCLUDED.blob)",
            (thread, version, kind, blob),
        )
        if cur.rowcount != 1:
            raise ValueError("Refusing to overwrite a different PostgreSQL message snapshot")
        cur.execute(
            "UPDATE checkpoints SET checkpoint = checkpoint || %s "
            "|| jsonb_build_object('channel_values', COALESCE(checkpoint->'channel_values', '{}'::jsonb) "
            "|| '{\"messages\": true}'::jsonb) "
            "WHERE thread_id = %s AND checkpoint_ns = '' "
            "AND checkpoint->'channel_versions'->>'messages' = %s AND checkpoint_id <> ALL(%s)",
            (Jsonb({_SNAPSHOT_MARKER: True}), thread, version, plan.victims),
        )
    if not plan.victims:
        return 0
    cur.execute(
        "UPDATE checkpoints SET parent_checkpoint_id = NULL "
        "WHERE thread_id = %s AND checkpoint_ns = '' AND checkpoint_id = ANY(%s)",
        (thread, plan.detach),
    )
    cur.execute(
        "DELETE FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_ns = '' AND checkpoint_id = ANY(%s)",
        (thread, plan.victims),
    )
    writes = cur.rowcount
    # Only consider versions referenced by the deleted rows. A surviving
    # checkpoint in this namespace can still refer to the same native blob.
    cur.execute(
        """
        WITH deleted AS (
            DELETE FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = '' AND checkpoint_id = ANY(%s)
            RETURNING checkpoint
        ), versions AS (
            SELECT DISTINCT v.key AS channel, v.value AS version
            FROM deleted d CROSS JOIN LATERAL jsonb_each_text(d.checkpoint->'channel_versions') v
        )
        DELETE FROM checkpoint_blobs b USING versions v
        WHERE b.thread_id = %s AND b.checkpoint_ns = '' AND b.channel = v.channel AND b.version = v.version
          AND NOT EXISTS (
              SELECT 1 FROM checkpoints c WHERE c.thread_id = b.thread_id AND c.checkpoint_ns = b.checkpoint_ns
                AND c.checkpoint_id <> ALL(%s) AND c.checkpoint->'channel_versions'->>b.channel = b.version
          )
        """,
        (thread, plan.victims, thread, plan.victims),
    )
    return writes


def _prune_thread(
    cur: _Cursor,
    reader: PostgresSaver,
    thread: str,
    *,
    keep_last: int | None,
    cutoff_id: str | None,
    drop_subgraph_streams: bool,
    force_drop: bool,
    skip_pending: bool,
    dry_run: bool,
    messages_channel: DeltaChannel[list[Any]] | None,
) -> tuple[int, int, int, int, bool]:
    cur.execute("SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id = %s", (thread,))
    namespaces = [ns for (ns,) in cur]
    subgraphs = [ns for ns in namespaces if ns]
    skipped = (len(namespaces), 0, 0, 0, True)
    if subgraphs and not drop_subgraph_streams:
        return skipped
    cur.execute(
        "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = '' ORDER BY checkpoint_id DESC",
        (thread,),
    )
    roots = [cid for (cid,) in cur]
    candidates = select_victim_ids(roots, keep_last=keep_last, cutoff_id=cutoff_id)
    try:
        latest = (
            reader.get_tuple(_config(thread))
            if (skip_pending and candidates) or (subgraphs and not force_drop)
            else None
        )
        pending = latest is not None and _has_pending(cur, thread, latest)
        if skip_pending and candidates and pending:
            return skipped
        plan = _plan(cur, thread, candidates, snapshot_messages=messages_channel is not None and not subgraphs)
        if subgraphs and not force_drop:
            if latest is None or pending or len(roots) - len(plan.victims) != 1:
                return skipped
            if (
                not checkpoint_ends_with_final_reply(latest.checkpoint)
                or cur.execute(
                    "SELECT 1 FROM checkpoints WHERE thread_id = %s AND checkpoint_ns <> '' "
                    "AND checkpoint_id > %s LIMIT 1",
                    (thread, latest.checkpoint["id"]),
                ).fetchone()
            ):
                return skipped
        snapshots = (
            _prepare_snapshots(cur, reader, thread, plan, messages_channel)
            if messages_channel is not None and plan.message_snapshot_ids
            else []
        )
    except REPORTABLE_ERRORS:
        _LOG.warning("prune_checkpoints: cannot verify ancestry for thread=%s", thread, exc_info=True)
        return skipped

    checkpoints = len(plan.victims)
    if dry_run:
        row = cur.execute(
            "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_ns = '' "
            "AND checkpoint_id = ANY(%s)",
            (thread, plan.victims),
        ).fetchone()
        writes = row[0] if row else 0
    else:
        writes = _apply(cur, thread, plan, snapshots)
    for ns in subgraphs:
        if dry_run:
            cp = cur.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s", (thread, ns)
            )
            cp_row = cp.fetchone()
            checkpoints += cp_row[0] if cp_row else 0
            wr = cur.execute(
                "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_ns = %s", (thread, ns)
            )
            wr_row = wr.fetchone()
            writes += wr_row[0] if wr_row else 0
        else:
            cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_ns = %s", (thread, ns))
            writes += cur.rowcount
            cur.execute("DELETE FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s", (thread, ns))
            checkpoints += cur.rowcount
            cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s AND checkpoint_ns = %s", (thread, ns))
    return len(namespaces), len(subgraphs), checkpoints, writes, False


def prune_postgres(
    backend: PostgresMemoryBackend, *, thread_id: str | None, dry_run: bool, **options: Any
) -> Iterator[tuple[int, int, int, int, bool]]:
    with _connection(backend) as conn, conn.cursor(row_factory=tuple_row) as cur:
        tables = cur.execute(
            "SELECT to_regclass('checkpoints'), to_regclass('checkpoint_writes'), to_regclass('checkpoint_blobs')"
        ).fetchone()
        if not tables or not all(tables):
            return
        cur.execute(
            "SELECT DISTINCT thread_id FROM checkpoints WHERE (%s::text IS NULL OR thread_id = %s) ORDER BY thread_id",
            (thread_id, thread_id),
        )
        threads = [thread for (thread,) in cur]
        reader = PostgresSaver(conn)
        for thread in threads:
            try:
                with conn.transaction():
                    if dry_run:
                        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    else:
                        # The saver writes all three tables. Reserve the writer
                        # before planning without waiting behind a live writer.
                        cur.execute(
                            "LOCK TABLE checkpoints, checkpoint_blobs, checkpoint_writes "
                            "IN SHARE ROW EXCLUSIVE MODE NOWAIT"
                        )
                    result = _prune_thread(cur, reader, thread, dry_run=dry_run, **options)
            except LockNotAvailable:
                _LOG.debug("prune_checkpoints: concurrent PostgreSQL writer for thread=%s", thread)
                result = (0, 0, 0, 0, True)
            yield result
