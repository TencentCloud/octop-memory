"""Memory written before the 1.0 rename (schema ``harness_memory``) reaches the current schema.

Each test gets its own throwaway database on the ``TEST_POSTGRES_DSN`` server,
because the legacy schema is shared by every namespace in a database. The
role needs ``CREATEDB``.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

psycopg = pytest.importorskip("psycopg")

from octop_memory.core import Memory  # noqa: E402
from octop_memory.storage.backends import postgres_legacy  # noqa: E402
from octop_memory.types import MemoryNode  # noqa: E402


@pytest.fixture
def dsn(postgres_dsn: str) -> Iterator[str]:
    from psycopg.conninfo import make_conninfo

    name = f"octop_memory_legacy_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(postgres_dsn, dbname=name)
    finally:
        with psycopg.connect(postgres_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _open(dsn: str, ns: str) -> Memory:
    return Memory(namespace=ns, backend="postgres", backend_config={"dsn": dsn})


def _write(dsn: str, ns: str, *, tag: str) -> None:
    memory = _open(dsn, ns)
    try:
        for i in range(3):
            memory.add_raw(f"{tag} event {i}", event_type="user_message")
        now = datetime.now(UTC)
        branch = MemoryNode(
            id=f"{tag}-branch",
            parent_id=None,
            level="branch",
            content=f"{tag} topic",
            topic=f"{tag} topic",
            conversation_id=None,
            created_at=now,
            updated_at=now,
        )
        child = MemoryNode(
            id=f"{tag}-child",
            parent_id=branch.id,
            level="branch",
            content=f"{tag} subtopic",
            topic=None,
            conversation_id=None,
            created_at=now,
            updated_at=now,
        )
        memory._backend.save_node(branch)
        memory._backend.save_node(child)
        memory.set_meta("extract_cursor", tag)
    finally:
        memory._backend.close()


def _rename_to_legacy(dsn: str) -> None:
    """What a 0.9.x install left behind: the shared tables in ``harness_memory``."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("ALTER SCHEMA octop_memory RENAME TO harness_memory")
        # 0.9.x predates entity_pages.mutation_version.
        conn.execute("ALTER TABLE harness_memory.entity_pages DROP COLUMN mutation_version")


