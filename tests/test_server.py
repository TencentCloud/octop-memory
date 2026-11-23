"""Standalone service tests for token binding, authorization and space isolation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from starlette.routing import Mount

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from octop_memory.server import mcp as mcp_module
from octop_memory.server.admin import build_admin_app
from octop_memory.server.app import build_app
from octop_memory.server.config import resolve_space
from octop_memory.server.control import ALL_SPACES, ControlStore, SpaceConflictError
from octop_memory.server.registry import SpaceRegistry


@pytest.fixture()
def control(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    yield store
    store.close()


def _admin_headers(client, control, space_id):
    response = client.post("/api/admin/session/exchange", json={"code": control.create_admin_code(space_id)})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['session_token']}"}


def _mcp_result(client, token, name, **arguments):
    response = client.post(
        "/mcp/",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )
    assert response.status_code == 200
    data = next(line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: "))
    return json.loads(data)["result"]


def _mcp_call(client, token, name, **arguments):
    result = _mcp_result(client, token, name, **arguments)
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_server_exposes_one_authenticated_mcp_entrypoint(control, tmp_path):
    control.create_space("alpha", "Alpha", "sqlite", {"db_path": str(tmp_path / "alpha.db")})
    _, token = control.create_token("alpha", "client", ["read"])
    app = build_app({"control": {"sqlite": {"db_path": str(control.path)}}})
    assert [route.path for route in app.routes if isinstance(route, Mount)] == ["/mcp"]

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, follow_redirects=False) as client:
        assert client.post("/mcp/", headers=headers, json=body).status_code == 401
        headers["Authorization"] = f"Bearer {token}"
        response = client.post("/mcp/", headers=headers, json=body)
        assert response.status_code == 200
        data = next(line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: "))
        tools = json.loads(data)["result"]["tools"]
        search = next(tool for tool in tools if tool["name"] == "memory_search")
        assert search["inputSchema"]["properties"]["corpus"]["default"] == "atom"


def test_space_admin_cannot_manage_other_spaces(control, tmp_path):
    control.create_space("alpha", "Alpha", "sqlite", {"db_path": str(tmp_path / "a.db")})
    token, secret = control.create_token("alpha", "codex", ["read"])
    with TestClient(build_admin_app(control)) as client:
        scoped = _admin_headers(client, control, "alpha")
        global_admin = _admin_headers(client, control, ALL_SPACES)
        spaces_url = "/api/admin/spaces"
        token_url = f"/api/admin/tokens/{token.token_id}"
        beta = {"space_id": "beta", "backend_config": {"db_path": str(tmp_path / "b.db")}}

        assert client.post(spaces_url, headers=scoped, json=beta).status_code == 403
        assert control.get_space("beta") is None
        assert client.post(spaces_url, headers=scoped, json={"space_id": "alpha"}).status_code == 409
        assert client.post(spaces_url, headers=global_admin, json=beta).status_code == 200
        assert client.post(spaces_url, headers=scoped, json=beta).status_code == 403
        assert client.post(spaces_url, headers=global_admin, json={"space_id": ALL_SPACES}).status_code == 400
        missing = _admin_headers(client, control, "unregistered")
        assert client.post(spaces_url, headers=missing, json={"space_id": "unregistered"}).status_code == 403
        assert control.get_space("unregistered") is None
        assert [s["space_id"] for s in client.get(spaces_url, headers=scoped).json()["spaces"]] == ["alpha"]
        spaces = client.get(spaces_url, headers=global_admin).json()["spaces"]
        assert {s["space_id"] for s in spaces} == {"alpha", "beta"}

        changes = {"space_id": "beta", "client_name": "renamed"}
        assert client.patch(token_url, headers=scoped, json=changes).status_code == 403
        assert control.authenticate(secret) == token
        assert client.patch(token_url, headers=scoped, json={"client_name": "renamed"}).status_code == 200
        assert client.patch(token_url, headers=global_admin, json=changes).json()["space_id"] == "beta"
        assert control.authenticate(secret).space_id == "beta"


def test_api_and_cli_register_from_config(tmp_path, monkeypatch):
    from octop_memory.adapters.cli import main

    base = tmp_path / "memory.db"
    local = tmp_path / "existing.db"
    config = {
        "backend": "postgres",
        "postgres": {"dsn": "postgresql://cluster/team"},
        "sqlite": {"db_path": str(base)},
        "spaces": {"local": {"backend": "sqlite", "sqlite": {"db_path": str(local)}}},
        "control": {"sqlite": {"db_path": str(tmp_path / "control.db")}},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr("octop_memory._config_io.get_config_path", lambda: config_path)
    app = build_app(config)
    store = app.state.control
    runner = CliRunner()
    with TestClient(app) as client:
        headers = _admin_headers(client, store, ALL_SPACES)
        for space_id, backend in (("api", "postgres"), ("local", "sqlite")):
            result = client.post("/api/admin/spaces", headers=headers, json={"space_id": space_id})
            assert result.json() == {"space_id": space_id, "backend": backend}
        assert store.get_space("api").backend_config == config["postgres"]
        assert store.get_space("local").backend_config == {"db_path": str(local)}
        for name in ("cli", "local"):
            assert runner.invoke(main, ["space", "create", name]).exit_code == 0
        assert store.get_space("cli").backend_config == config["postgres"]
        assert store.get_space("local").created_by == "admin"
        bad = client.post(
            "/api/admin/spaces",
            headers=headers,
            json={
                "space_id": "typo",
                "backend": "pg",
                "backend_config": config["postgres"],
            },
        )
        assert bad.status_code == 400 and store.get_space("typo") is None
        assert runner.invoke(main, ["space", "create", "override", "--backend", "sqlite"]).exit_code == 0
        assert store.get_space("override").backend_config == {"db_path": str(tmp_path / "spaces/override.db")}
        conflict = runner.invoke(main, ["space", "create", "cli", "--dsn", "postgresql://cluster/other"])
        assert conflict.exit_code == 1 and "different storage" in conflict.output
        assert store.get_space("cli").backend_config == config["postgres"]


@pytest.mark.parametrize("scope", [ALL_SPACES, "team"])
def test_repeat_registration_checks_actual_sqlite_file(control, tmp_path, scope):
    db = tmp_path / "team.db"
    db.touch()
    alias = tmp_path / "alias.db"
    alias.hardlink_to(db)
    original = control.create_space("team", "Original", "sqlite", {"db_path": str(db)})
    with TestClient(build_admin_app(control)) as client:
        headers = _admin_headers(client, control, scope)
        for requested, status in ((alias, 200), (tmp_path / "other.db", 409)):
            response = client.post(
                "/api/admin/spaces",
                headers=headers,
                json={"space_id": "team", "display_name": "Changed", "backend_config": {"db_path": str(requested)}},
            )
            assert response.status_code == status
        assert (
            client.post(
                "/api/admin/spaces", headers=headers, json={"space_id": "team", "backend": "postgres"}
            ).status_code
            == 409
        )
    assert control.get_space("team") == original
    assert not (tmp_path / "other.db").exists()


def test_postgres_reregistration_compares_connection_settings(control):
    pytest.importorskip("psycopg")
    original = control.create_space("pg", "Original", "postgres", {"dsn": "postgresql://reader@cluster/team"})
    with TestClient(build_admin_app(control)) as client:
        headers = _admin_headers(client, control, "pg")
        for dsn, status in (
            ("dbname=team host=cluster user=reader", 200),
            ("dbname=team host=cluster user=reader application_name=cli", 200),
            ("postgresql://reader@cluster/other", 409),
            ("postgresql://reader@other/team", 409),
            ("dbname=team host=cluster user=another_role", 409),
        ):
            response = client.post(
                "/api/admin/spaces",
                headers=headers,
                json={
                    "space_id": "pg",
                    "backend": "postgres",
                    "backend_config": {"dsn": dsn},
                },
            )
            assert response.status_code == status
    assert control.get_space("pg") == original


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_revoke_one_client_in_a_shared_space(control, tmp_path, backend, request):
    space = f"clients-{uuid4().hex}"
    settings = (
        {"dsn": request.getfixturevalue("postgres_dsn")}
        if backend == "postgres"
        else {"db_path": str(tmp_path / "shared.db")}
    )
    control.create_space(space, space, backend, settings)
    app = build_app({"control": {"sqlite": {"db_path": str(control.path)}}})
    with TestClient(app) as client:
        admin = _admin_headers(client, control, space)
        base = f"/api/admin/spaces/{space}"
        tokens = {}
        for name in ("codex", "cursor"):
            created = client.post(f"{base}/tokens", headers=admin, json={"client_name": name})
            assert created.status_code == 200
            tokens[name] = created.json()
        codex, cursor = tokens["codex"], tokens["cursor"]
        assert codex["token_id"] != cursor["token_id"]
        cursor_url = f"/api/admin/tokens/{cursor['token_id']}"
        assert client.patch(cursor_url, headers=admin, json={"disabled_clients": ["cursor"]}).status_code == 422

        client.headers["X-Octop-Client"] = "untrusted-label"
        _mcp_call(
            client,
            cursor["token"],
            "memory_capture",
            content="Client attribution test",
            session_id="audit",
            extract=False,
        )
        raw = app.state.registry.call(space, lambda memory: memory.list_raw(session_id="audit")[0])
        assert raw.payload["caller"] == cursor["token_id"]
        assert raw.payload["source_host"] == "cursor"
        _mcp_call(client, cursor["token"], "memory_save", source="test", content="The team builds cobalt freighters.")
        assert _mcp_call(client, codex["token"], "memory_search", query="cobalt")["total"] == 1
        assert client.delete(cursor_url, headers=admin).json() == {"revoked": True}
        assert client.post(f"{cursor_url}/rotate", headers=admin).status_code == 409
        headers = {"Authorization": f"Bearer {cursor['token']}", "Accept": "application/json, text/event-stream"}
        denied = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert denied.status_code == 401
        _mcp_call(client, codex["token"], "memory_save", source="test", content="The team builds violet tankers.")
        assert _mcp_call(client, codex["token"], "memory_search", query="violet")["total"] == 1
        entries = {entry["client"]: entry for entry in client.get(f"{base}/clients", headers=admin).json()["clients"]}
        assert set(entries) == {"codex", "cursor"}
        assert entries["cursor"]["token_id"] == cursor["token_id"]
        assert entries["cursor"]["count"] == 2 and entries["cursor"]["revoked_at"]
        assert entries["codex"]["count"] == 3 and entries["codex"]["revoked_at"] is None


def test_upgrade_preserves_usage_and_retires_known_shared_tokens(tmp_path):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "CREATE TABLE memory_space_tokens (token_id TEXT PRIMARY KEY, space_id TEXT, client_name TEXT, "
            "scopes TEXT, token_hash TEXT UNIQUE, token_hint TEXT, created_at TEXT, last_used_at TEXT, "
            "revoked_at TEXT, client_stats TEXT NOT NULL DEFAULT '{}', disabled_clients TEXT NOT NULL DEFAULT '[]')"
        )
        for name, clients, disabled in (
            ("single", ["codex"], []),
            ("shared", ["codex", "cursor"], []),
            ("partial", [], ["cursor"]),
        ):
            stats = {label: {"count": 4, "first_seen": "2026-01-01"} for label in clients}
            conn.execute(
                "INSERT INTO memory_space_tokens VALUES (?, 'a', ?, '[]', ?, 'test', '', NULL, NULL, ?, ?)",
                (name, name, hashlib.sha256(name.encode()).hexdigest(), json.dumps(stats), json.dumps(disabled)),
            )
    conn.close()
    for _ in range(2):
        store = ControlStore(path)
        try:
            assert store.authenticate("single").request_count == 4
            assert store.authenticate("single").first_used_at == "2026-01-01"
            assert store.authenticate("partial") is None
            assert store.authenticate("shared") is None
            assert store.get_token("shared").request_count == 8
        finally:
            store.close()


def test_older_control_store_uses_only_the_tokens_current_binding(control, tmp_path):
    for name in ("alpha", "beta"):
        control.create_space(name, name, "sqlite", {"db_path": str(tmp_path / f"{name}.db")})
    token, secret = control.create_token("alpha", "terminal", ["read"])
    _, writer = control.create_token("beta", "writer", ["write"])

    tables = {row[0] for row in control._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables.isdisjoint({"memory_space_grants", "memory_token_map"})
    # Older releases could also store a grant to beta for this same token.
    with control.write_transaction():
        control._conn.execute(
            "CREATE TABLE memory_space_grants (grant_id TEXT PRIMARY KEY, token_id TEXT, "
            "space_id TEXT, scopes TEXT, created_at TEXT, UNIQUE(token_id, space_id))"
        )
        control._conn.execute(
            "INSERT INTO memory_space_grants VALUES ('legacy', ?, 'beta', ?, '')",
            (token.token_id, json.dumps(["read", "write"])),
        )

    app = build_app({"control": {"sqlite": {"db_path": str(control.path)}}})
    assert "/api/admin/spaces/{space_id}/tokens/import" not in app.openapi()["paths"]
    with TestClient(app) as client:
        _mcp_call(client, writer, "memory_save", source="test", content="Beta uses cobalt freighters.")
        assert _mcp_call(client, secret, "memory_search", query="cobalt")["total"] == 0

        headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json, text/event-stream"}
        tools_list = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert client.post("/mcp/", headers={**headers, "X-Octop-Space": "alpha"}, json=tools_list).status_code == 200
        assert client.post("/mcp/", headers={**headers, "X-Octop-Space": "beta"}, json=tools_list).status_code == 403
        assert client.post("/mcp/", headers={**headers, "X-Octop-Agent-Id": "beta"}, json=tools_list).status_code == 400

        token_url = f"/api/admin/tokens/{token.token_id}"
        admin = _admin_headers(client, control, ALL_SPACES)
        assert client.patch(token_url, headers=admin, json={"space_id": "beta"}).status_code == 200
        assert _mcp_call(client, secret, "memory_search", query="cobalt")["total"] == 1
        denied = _mcp_result(client, secret, "memory_save", source="test", content="Test note")
        assert denied["isError"] and "missing required scope 'write'" in denied["content"][0]["text"]
        assert client.post("/mcp/", headers={**headers, "X-Octop-Space": "alpha"}, json=tools_list).status_code == 403
        old_admin = _admin_headers(client, control, "alpha")
        assert client.post(f"{token_url}/rotate", headers=old_admin).status_code == 403
        assert client.delete(token_url, headers=old_admin).status_code == 403


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_registered_spaces_keep_separate_memory(backend, control, tmp_path, request, monkeypatch):
    suffix = uuid4().hex[:10]
    alpha = f"test-{suffix}-alpha"
    beta = f"test-{suffix}-beta"
    config_path = tmp_path / "config.json"
    dsn = request.getfixturevalue("postgres_dsn") if backend == "postgres" else None
    settings = {"dsn": dsn} if dsn else {"db_path": str(tmp_path / "shared.db")}
    config = {
        "control": {"sqlite": {"db_path": str(control.path)}},
        "backend": backend,
        "spaces": {space_id: {backend: settings} for space_id in (alpha, beta)},
    }
    with TestClient(build_app(config)) as client:
        headers = _admin_headers(client, control, ALL_SPACES)
        url = "/api/admin/spaces"
        for space_id in (alpha, beta):
            assert client.post(url, headers=headers, json={"space_id": space_id}).status_code == 200
            body = {"space_id": space_id, "backend": backend, "backend_config": settings}
            assert client.post(url, headers=headers, json=body).status_code == 200

        _, writer = control.create_token(alpha, "writer", ["read", "write"])
        _, other_reader = control.create_token(beta, "reader", ["read"])
        _mcp_call(client, writer, "memory_save", source="test", content="The launch project uses cobalt freighters.")
        alternate_id = alpha.upper().replace("-", "_")
        conflict = {"space_id": alternate_id, "backend": backend, "backend_config": settings}
        assert client.post(url, headers=headers, json=conflict).status_code == 409
        assert control.get_space(alternate_id) is None

        if dsn:
            from octop_memory.adapters.cli import main

            config_path.write_text(json.dumps(config))
            monkeypatch.setattr("octop_memory._config_io.get_config_path", lambda: config_path)
            result = CliRunner().invoke(main, ["space", "create", alternate_id, "--backend", "postgres"])
            assert result.exit_code == 1
            assert "PostgreSQL namespace" in result.output
        else:
            conflict["backend_config"] = {"db_path": str(tmp_path / "separate.db")}
            assert client.post(url, headers=headers, json=conflict).status_code == 200
            _, separate_reader = control.create_token(alternate_id, "reader", ["read"])
            assert _mcp_call(client, separate_reader, "memory_search", query="cobalt")["total"] == 0
        assert _mcp_call(client, writer, "memory_search", query="cobalt")["total"] == 1
        assert _mcp_call(client, other_reader, "memory_search", query="cobalt")["total"] == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_mcp_search_corpora_and_paginated_get(backend, control, tmp_path, request):
    space_id = f"mcp-search-{uuid4().hex[:10]}"
    dsn = request.getfixturevalue("postgres_dsn") if backend == "postgres" else None
    settings = {"dsn": dsn} if dsn else {"db_path": str(tmp_path / "search.db")}
    control.create_space(space_id, "Search", backend, settings)
    _, writer = control.create_token(space_id, "writer", ["write"])
    _, reader = control.create_token(space_id, "reader", ["read"])
    app = build_app({"control": {"sqlite": {"db_path": str(control.path)}}})

    with TestClient(app) as client:
        _mcp_call(client, writer, "memory_save", source="test", content="The cobalt project uses stable ticket keys.")
        raw_text = "The cobalt meeting used a slate label for requests."
        recorded = _mcp_call(client, writer, "memory_capture", source="test", content=raw_text, extract=False)
        assert recorded["accepted"] == 1

        default = _mcp_call(client, reader, "memory_search", query="cobalt")
        assert default == _mcp_call(client, reader, "memory_search", query="cobalt", corpus="atom")
        assert [hit["layer"] for hit in default["hits"]] == ["atom"]
        assert _mcp_call(client, reader, "memory_search", query="slate")["hits"] == []
        broad = _mcp_call(client, reader, "memory_search", query="slate", corpus="all")
        assert [hit["layer"] for hit in broad["hits"]] == ["raw"]

        raw = _mcp_call(client, reader, "memory_search", query="cobalt", corpus="raw", max_results=1)
        assert [hit["layer"] for hit in raw["hits"]] == ["raw"]
        captured = _mcp_call(client, reader, "memory_search", query="slate", corpus="raw")
        assert captured["hits"][0]["snippet"] == raw_text
        raw_doc = _mcp_call(client, reader, "memory_get", path=captured["hits"][0]["path"])
        assert raw_text in raw_doc["content"]

        path = default["hits"][0]["path"]
        doc = _mcp_call(client, reader, "memory_get", path=path)
        page = _mcp_call(client, reader, "memory_get", path=path, start=2, lines=1)
        assert page["from_line"] == page["to_line"] == 2
        assert page["content"] == page["excerpt"] == doc["content"].splitlines()[1]
        assert page == _mcp_call(client, reader, "memory_get", path=path, from_line=2, lines=1)


@pytest.mark.parametrize("alias", ["relative", "symlink", "hardlink"])
def test_sqlite_conflict_compares_actual_file(control, tmp_path, monkeypatch, alias):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "original.db"
    db.touch()
    other = Path("original.db")
    if alias == "symlink":
        other = tmp_path / "link.db"
        other.symlink_to(db)
    elif alias == "hardlink":
        other = tmp_path / "other.db"
        other.hardlink_to(db)
    control.create_space("team.blue", "Blue", "sqlite", {"db_path": str(db)})
    with pytest.raises(SpaceConflictError, match="SQLite table prefix"):
        control.create_space("team_blue", "Another team", "sqlite", {"db_path": str(other)})
    assert db.stat().st_size == 0


def test_sqlite_case_only_names_follow_actual_filesystem(control, tmp_path):
    probe = tmp_path / "probe"
    probe.touch()
    case_insensitive = (tmp_path / "PROBE").exists()
    control.create_space("TeamA", "First", "sqlite", {"db_path": str(tmp_path / "TeamA.db")})
    settings = {"db_path": str(tmp_path / "teama.db")}
    if case_insensitive:
        with pytest.raises(SpaceConflictError, match="SQLite table prefix"):
            control.create_space("teama", "Second", "sqlite", settings)
    else:
        assert control.create_space("teama", "Second", "sqlite", settings).space_id == "teama"


def test_concurrent_sqlite_registrations_reserve_one_prefix(control, tmp_path):
    start = Barrier(2)
    settings = {"db_path": str(tmp_path / "shared.db")}

    def register(name):
        store = ControlStore(control.path, {})
        try:
            start.wait(timeout=5)
            store.create_space(name, name, "sqlite", settings)
            return True
        except SpaceConflictError:
            return False
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(register, ("team-blue", "team_blue"))) == [False, True]
    assert len(control.list_spaces()) == 1


@pytest.mark.parametrize("same_file", [True, False])
def test_concurrent_registrations_keep_first_binding(control, tmp_path, same_file):
    start = Barrier(2)

    def register(path):
        store = ControlStore(control.path, {})
        try:
            start.wait(timeout=5)
            return store.create_space("same-id", backend_config={"db_path": str(path)})
        except SpaceConflictError:
            return None
        finally:
            store.close()

    first = tmp_path / "first.db"
    second = first if same_file else tmp_path / "second.db"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, (first, second)))
    assert results.count(None) == (0 if same_file else 1)
    assert all(result == control.get_space("same-id") for result in results if result is not None)


def test_sqlite_default_and_private_databases(control, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    control.create_space("team-blue", "First", "sqlite")
    # Older registrations with no explicit path use the storage backend's default file.
    with control.write_transaction():
        control._conn.execute("UPDATE memory_spaces SET backend_config='{}' WHERE space_id='team-blue'")
    default_db = tmp_path / ".octop-memory/session.sqlite"
    with pytest.raises(SpaceConflictError, match="SQLite table prefix"):
        control.create_space("TEAM_BLUE", "Second", "sqlite", {"db_path": str(default_db)})
    for name in ("TEAM_BLUE", "team.blue"):
        control.create_space(name, name, "sqlite", {"db_path": ":memory:"})


def test_configured_sqlite_spaces_use_effective_paths(tmp_path):
    shared = str(tmp_path / "shared.db")
    cfg = {"spaces": {"team-blue": {}, "team_blue": {}}, "sqlite": {"db_path": shared}}
    control = ControlStore(tmp_path / "control.db", cfg)
    registry = SpaceRegistry(control)
    try:
        for name in cfg["spaces"]:
            with pytest.raises(SpaceConflictError, match="SQLite table prefix"):
                registry.call(name, lambda memory: memory)
        with pytest.raises(SpaceConflictError):
            control.create_space("team.blue", "Third", "sqlite", {"db_path": shared})
        assert not Path(shared).exists()
        # A registration overrides the same ID's configuration file entry.
        control.create_space("team-blue", "First", "sqlite", {"db_path": str(tmp_path / "separate.db")})
        for name in cfg["spaces"]:
            assert registry.call(name, lambda memory: memory.list_raw()) == []
    finally:
        registry.close()
        control.close()


def test_registry_rejects_preexisting_sqlite_namespace_conflicts(control, tmp_path):
    db = tmp_path / "legacy.db"
    settings = {"db_path": str(db)}
    control.create_space("project-alpha", "Alpha", "sqlite", settings)
    with control.write_transaction():
        control._conn.execute(
            "INSERT INTO memory_spaces VALUES (?, ?, 'sqlite', ?, 'legacy', '', NULL)",
            ("PROJECT_ALPHA", "Legacy", json.dumps(settings)),
        )
    registry = SpaceRegistry(control)
    for name in ("project-alpha", "PROJECT_ALPHA"):
        with pytest.raises(SpaceConflictError, match="SQLite table prefix"):
            registry.call(name, lambda memory: memory)
    assert not db.exists()


def test_registry_rejects_preexisting_postgres_namespace_conflicts(control):
    control.create_space("project-alpha", "Alpha", "postgres", {})
    # Simulate an entry written by a previous version of the control store.
    with control._lock, control._conn:
        control._conn.execute(
            "INSERT INTO memory_spaces VALUES (?, ?, 'postgres', '{}', 'legacy', '', NULL)",
            ("PROJECT_ALPHA", "Legacy"),
        )
    registry = SpaceRegistry(control, {"server": {}})
    for space_id in ("project-alpha", "PROJECT_ALPHA"):
        with pytest.raises(SpaceConflictError, match="PostgreSQL namespace"):
            registry.call(space_id, lambda memory: memory)


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
    mcp = mcp_module.build_mcp(registry)
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
    mcp = mcp_module.build_mcp(registry)
    capture = mcp._tool_manager._tools["memory_capture"].fn

    space_token = mcp_module._current_space.set("alpha")
    scope_token = mcp_module._current_scopes.set(("read", "write"))
    client_token = mcp_module._current_token.set("test-codex-token-id")
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
        mcp_module._current_token.reset(client_token)
        mcp_module._current_scopes.reset(scope_token)
        mcp_module._current_space.reset(space_token)
        store.close()


def test_review_tools_require_review_scope(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.create_space("alpha", "Alpha", "sqlite", {"db_path": str(tmp_path / "a.db")})
    registry = SpaceRegistry(store, {})
    mcp = mcp_module.build_mcp(registry)
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


def test_cleanup_legacy_group_schema_is_optional(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.create_space("alpha", "Alpha", "sqlite", {"db_path": str(tmp_path / "a.db")})
    token, plaintext = store.create_token("alpha", "codex", ["read"])
    with store._lock, store._conn:
        store._conn.execute("CREATE TABLE memory_access_groups (group_id TEXT PRIMARY KEY, space_id TEXT)")
        store._conn.execute("ALTER TABLE memory_space_tokens ADD COLUMN group_id TEXT")
    assert store.legacy_group_schema() == {"table": True, "group_id_column": True}

    assert store.cleanup_legacy_groups(dry_run=True)["applied"] is False
    assert store.legacy_group_schema() == {"table": True, "group_id_column": True}

    assert store.cleanup_legacy_groups()["applied"] is True
    assert store.legacy_group_schema() == {"table": False, "group_id_column": False}
    assert store.authenticate(plaintext).token_id == token.token_id
    store.close()
