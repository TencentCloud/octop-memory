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
        assert memory.merge_legacy_schema(dry_run=False)["status"] == "already_migrated"
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

    def copy_then_fail(cur: Any, table: str, ns: str) -> int:
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
        }
        assert memory.merge_legacy_schema(dry_run=False)["status"] == "nothing_to_merge"
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