def _count(dsn: str, schema: str, table: str, ns: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(f"SELECT count(*) FROM {schema}.{table} WHERE namespace = %s", (ns,)).fetchone()
    return int(row[0])


def _meta(dsn: str, key: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT value FROM octop_memory.meta WHERE key = %s", (key,)).fetchone()
    return None if row is None else str(row[0])


def test_empty_namespace_is_migrated_on_open(dsn: str) -> None:
    _write(dsn, "agent_a", tag="a")
    _rename_to_legacy(dsn)

    memory = _open(dsn, "agent_a")
    try:
        assert memory._backend.count_stats()["raw_events"] == 3
        assert {n.id for n in memory._backend.get_tree()} >= {"a-branch", "a-child"}
        assert memory.get_meta("extract_cursor") == "a"
        assert memory.legacy_schema_status()["pending"] is False
        assert memory.legacy_schema_status()["migrated"]["mode"] == "auto"
    finally:
        memory._backend.close()
    # The shared legacy schema is left as it was.
    assert _count(dsn, "harness_memory", "raw_events", "agent_a") == 3


def test_marker_keeps_purged_memory_from_coming_back(dsn: str) -> None:
    _write(dsn, "agent_a", tag="a")
    _rename_to_legacy(dsn)
    memory = _open(dsn, "agent_a")
    try:
        memory._backend.purge_namespace()
    finally:
        memory._backend.close()

    memory = _open(dsn, "agent_a")
    try:
        assert memory._backend.count_stats()["raw_events"] == 0
        assert memory.merge_legacy_schema(dry_run=False)["status"] == "purged"
    finally:
        memory._backend.close()


def test_namespaces_migrate_independently(dsn: str) -> None:
    _write(dsn, "agent_a", tag="a")
    _write(dsn, "agent_b", tag="b")
    _rename_to_legacy(dsn)

    _open(dsn, "agent_a")._backend.close()
    assert _count(dsn, "octop_memory", "raw_events", "agent_a") == 3
    assert _count(dsn, "octop_memory", "raw_events", "agent_b") == 0

    _open(dsn, "agent_b")._backend.close()
    assert _count(dsn, "octop_memory", "raw_events", "agent_b") == 3
    assert _meta(dsn, "agent_a:extract_cursor") == "a"
    assert _meta(dsn, "agent_b:extract_cursor") == "b"


def _start_with_new_memory(dsn: str) -> None:
    """Legacy rows for agent_a, plus one of them already rewritten after the upgrade."""
    _write(dsn, "agent_a", tag="a")
    _rename_to_legacy(dsn)
    _open(dsn, "agent_other")._backend.close()  # creates the current tables only
    with psycopg.connect(dsn, autocommit=True) as conn:
        cols = [
            str(r[0])
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = "
                "'octop_memory' AND table_name = 'raw_events' AND is_generated = 'NEVER'"
            )
        ]
        col_list = ", ".join(f'"{c}"' for c in cols)
        conn.execute(
            f"INSERT INTO octop_memory.raw_events ({col_list}) SELECT {col_list} "
            "FROM harness_memory.raw_events WHERE namespace = 'agent_a' ORDER BY id LIMIT 1"
        )
        conn.execute("INSERT INTO octop_memory.meta (key, value) VALUES ('agent_a:extract_cursor', 'new')")


def test_namespace_with_new_memory_waits_for_explicit_merge(dsn: str) -> None:
    _start_with_new_memory(dsn)

    memory = _open(dsn, "agent_a")
    try:
        assert memory._backend.count_stats()["raw_events"] == 1
        status = memory.legacy_schema_status()
        assert status["pending"] is True
        assert status["rows"]["raw_events"] == 3
        assert status["rows"]["memory_nodes"] == 2

        plan = memory.merge_legacy_schema()
        assert plan["status"] == "dry_run"
        assert plan["exact"] is True
        assert plan["blocked"] == []
        assert plan["tables"]["raw_events"] == {"insert": 2, "skip": 1}
        assert plan["tables"]["memory_nodes"] == {"insert": 2, "skip": 0}
        assert plan["meta_keys"] == 0
        assert memory._backend.count_stats()["raw_events"] == 1  # dry run wrote nothing

        done = memory.merge_legacy_schema(dry_run=False)
        assert done["status"] == "merged"
        assert done["tables"] == plan["tables"]
        assert memory._backend.count_stats()["raw_events"] == 3
        assert memory.get_meta("extract_cursor") == "new"  # newer value wins
        assert memory.legacy_schema_status()["pending"] is False
        assert memory.merge_legacy_schema(dry_run=False)["status"] == "already_migrated"
    finally:
        memory._backend.close()


def test_failed_merge_writes_nothing(dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _start_with_new_memory(dsn)
    real = postgres_legacy._copy_table
    calls: list[str] = []

    def copy_then_fail(cur: Any, table: str, ns: str) -> tuple[int, int]:
        calls.append(table)
        if len(calls) == 2:
            raise psycopg.errors.QueryCanceled("simulated failure")
        return real(cur, table, ns)

    memory = _open(dsn, "agent_a")
    try:
        monkeypatch.setattr(postgres_legacy, "_copy_table", copy_then_fail)
        with pytest.raises(psycopg.errors.QueryCanceled):
            memory.merge_legacy_schema(dry_run=False)
        monkeypatch.undo()

        assert memory._backend.count_stats()["raw_events"] == 1
        assert _count(dsn, "octop_memory", "memory_nodes", "agent_a") == 0
        assert memory.legacy_schema_status()["pending"] is True
    finally:
        memory._backend.close()


def test_no_legacy_schema_reports_nothing(dsn: str) -> None:
    memory = _open(dsn, "agent_a")
    try:
        assert memory.legacy_schema_status() == {
            "supported": True,
            "pending": False,
            "migrated": None,
            "rows": {},
            "meta_keys": 0,
            "legacy_grew": {},
        }
        assert memory.merge_legacy_schema(dry_run=False)["status"] == "nothing_to_merge"
    finally:
        memory._backend.close()


def test_purge_before_migration_discards_legacy_memory(dsn: str) -> None:
    _start_with_new_memory(dsn)
    memory = _open(dsn, "agent_a")
    try:
        assert memory.legacy_schema_status()["pending"] is True
        memory._backend.purge_namespace()
    finally:
        memory._backend.close()

    memory = _open(dsn, "agent_a")
    try:
        assert memory._backend.count_stats()["raw_events"] == 0
        status = memory.legacy_schema_status()
        assert status["pending"] is False
        assert status["migrated"]["mode"] == "purged"
        assert memory.merge_legacy_schema(dry_run=False)["status"] == "purged"
    finally:
        memory._backend.close()


def test_namespace_named_like_the_legacy_schema_copies_nothing(dsn: str) -> None:
    _write(dsn, "agent_a", tag="a")
    _write(dsn, "agent_b", tag="b")
    _rename_to_legacy(dsn)

    memory = _open(dsn, "harness_memory")
    try:
        assert memory._backend.count_stats()["raw_events"] == 0
    finally:
        memory._backend.close()
    with psycopg.connect(dsn) as conn:
        schemas = {str(r[0]) for r in conn.execute("SELECT nspname FROM pg_namespace")}
        rows = conn.execute("SELECT count(*) FROM harness_memory.raw_events").fetchone()
    assert "harness_memory" in schemas
    assert "harness_memory__old" not in schemas
    assert rows is not None and rows[0] == 6


def test_old_writer_after_migration_is_reported(dsn: str) -> None:
    _write(dsn, "agent_a", tag="a")
    _rename_to_legacy(dsn)
    _open(dsn, "agent_a")._backend.close()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO harness_memory.raw_events (namespace, id, host, timestamp, event_type, content) "
            "VALUES ('agent_a', 'late', 'manual', now(), 'user_message', 'written by 0.9.x')"
        )

    memory = _open(dsn, "agent_a")
    try:
        status = memory.legacy_schema_status()
        assert status["pending"] is False
        assert status["legacy_grew"] == {"raw_events": 1}
        assert memory._backend.count_stats()["raw_events"] == 3
    finally:
        memory._backend.close()


def _insert_node(conn: Any, schema: str, node_id: str, parent: str | None, atom: str | None) -> None:
    conn.execute(
        f"INSERT INTO {schema}.memory_nodes (namespace, id, parent_id, level, atom_id, content) "
        "VALUES ('agent_a', %s, %s, %s, %s, %s)",
        (node_id, parent, "leaf" if atom else "branch", atom, node_id),
    )


def test_dry_run_reports_nodes_whose_parent_would_be_skipped(dsn: str) -> None:
    _start_with_new_memory(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        # The old parent shares atom_id with a different current node, so it is skipped.
        _insert_node(conn, "harness_memory", "old-parent", None, "atom-1")
        _insert_node(conn, "harness_memory", "old-child", "old-parent", None)
        _insert_node(conn, "octop_memory", "new-node", None, "atom-1")

    memory = _open(dsn, "agent_a")
    try:
        plan = memory.merge_legacy_schema()
        assert plan["status"] == "blocked"
        assert plan["blocked"] == ["old-child"]
        done = memory.merge_legacy_schema(dry_run=False)
        assert done["status"] == "blocked"
        assert memory._backend.count_stats()["raw_events"] == 1  # nothing written
        assert memory.legacy_schema_status()["pending"] is True
    finally:
        memory._backend.close()


def test_custom_unique_index_makes_preview_an_estimate(dsn: str) -> None:
    _start_with_new_memory(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX raw_events_custom ON octop_memory.raw_events (namespace, content) "
            "WHERE host = 'manual'"
        )
    memory = _open(dsn, "agent_a")
    try:
        assert memory.merge_legacy_schema()["exact"] is False
    finally:
        memory._backend.close()


def test_a_held_namespace_lock_does_not_block_other_agents(dsn: str) -> None:
    _write(dsn, "agent_a", tag="a")
    _write(dsn, "agent_b", tag="b")
    _rename_to_legacy(dsn)
    with psycopg.connect(dsn, autocommit=True) as holder:
        # As if agent_a were mid-merge in another process.
        holder.execute("SELECT pg_advisory_lock(%s, hashtext('agent_a'))", (postgres_legacy._LOCK_CLASS,))
        memory = _open(dsn, "agent_b")
        try:
            assert memory._backend.count_stats()["raw_events"] == 3
        finally:
            memory._backend.close()


def _legacy_copy_of_current(dsn: str, ns: str) -> None:
    """Create a pre-1.0 schema holding three raw events for ``ns`` after the fact."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA harness_memory")
        conn.execute("CREATE TABLE harness_memory.raw_events (LIKE octop_memory.raw_events INCLUDING DEFAULTS)")
        for i in range(3):
            conn.execute(
                "INSERT INTO harness_memory.raw_events (namespace, id, host, timestamp, event_type, content) "
                "VALUES (%s, %s, 'manual', now(), 'user_message', 'old')",
                (ns, f"old-{i}"),
            )


def test_purge_is_recorded_even_when_the_legacy_schema_is_not_visible(dsn: str) -> None:
    memory = _open(dsn, "agent_a")
    try:
        memory.add_raw("new", event_type="user_message")
        memory._backend.purge_namespace()
    finally:
        memory._backend.close()
    # Access to the old schema appears only afterwards (created, or granted to this role).
    _legacy_copy_of_current(dsn, "agent_a")

    memory = _open(dsn, "agent_a")
    try:
        assert memory._backend.count_stats()["raw_events"] == 0
        assert memory.legacy_schema_status()["migrated"]["mode"] == "purged"
    finally:
        memory._backend.close()


def test_uncommitted_write_does_not_stall_other_startups(dsn: str) -> None:
    _open(dsn, "agent_a")._backend.close()
    with psycopg.connect(dsn) as holder:
        # An open transaction that already wrote to a shared table, like a merge in progress.
        holder.execute(
            "INSERT INTO octop_memory.memory_nodes (namespace, id, parent_id, level, content) "
            "VALUES ('agent_a', 'pending', NULL, 'branch', 'pending')"
        )
        memory = _open(dsn, "agent_b")
        try:
            assert memory._backend.count_stats()["raw_events"] == 0
        finally:
            memory._backend.close()
        holder.rollback()


def test_failed_auto_migration_on_autocommit_connection_writes_nothing(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(dsn, "agent_a", tag="a")
    _rename_to_legacy(dsn)
    real = postgres_legacy._copy_table
    calls: list[str] = []

    def copy_then_fail(cur: Any, table: str, ns: str) -> tuple[int, int]:
        calls.append(table)
        if len(calls) == 2:
            raise psycopg.errors.QueryCanceled("simulated failure")
        return real(cur, table, ns)

    monkeypatch.setattr(postgres_legacy, "_copy_table", copy_then_fail)
    memory = Memory(namespace="agent_a", backend="postgres", backend_config={"dsn": dsn, "autocommit": True})
    try:
        monkeypatch.undo()
        assert memory._backend.count_stats()["raw_events"] == 0
        assert _count(dsn, "octop_memory", "memory_nodes", "agent_a") == 0
        assert _meta(dsn, "agent_a:legacy_schema_migrated") is None
    finally:
        memory._backend.close()


def test_inexact_preview_does_not_block_and_the_merge_rolls_back(dsn: str) -> None:
    _start_with_new_memory(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        _insert_node(conn, "harness_memory", "old-parent", None, "atom-1")
        _insert_node(conn, "harness_memory", "old-child", "old-parent", None)
        _insert_node(conn, "octop_memory", "new-node", None, "atom-1")
        conn.execute(
            "CREATE UNIQUE INDEX raw_events_custom ON octop_memory.raw_events (namespace, content) "
            "WHERE host = 'nobody'"
        )
    memory = _open(dsn, "agent_a")
    try:
        plan = memory.merge_legacy_schema()
        assert plan["exact"] is False
        assert plan["status"] == "dry_run"
        assert plan["blocked"] == ["old-child"]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            memory.merge_legacy_schema(dry_run=False)
        assert memory._backend.count_stats()["raw_events"] == 1
        assert memory.legacy_schema_status()["pending"] is True
    finally:
        memory._backend.close()


def test_deferrable_unique_constraint_is_unsupported(dsn: str) -> None:
    _start_with_new_memory(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "ALTER TABLE octop_memory.raw_events ADD CONSTRAINT raw_events_deferred "
            "UNIQUE (namespace, id, host) DEFERRABLE"
        )
    memory = _open(dsn, "agent_a")
    try:
        assert memory.merge_legacy_schema()["status"] == "unsupported"
        done = memory.merge_legacy_schema(dry_run=False)
        assert done["status"] == "unsupported"
        assert done["deferrable"] == ["raw_events"]
        assert memory._backend.count_stats()["raw_events"] == 1
    finally:
        memory._backend.close()


def test_nested_purge_keeps_the_callers_lock_timeout(dsn: str) -> None:
    memory = _open(dsn, "agent_a")
    try:
        with memory._backend.transaction(), memory._backend._cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '50ms'")
            memory._backend.purge_namespace()
            cur.execute("SELECT current_setting('lock_timeout') AS v")
            assert cur.fetchone()["v"] == "50ms"
    finally:
        memory._backend.close()


def test_purge_marker_stays_visible_after_archiving_the_legacy_schema(dsn: str) -> None:
    _write(dsn, "agent_a", tag="a")
    _rename_to_legacy(dsn)
    memory = _open(dsn, "agent_a")
    try:
        memory._backend.purge_namespace()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("ALTER SCHEMA harness_memory RENAME TO harness_memory_archive")
        status = memory.legacy_schema_status()
        assert status["migrated"]["mode"] == "purged"
        assert status["migrated"]["previous"]["mode"] == "auto"
        assert memory.merge_legacy_schema(dry_run=False)["status"] == "purged"
    finally:
        memory._backend.close()


def test_marker_records_what_the_copy_saw(dsn: str) -> None:
    _write(dsn, "agent_a", tag="a")
    _rename_to_legacy(dsn)
    memory = _open(dsn, "agent_a")
    try:
        marker = memory.legacy_schema_status()["migrated"]
        assert marker["source_rows"]["raw_events"] == 3
        assert marker["skipped"] == 0
    finally:
        memory._backend.close()


def test_sqlite_has_no_legacy_schema(tmp_path: Any) -> None:
    memory = Memory(namespace="agent_a", backend="sqlite", backend_config={"db_path": str(tmp_path / "m.db")})
    try:
        assert memory.legacy_schema_status() == {"supported": False, "pending": False}
        with pytest.raises(NotImplementedError):
            memory.merge_legacy_schema()
    finally:
        with contextlib.suppress(Exception):
            memory._backend.close()
