"""Prototype tests: control store, token auth, scope checks and migration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from octop_memory.server import mcp as mcp_module
from octop_memory.server.config import resolve_space
from octop_memory.server.control import ControlStore
from octop_memory.server.registry import SpaceRegistry


@pytest.fixture()
def control(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    yield store
    store.close()


def test_space_and_token_lifecycle(control, tmp_path):
    control.create_space("alpha", "Alpha", "sqlite", {"db_path": str(tmp_path / "a.db")})
    token, plaintext = control.create_token("alpha", "codex", ["read", "write"])

    assert control.authenticate(plaintext).token_id == token.token_id
    assert control.authenticate("omcp_wrong") is None

    rotated, fresh = control.rotate(token.token_id)
    assert control.authenticate(plaintext) is None
    assert control.authenticate(fresh).token_id == rotated.token_id

    assert control.revoke(token.token_id) is True
    assert control.authenticate(fresh) is None


def test_grants_allow_secondary_space_only_when_requested(control, tmp_path):
    control.create_space("a", "A", "sqlite", {"db_path": str(tmp_path / "a.db")})
    control.create_space("b", "B", "sqlite", {"db_path": str(tmp_path / "b.db")})
    token, _ = control.create_token("a", "codex", ["read"])
    control.add_grant(token.token_id, "b", ["read"])

    allowed = control.allowed_spaces(control.get_token(token.token_id))
    assert {space for space, _ in allowed} == {"a", "b"}


def test_admin_code_exchange_is_one_time(control):
    control.create_space("alpha", "Alpha", "sqlite", {"db_path": ":memory:"})
    code = control.create_admin_code("alpha")
    session, record = control.redeem_admin_code(code)
    assert record.space_id == "alpha"
    assert control.verify_session(session).space_id == "alpha"
    with pytest.raises(ValueError):
        control.redeem_admin_code(code)


def test_space_config_picks_sqlite_or_postgres(tmp_path):
    cfg = {
        "backend": "sqlite",
        "sqlite": {"db_path": str(tmp_path / "memory.db")},
        "postgres": {"dsn": "postgresql://localhost/octop_memory"},
        "spaces": {"pg": {"backend": "postgres", "postgres": {"dsn": "postgresql://h/pg"}}},
    }
    sqlite_space = resolve_space(cfg, "alpha")
    assert sqlite_space.backend == "sqlite"
    assert sqlite_space.backend_config["db_path"].endswith("spaces/alpha.db")
    pg_space = resolve_space(cfg, "pg")
    assert pg_space.backend == "postgres"
    assert pg_space.backend_config["dsn"] == "postgresql://h/pg"


def test_registry_opens_postgres_with_space_namespace(monkeypatch):
    captured = {}

    class FakeMemory:
        def __init__(self, namespace, backend, backend_config):
            captured.update(namespace=namespace, backend=backend, config=backend_config)

    monkeypatch.setattr("octop_memory.server.registry.Memory", FakeMemory)
    store = ControlStore(":memory:")
    store.create_space("team", "Team", "postgres", {"dsn": "postgresql://h/db"})
    registry = SpaceRegistry(store, {})
    registry.call("team", lambda memory: memory)
    assert captured == {
        "namespace": "team",
        "backend": "postgres",
        "config": {"dsn": "postgresql://h/db"},
    }
    store.close()


def test_tool_scope_is_enforced(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.create_space("alpha", "Alpha", "sqlite", {"db_path": str(tmp_path / "a.db")})
    registry = SpaceRegistry(store, {})
    mcp = mcp_module.build_mcp(registry, store)
    tool = mcp._tool_manager._tools["memory_capture"].fn

    space_token = mcp_module._current_space.set("alpha")
    scope_token = mcp_module._current_scopes.set(("read",))
    try:
        import asyncio

        with pytest.raises(PermissionError):
            asyncio.run(tool(content="只读 token 不应写入", source="codex"))
    finally:
        mcp_module._current_scopes.reset(scope_token)
        mcp_module._current_space.reset(space_token)
        store.close()


def test_capture_retry_is_idempotent(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.create_space("alpha", "Alpha", "sqlite", {"db_path": str(tmp_path / "a.db")})
    registry = SpaceRegistry(store, {})
    mcp = mcp_module.build_mcp(registry, store)
    capture = mcp._tool_manager._tools["memory_capture"].fn

    space_token = mcp_module._current_space.set("alpha")
    scope_token = mcp_module._current_scopes.set(("read", "write"))
    client_token = mcp_module._current_client.set("codex")
    events = [{"event_type": "user_message", "content": "幂等测试", "payload": {"src_key": "f1:1"}}]
    try:
        import asyncio

        first = asyncio.run(capture(events=events, source="codex", session_id="s1"))
        second = asyncio.run(capture(events=events, source="codex", session_id="s1"))
        assert first["accepted"] == 1
        assert second["accepted"] == 0
        assert second["skipped_duplicates"] == 1
        rows = registry.call("alpha", lambda memory: memory.list_raw(session_id="s1", limit=10))
        assert len(rows) == 1
    finally:
        mcp_module._current_client.reset(client_token)
        mcp_module._current_scopes.reset(scope_token)
        mcp_module._current_space.reset(space_token)
        store.close()


def test_review_tools_require_review_scope(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.create_space("alpha", "Alpha", "sqlite", {"db_path": str(tmp_path / "a.db")})
    registry = SpaceRegistry(store, {})
    mcp = mcp_module.build_mcp(registry, store)
    reject = mcp._tool_manager._tools["memory_reject"].fn

    space_token = mcp_module._current_space.set("alpha")
    scope_token = mcp_module._current_scopes.set(("read", "write"))
    try:
        import asyncio

        with pytest.raises(PermissionError):
            asyncio.run(reject(candidate_ids=["c1"]))
    finally:
        mcp_module._current_scopes.reset(scope_token)
        mcp_module._current_space.reset(space_token)
        store.close()


def test_access_group_disables_one_terminal_only(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.create_space("alpha", "Alpha", "sqlite", {"db_path": str(tmp_path / "a.db")})
    store.create_space("beta", "Beta", "sqlite", {"db_path": str(tmp_path / "b.db")})
    group_id, created = store.create_access_group(
        "alpha", "my terminals", ["read", "write"], ["codex", "cursor"], extra_space_ids=["beta"]
    )
    codex = next(token for token, _ in created if token.client_name == "codex")
    cursor = next(token for token, _ in created if token.client_name == "cursor")
    plaintexts = {token.client_name: plain for token, plain in created}

    assert store.authenticate(plaintexts["codex"]).token_id == codex.token_id
    assert {space for space, _ in store.allowed_spaces(codex)} == {"alpha", "beta"}

    assert store.revoke(cursor.token_id) is True
    assert store.authenticate(plaintexts["cursor"]) is None
    assert store.authenticate(plaintexts["codex"]).token_id == codex.token_id

    rotated, fresh = store.rotate(codex.token_id)
    assert store.authenticate(plaintexts["codex"]) is None
    assert store.authenticate(fresh).token_id == rotated.token_id
    assert rotated.group_id == group_id

    groups = store.list_access_groups("alpha")
    assert len(groups) == 1
    assert {token.client_name for token in groups[0]["tokens"]} == {"codex", "cursor"}
    store.close()


def test_legacy_migration_preserves_plaintext(tmp_path):
    octop_db = tmp_path / "octop.db"
    plain = "omcp_legacy_plain"
    conn = sqlite3.connect(octop_db)
    conn.executescript(
        """
        CREATE TABLE agents (agent_id TEXT, user_id INTEGER, name TEXT);
        CREATE TABLE memory_mcp_tokens (
          token_id TEXT, user_id INTEGER, name TEXT, expert_ids TEXT,
          token_hash TEXT, token_hint TEXT, created_at TEXT,
          last_used_at TEXT, revoked_at TEXT);
        """
    )
    conn.execute("INSERT INTO agents VALUES ('A1',1,'Expert A')")
    conn.execute(
        "INSERT INTO memory_mcp_tokens VALUES ('old',1,'legacy','[\"A1\"]',?,?,?,NULL,NULL)",
        (hashlib.sha256(plain.encode()).hexdigest(), plain[-4:], "2026-11-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    from tools.migrate_octop_tokens import main as migrate

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"control": {"sqlite": {"db_path": str(tmp_path / "control.db")}}}))
    assert migrate(["--octop-db", str(octop_db), "--config", str(config)]) == 0

    store = ControlStore(tmp_path / "control.db")
    token = store.authenticate(plain)
    assert token is not None and token.space_id == "octop-expert-A1"
    store.close()
