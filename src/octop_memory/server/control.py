"""Control-plane store: spaces, tokens, grants and short-lived admin sessions."""

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

from octop_memory.storage.backends.namespace import normalize_postgres_namespace

ALL_SPACES = "*"


class SpaceConflictError(ValueError):
    """A space ID or its underlying storage namespace is already in use."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row_get(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


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
    client_stats: dict[str, Any]
    disabled_clients: list[str]


@dataclass(frozen=True)
class Session:
    session_id: str
    space_id: str
    scopes: list[str]
    expires_at: str


class ControlStore:
    """SQLite control store. One file, WAL, thread-safe via a single lock."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser()
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
                  client_stats TEXT NOT NULL DEFAULT '{}',
                  disabled_clients TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_tokens_space ON memory_space_tokens(space_id);
                CREATE TABLE IF NOT EXISTS memory_space_grants (
                  grant_id TEXT PRIMARY KEY,
                  token_id TEXT NOT NULL,
                  space_id TEXT NOT NULL,
                  scopes TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(token_id, space_id)
                );
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
                CREATE TABLE IF NOT EXISTS memory_token_map (
                  octop_token_id TEXT PRIMARY KEY,
                  memory_token_id TEXT NOT NULL,
                  space_id TEXT NOT NULL,
                  migrated_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(memory_space_tokens)")}
            if "disabled_clients" not in columns:
                self._conn.execute(
                    "ALTER TABLE memory_space_tokens ADD COLUMN disabled_clients TEXT NOT NULL DEFAULT '[]'"
                )

    # ── spaces ────────────────────────────────────────────────────────────
    def create_space(
        self,
        space_id: str,
        display_name: str,
        backend: str,
        backend_config: dict[str, Any],
        *,
        created_by: str = "local",
    ) -> Space:
        if not space_id.strip() or space_id == ALL_SPACES:
            raise ValueError("space ID must be nonempty and cannot be '*'")
        with self.write_transaction():
            if self.get_space(space_id) is not None:
                raise SpaceConflictError(f"space {space_id!r} already exists")
            if backend == "postgres":
                self.check_postgres_namespace(space_id)
            self._conn.execute(
                "INSERT INTO memory_spaces VALUES (?,?,?,?,?,?,NULL)",
                (
                    space_id,
                    display_name,
                    backend,
                    json.dumps(backend_config, ensure_ascii=False),
                    created_by,
                    _now(),
                ),
            )
            return self.get_space(space_id)  # type: ignore[return-value]

    def check_postgres_namespace(self, space_id: str) -> None:
        """Require unique PostgreSQL namespaces in this control store.

        Different connection strings can point to the same database, so the
        check covers every PostgreSQL space, independent of its DSN.
        """
        namespace = normalize_postgres_namespace(space_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT space_id FROM memory_spaces WHERE backend = 'postgres' AND space_id != ?",
                (space_id,),
            ).fetchall()
        if any(normalize_postgres_namespace(row["space_id"]) == namespace for row in rows):
            raise SpaceConflictError(f"PostgreSQL namespace {namespace!r} is already used by another space")

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
                "  created_at, last_used_at, revoked_at, client_stats,"
                "  disabled_clients)"
                " VALUES (?,?,?,?,?,?,?,NULL,NULL,'{}','[]')",
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

    def import_token(
        self,
        *,
        space_id: str,
        client_name: str,
        scopes: Iterable[str],
        token_hash: str,
        token_hint: str,
        created_at: str,
        last_used_at: str | None = None,
        revoked_at: str | None = None,
        octop_token_id: str = "",
        extra_space_ids: Iterable[str] = (),
        disabled_clients: Iterable[str] = (),
        client_stats: dict[str, Any] | None = None,
    ) -> Token:
        token_id = "mt_" + secrets.token_hex(8)
        scopes = sorted(set(scopes))
        with self.write_transaction():
            self._conn.execute(
                "INSERT INTO memory_space_tokens"
                " (token_id, space_id, client_name, scopes, token_hash, token_hint,"
                "  created_at, last_used_at, revoked_at, client_stats, disabled_clients)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    token_id,
                    space_id,
                    client_name,
                    json.dumps(scopes, ensure_ascii=False),
                    token_hash,
                    token_hint,
                    created_at,
                    last_used_at,
                    revoked_at,
                    json.dumps(client_stats or {}, ensure_ascii=False),
                    json.dumps(sorted(set(disabled_clients)), ensure_ascii=False),
                ),
            )
            if octop_token_id:
                self._conn.execute(
                    "INSERT OR REPLACE INTO memory_token_map VALUES (?,?,?,?)",
                    (octop_token_id, token_id, space_id, _now()),
                )
            for extra in dict.fromkeys(extra_space_ids):
                if extra != space_id:
                    self.add_grant(token_id, extra, scopes)
            return self.get_token(token_id)  # type: ignore[return-value]

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

    def token_by_hash(self, token_hash: str) -> Token | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memory_space_tokens WHERE token_hash=?", (token_hash,)).fetchone()
        return self._token(row) if row else None

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
            client_stats=json.loads(row["client_stats"] or "{}"),
            disabled_clients=json.loads(_row_get(row, "disabled_clients") or "[]"),
        )

    def authenticate(self, plaintext: str) -> Token | None:
        token = self.token_by_hash(_hash(plaintext))
        if token is None or token.revoked_at is not None:
            return None
        return token

    def allowed_spaces(self, token: Token) -> list[tuple[str, list[str]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT space_id, scopes FROM memory_space_grants WHERE token_id=?",
                (token.token_id,),
            ).fetchall()
        return [(token.space_id, token.scopes)] + [(row["space_id"], json.loads(row["scopes"] or "[]")) for row in rows]

    def add_grant(self, token_id: str, space_id: str, scopes: Iterable[str]) -> None:
        with self.write_transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO memory_space_grants VALUES (?,?,?,?,?)",
                (
                    "mg_" + secrets.token_hex(8),
                    token_id,
                    space_id,
                    json.dumps(sorted(set(scopes)), ensure_ascii=False),
                    _now(),
                ),
            )

    def rotate(self, token_id: str) -> tuple[Token, str]:
        plaintext = _new_token()
        with self.write_transaction():
            if self.get_token(token_id) is None:
                raise KeyError(token_id)
            self._conn.execute(
                "UPDATE memory_space_tokens SET token_hash=?, token_hint=?,"
                " revoked_at=NULL, last_used_at=NULL WHERE token_id=?",
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
        disabled_clients: Iterable[str] | None = None,
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
            if disabled_clients is not None:
                self._conn.execute(
                    "UPDATE memory_space_tokens SET disabled_clients=? WHERE token_id=?",
                    (json.dumps(sorted(set(disabled_clients))), token_id),
                )
            if space_id is not None and space_id != token.space_id:
                self._conn.execute(
                    "UPDATE memory_space_tokens SET space_id=? WHERE token_id=?",
                    (space_id, token_id),
                )
                self._conn.execute("DELETE FROM memory_space_grants WHERE token_id=?", (token_id,))
            return self.get_token(token_id)

    def record_client(self, token_id: str, client: str) -> None:
        token = self.get_token(token_id)
        if token is None:
            return
        stats = dict(token.client_stats)
        entry = dict(stats.get(client) or {})
        entry.setdefault("first_seen", _now())
        entry["last_seen"] = _now()
        entry["count"] = int(entry.get("count") or 0) + 1
        stats[client] = entry
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE memory_space_tokens SET client_stats=?, last_used_at=? WHERE token_id=?",
                (json.dumps(stats, ensure_ascii=False), _now(), token_id),
            )

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
