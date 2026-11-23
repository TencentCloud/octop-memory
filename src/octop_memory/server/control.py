"""Control-plane store for spaces, single-space tokens and short-lived admin sessions."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from octop_memory._config_io import load_config
from octop_memory.server.config import SpaceConfig, resolve_space
from octop_memory.storage.backends.namespace import normalize_namespace, sqlite_database_path

ALL_SPACES = "*"


class SpaceConflictError(ValueError):
    """A space ID or its underlying storage namespace is already in use."""


def _same_sqlite_database(path: Path, other: Path, *, probe_missing: bool = False) -> bool:
    if ":memory:" in (str(path), str(other)):
        return False  # Each connection owns a private database.
    path, other = path.resolve(), other.resolve()
    if str(path) == str(other):
        return True
    if probe_missing and not path.exists() and not other.exists():
        # Let the filesystem identify case/Unicode aliases even before first use.
        # Materialize only the proposed database, without initializing tables.
        path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(path).close()
    try:
        return path.samefile(other)
    except FileNotFoundError:
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return "omcp_" + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Space:
    space_id: str
    display_name: str
    backend: str
    backend_config: dict[str, Any]
    created_by: str
    created_at: str
    disabled_at: str | None

    def matches_storage(self, space: SpaceConfig) -> bool:
        if self.backend != space.backend:
            return False
        if self.backend == "sqlite":
            stored = sqlite_database_path(self.backend_config)
            requested = sqlite_database_path(space.backend_config)
            return stored == requested or _same_sqlite_database(stored, requested)

        if self.backend_config == space.backend_config:
            return True
        try:
            from psycopg import ProgrammingError
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            return False

        def connection_target(config: dict[str, Any]) -> dict[str, Any]:
            kwargs = dict(config)
            dsn = kwargs.pop("dsn", "postgresql://localhost/octop_memory")
            params = conninfo_to_dict(dsn, **kwargs)
            # Authentication secrets, client labels and timeouts do not select a database.
            for key in ("password", "passfile", "application_name", "fallback_application_name", "connect_timeout"):
                params.pop(key, None)
            return params

        try:
            return connection_target(self.backend_config) == connection_target(space.backend_config)
        except ProgrammingError:
            return False


@dataclass(frozen=True)
class Token:
    token_id: str
    space_id: str
    client_name: str
    scopes: list[str]
    token_hint: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    first_used_at: str | None
    request_count: int


@dataclass(frozen=True)
class Session:
    session_id: str
    space_id: str
    scopes: list[str]
    expires_at: str


class ControlStore:
    """SQLite control store. One file, WAL, thread-safe via a single lock."""

    def __init__(self, db_path: str | Path, config: dict[str, Any] | None = None) -> None:
        self.path = Path(db_path).expanduser()
        self.config = load_config() if config is None else config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def write_transaction(self) -> Iterator[None]:
        """Keep permission checks and writes atomic across CLI and API connections."""
        with self._lock:
            if self._conn.in_transaction:
                yield
                return
            with self._conn:
                self._conn.execute("BEGIN IMMEDIATE")
                yield

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_spaces (
                  space_id TEXT PRIMARY KEY,
                  display_name TEXT NOT NULL,
                  backend TEXT NOT NULL,
                  backend_config TEXT NOT NULL,
                  created_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  disabled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS memory_space_tokens (
                  token_id TEXT PRIMARY KEY,
                  space_id TEXT NOT NULL,
                  client_name TEXT NOT NULL,
                  scopes TEXT NOT NULL,
                  token_hash TEXT NOT NULL UNIQUE,
                  token_hint TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  last_used_at TEXT,
                  revoked_at TEXT,
                  first_used_at TEXT,
                  request_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_tokens_space ON memory_space_tokens(space_id);
                CREATE TABLE IF NOT EXISTS memory_admin_codes (
                  code_hash TEXT PRIMARY KEY,
                  space_id TEXT NOT NULL,
                  scopes TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS memory_admin_sessions (
                  session_hash TEXT PRIMARY KEY,
                  space_id TEXT NOT NULL,
                  scopes TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );
                """
            )

        # Upgrade the token rows once, under the same lock used by other writers.
        with self.write_transaction():
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(memory_space_tokens)")}
            if "request_count" not in columns:
                self._conn.execute("ALTER TABLE memory_space_tokens ADD COLUMN first_used_at TEXT")
                self._conn.execute(
                    "ALTER TABLE memory_space_tokens ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0"
                )
                for row in self._conn.execute("SELECT * FROM memory_space_tokens").fetchall():
                    stats = json.loads(row["client_stats"] or "{}") if "client_stats" in columns else {}
                    disabled = "disabled_clients" in columns and json.loads(row["disabled_clients"] or "[]")
                    first_seen = [v["first_seen"] for v in stats.values() if v.get("first_seen")]
                    self._conn.execute(
                        "UPDATE memory_space_tokens SET first_used_at=?, request_count=?, "
                        "revoked_at=COALESCE(revoked_at, ?) WHERE token_id=?",
                        (
                            min(first_seen, default=row["last_used_at"]),
                            sum(int(v.get("count") or 0) for v in stats.values()),
                            _now() if disabled or len(stats) > 1 else None,
                            row["token_id"],
                        ),
                    )

    # ── spaces ────────────────────────────────────────────────────────────
    def create_space(
        self,
        space_id: str,
        display_name: str = "",
        backend: str | None = None,
        backend_config: dict[str, Any] | None = None,
        *,
        created_by: str = "local",
    ) -> Space:
        if not space_id.strip() or space_id == ALL_SPACES:
            raise ValueError("space ID must be nonempty and cannot be '*'")
        with self.write_transaction():
            existing = self.get_space(space_id)
            space = resolve_space(self.config, space_id, display_name, backend=backend, backend_config=backend_config)
            if existing is not None:
                if existing.matches_storage(space):
                    return existing
                raise SpaceConflictError(f"space {space_id!r} is already bound to different storage")
            self.check_storage_namespace(space_id, space.backend, space.backend_config)
            self._conn.execute(
                "INSERT INTO memory_spaces VALUES (?,?,?,?,?,?,NULL)",
                (
                    space_id,
                    space.display_name,
                    space.backend,
                    json.dumps(space.backend_config, ensure_ascii=False),
                    created_by,
                    _now(),
                ),
            )
            return self.get_space(space_id)  # type: ignore[return-value]

    def check_storage_namespace(
        self, space_id: str, backend: str, backend_config: dict[str, Any], *, config: dict[str, Any] | None = None
    ) -> None:
        """Check registered spaces and effective file-config fallbacks before first use."""
        namespace = normalize_namespace(space_id)
        path = sqlite_database_path(backend_config) if backend == "sqlite" else None
        cfg = self.config if config is None else config
        registered = {s.space_id: s for s in self.list_spaces()}
        for other_id in registered.keys() | (cfg.get("spaces") or {}).keys():
            if other_id == space_id or normalize_namespace(other_id) != namespace:
                continue
            other = registered.get(other_id) or resolve_space(cfg, other_id)
            if other.backend != backend:
                continue
            # Different PostgreSQL DSNs can address the same database.
            if backend == "postgres":
                raise SpaceConflictError(f"PostgreSQL namespace {namespace!r} is already used by space {other_id!r}")
            if path is not None and _same_sqlite_database(
                path, sqlite_database_path(other.backend_config), probe_missing=True
            ):
                raise SpaceConflictError(f"SQLite table prefix {namespace!r} in this database is used by {other_id!r}")

    def get_space(self, space_id: str) -> Space | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memory_spaces WHERE space_id=?", (space_id,)).fetchone()
        return self._space(row) if row else None

    def list_spaces(self) -> list[Space]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM memory_spaces ORDER BY created_at").fetchall()
        return [self._space(row) for row in rows]

    @staticmethod
    def _space(row: sqlite3.Row) -> Space:
        return Space(
            space_id=row["space_id"],
            display_name=row["display_name"],
            backend=row["backend"],
            backend_config=json.loads(row["backend_config"] or "{}"),
            created_by=row["created_by"],
            created_at=row["created_at"],
            disabled_at=row["disabled_at"],
        )

    # ── tokens ────────────────────────────────────────────────────────────
    def create_token(self, space_id: str, client_name: str, scopes: Iterable[str]) -> tuple[Token, str]:
        plaintext = _new_token()
        token_id = "mt_" + secrets.token_hex(8)
        scopes_json = json.dumps(sorted(set(scopes)), ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memory_space_tokens"
                " (token_id, space_id, client_name, scopes, token_hash, token_hint,"
                "  created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    token_id,
                    space_id,
                    client_name,
                    scopes_json,
                    _hash(plaintext),
                    plaintext[-4:],
                    _now(),
                ),
            )
        return self.get_token(token_id), plaintext  # type: ignore[return-value]

    def get_token(self, token_id: str) -> Token | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memory_space_tokens WHERE token_id=?", (token_id,)).fetchone()
        return self._token(row) if row else None

    def list_tokens(self, space_id: str) -> list[Token]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_space_tokens WHERE space_id=? ORDER BY created_at",
                (space_id,),
            ).fetchall()
        return [self._token(row) for row in rows]

    @staticmethod
    def _token(row: sqlite3.Row) -> Token:
        return Token(
            token_id=row["token_id"],
            space_id=row["space_id"],
            client_name=row["client_name"],
            scopes=json.loads(row["scopes"] or "[]"),
            token_hint=row["token_hint"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"],
            first_used_at=row["first_used_at"],
            request_count=row["request_count"],
        )

    def authenticate(self, plaintext: str) -> Token | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_space_tokens WHERE token_hash=? AND revoked_at IS NULL",
                (_hash(plaintext),),
            ).fetchone()
        return self._token(row) if row else None

    def rotate(self, token_id: str) -> tuple[Token, str]:
        plaintext = _new_token()
        with self.write_transaction():
            token = self.get_token(token_id)
            if token is None:
                raise KeyError(token_id)
            if token.revoked_at is not None:
                raise ValueError("token is revoked; issue a new token for this client")
            self._conn.execute(
                "UPDATE memory_space_tokens SET token_hash=?, token_hint=? WHERE token_id=?",
                (_hash(plaintext), plaintext[-4:], token_id),
            )
            return self.get_token(token_id), plaintext  # type: ignore[return-value]

    def revoke(self, token_id: str) -> bool:
        with self.write_transaction():
            cur = self._conn.execute(
                "UPDATE memory_space_tokens SET revoked_at=? WHERE token_id=? AND revoked_at IS NULL",
                (_now(), token_id),
            )
        return cur.rowcount > 0

    def update_token(
        self,
        token_id: str,
        *,
        client_name: str | None = None,
        scopes: Iterable[str] | None = None,
        space_id: str | None = None,
    ) -> Token | None:
        with self.write_transaction():
            token = self.get_token(token_id)
            if token is None:
                return None
            if client_name is not None:
                self._conn.execute(
                    "UPDATE memory_space_tokens SET client_name=? WHERE token_id=?",
                    (client_name, token_id),
                )
            if scopes is not None:
                self._conn.execute(
                    "UPDATE memory_space_tokens SET scopes=? WHERE token_id=?",
                    (json.dumps(sorted(set(scopes))), token_id),
                )
            if space_id is not None and space_id != token.space_id:
                self._conn.execute(
                    "UPDATE memory_space_tokens SET space_id=? WHERE token_id=?",
                    (space_id, token_id),
                )
            return self.get_token(token_id)

    def record_use(self, plaintext: str) -> bool:
        now = _now()
        with self._lock, self._conn:
            updated = self._conn.execute(
                "UPDATE memory_space_tokens SET first_used_at=COALESCE(first_used_at, ?), "
                "last_used_at=?, request_count=request_count+1 "
                "WHERE token_hash=? AND revoked_at IS NULL",
                (now, now, _hash(plaintext)),
            )
            return updated.rowcount == 1

    # ── optional legacy cleanup ───────────────────────────────────────────
    def legacy_group_schema(self) -> dict[str, bool]:
        """Whether an older control DB still carries access-group leftovers."""
        with self._lock:
            table = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_access_groups'"
            ).fetchone()
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(memory_space_tokens)")}
        return {"table": table is not None, "group_id_column": "group_id" in columns}

    def cleanup_legacy_groups(self, *, dry_run: bool = False) -> dict[str, bool]:
        """Drop the retired access-group table/column (optional maintenance)."""
        state = self.legacy_group_schema()
        if dry_run or not any(state.values()):
            return {**state, "applied": False}
        with self._lock, self._conn:
            if state["table"]:
                self._conn.execute("DROP TABLE IF EXISTS memory_access_groups")
            if state["group_id_column"]:
                with suppress(sqlite3.OperationalError):
                    # Older SQLite: leaving the unused column is harmless.
                    self._conn.execute("ALTER TABLE memory_space_tokens DROP COLUMN group_id")
        return {**self.legacy_group_schema(), "applied": True}

    # ── admin sessions / codes ────────────────────────────────────────────
    def create_admin_code(self, space_id: str, ttl_seconds: int = 600) -> str:
        code = "mcode_" + secrets.token_urlsafe(24)
        expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memory_admin_codes VALUES (?,?,?,?,NULL)",
                (_hash(code), space_id, json.dumps(["admin"]), expires),
            )
        return code

    def redeem_admin_code(self, code: str, session_ttl_seconds: int = 7 * 86400) -> tuple[str, Session]:
        code_hash = _hash(code)
        with self._lock:
            row = self._conn.execute("SELECT * FROM memory_admin_codes WHERE code_hash=?", (code_hash,)).fetchone()
            if row is None or row["used_at"] is not None:
                raise ValueError("invalid or used code")
            if row["expires_at"] < _now():
                raise ValueError("code expired")
            session = "msess_" + secrets.token_urlsafe(32)
            expires = (datetime.now(UTC) + timedelta(seconds=session_ttl_seconds)).isoformat()
            with self._conn:
                self._conn.execute(
                    "UPDATE memory_admin_codes SET used_at=? WHERE code_hash=?",
                    (_now(), code_hash),
                )
                self._conn.execute(
                    "INSERT INTO memory_admin_sessions VALUES (?,?,?,?)",
                    (_hash(session), row["space_id"], row["scopes"], expires),
                )
        return session, Session(
            session_id=_hash(session),
            space_id=row["space_id"],
            scopes=json.loads(row["scopes"] or "[]"),
            expires_at=expires,
        )

    def verify_session(self, plaintext: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_admin_sessions WHERE session_hash=?", (_hash(plaintext),)
            ).fetchone()
        if row is None or row["expires_at"] < _now():
            return None
        return Session(
            session_id=row["session_hash"],
            space_id=row["space_id"],
            scopes=json.loads(row["scopes"] or "[]"),
            expires_at=row["expires_at"],
        )


__all__ = ["ControlStore", "Session", "Space", "Token"]
