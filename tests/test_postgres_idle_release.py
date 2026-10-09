"""Server-less tests for idle-state handling of the Postgres backend.

A loaded-but-unused agent must not hold Postgres connections for the rest of
its life — dozens of agents behind one control plane otherwise exhaust
``max_connections`` (TencentCloud/Octop#1795). Three changes deliver that, and
none of them needs a running server:

- the backend closes its own socket after ``_IDLE_SESSION_TIMEOUT_S`` with no
  further query, including when the server rejects ``idle_session_timeout``
  (PostgreSQL < 14); the next operation redials;
- the same threshold is also sent as ``idle_session_timeout`` on PostgreSQL
  14+, so the server drops the session if this process never comes back;
- the checkpointer pool is built with ``min_size=0``, and checkpoint pruning
  refuses a Postgres backend *before* opening that pool. Hourly maintenance
  would otherwise pin a connection per loaded agent.

Real-server behaviour of the same code stays covered by
``tests/test_postgres.py`` / ``tests/test_checkpointer.py``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from octop_memory.storage.backends.postgres import (
    _IDLE_SESSION_TIMEOUT_S,
    SHARED_SCHEMA,
    PostgresMemoryBackend,
)

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")
pytest.importorskip("langgraph.checkpoint.postgres")

from psycopg.pq import TransactionStatus  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402


class _FakeCursor:
    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        return self

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> None:
        return None


class _FakeInfo:
    transaction_status = TransactionStatus.IDLE


class _FakeConnection:
    """Records the session-level SQL a real connection would receive."""

    def __init__(self, fail_execute_on: int | None = None, fail_rollback: bool = False) -> None:
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.info = _FakeInfo()
        self._fail_execute_on = fail_execute_on
        self._fail_rollback = fail_rollback

    def execute(self, sql: str, params: Any = None) -> None:
        if self._fail_execute_on is not None and len(self.statements) + 1 == self._fail_execute_on:
            raise psycopg.errors.UndefinedObject('unrecognized configuration parameter "idle_session_timeout"')
        self.statements.append(sql)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        if self._fail_rollback:
            raise psycopg.OperationalError("connection is gone")

    def close(self) -> None:
        self.closed = True

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield


def _backend_with(conn: _FakeConnection, **attrs: Any) -> Any:
    """Build a backend around a fake connection without running ``__init__``."""
    backend = object.__new__(PostgresMemoryBackend)
    backend._conn = conn
    backend._closed = False
    backend._in_transaction = False
    backend._booting = False
    backend._last_reconnect_at = 0.0
    backend._last_used_at = time.monotonic()
    backend._idle_release_enabled = False
    backend._idle_timer = None
    backend._conn_lock = threading.RLock()
    for name, value in attrs.items():
        setattr(backend, name, value)
    return backend


@pytest.fixture(autouse=True)
def _stop_idle_timers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Cancel client-side idle timers before they can fire across tests."""
    started: list[threading.Timer] = []
    original = threading.Timer.start

    def _start(self: threading.Timer) -> None:
        if self.name == "octop-memory-pg-idle":
            started.append(self)
        original(self)

    monkeypatch.setattr(threading.Timer, "start", _start)
    yield
    for timer in started:
        timer.cancel()


class TestConstruction:
    def test_bootstrap_asks_the_server_for_idle_release(self) -> None:
        """The bootstrap connection is the long-lived one the issue is about."""
        conn = _FakeConnection()
        backend = _backend_with(conn, _booting=True)

        backend._init_schema()

        assert backend._idle_release_enabled is True
        assert conn.statements[-1] == f"SET idle_session_timeout = '{_IDLE_SESSION_TIMEOUT_S}s'"


class TestEnableIdleRelease:
    def test_sets_the_session_timeout(self) -> None:
        conn = _FakeConnection()
        backend = _backend_with(conn)

        assert backend._enable_idle_release() is True
        assert conn.statements == [f"SET idle_session_timeout = '{_IDLE_SESSION_TIMEOUT_S}s'"]
        assert conn.commits == 1

    def test_unsupported_server_degrades_gracefully(self) -> None:
        """PostgreSQL < 14 rejects the SET; that must not break construction."""
        conn = _FakeConnection(fail_execute_on=1)
        backend = _backend_with(conn)

        assert backend._enable_idle_release() is False
        assert conn.rollbacks == 1

    def test_a_failing_rollback_is_still_swallowed(self) -> None:
        conn = _FakeConnection(fail_execute_on=1, fail_rollback=True)
        backend = _backend_with(conn)

        assert backend._enable_idle_release() is False


class TestRestoreSessionState:
    def test_reapplies_connection_scoped_settings(self) -> None:
        conn = _FakeConnection()
        backend = _backend_with(conn)

        backend._restore_session_state()

        assert conn.statements == [
            f"SET search_path TO {SHARED_SCHEMA}, public",
            f"SET idle_session_timeout = '{_IDLE_SESSION_TIMEOUT_S}s'",
        ]
        assert backend._idle_release_enabled is True

    def test_flag_reflects_a_rejecting_server(self) -> None:
        conn = _FakeConnection(fail_execute_on=2)
        backend = _backend_with(conn, _idle_release_enabled=True)

        backend._restore_session_state()

        assert backend._idle_release_enabled is False


class TestIdleSessionExpired:
    def test_false_while_recently_used(self) -> None:
        backend = _backend_with(_FakeConnection(), _idle_release_enabled=True)

        assert backend._idle_session_expired() is False

    def test_true_once_locally_idle_past_the_threshold(self) -> None:
        backend = _backend_with(_FakeConnection(), _idle_release_enabled=True)
        backend._last_used_at = time.monotonic() - _IDLE_SESSION_TIMEOUT_S

        assert backend._idle_session_expired() is True

    def test_true_even_when_the_server_rejected_the_setting(self) -> None:
        """PostgreSQL < 14 must still count as idle. Client close does not use the flag."""
        backend = _backend_with(_FakeConnection(), _idle_release_enabled=False)
        backend._last_used_at = 0.0

        assert backend._idle_session_expired() is True


class TestReconnectIfDead:
    def test_idle_expired_connection_is_released_and_redialled(self) -> None:
        old = _FakeConnection()
        new = _FakeConnection()
        backend = _backend_with(old, _idle_release_enabled=True)
        backend._last_used_at = time.monotonic() - _IDLE_SESSION_TIMEOUT_S - 1
        redials: list[int] = []

        def fake_connect() -> _FakeConnection:
            redials.append(1)
            return new

        backend._connect = fake_connect

        backend._reconnect_if_dead()

        assert redials == [1]
        assert old.closed  # let go of the session the server already dropped
        assert backend._conn is new
        assert any("search_path" in stmt for stmt in new.statements)
        assert any("idle_session_timeout" in stmt for stmt in new.statements)
        # Idle tracking restarts, so the next operation does not redial again.
        assert backend._idle_session_expired() is False

    def test_fresh_connection_is_left_alone(self) -> None:
        conn = _FakeConnection()
        backend = _backend_with(conn, _idle_release_enabled=True)

        def unexpected_redial() -> _FakeConnection:
            raise AssertionError("a recently-used connection must not be redialled")

        backend._connect = unexpected_redial

        backend._reconnect_if_dead()

        assert not conn.closed

    def test_idle_connection_is_redialled_even_when_the_server_rejected_the_setting(self) -> None:
        """A rejecting server used to pin the socket for the process lifetime."""
        old = _FakeConnection()
        new = _FakeConnection()
        backend = _backend_with(old, _idle_release_enabled=False)
        backend._last_used_at = 0.0

        def fake_connect() -> _FakeConnection:
            return new

        backend._connect = fake_connect

        backend._reconnect_if_dead()

        assert old.closed
        assert backend._conn is new


class TestLastUsedTracking:
    def test_cursor_marks_the_connection_used(self) -> None:
        backend = _backend_with(_FakeConnection())
        backend._last_used_at = time.monotonic() - 5

        before = time.monotonic()
        with backend._cursor():
            pass

        assert before <= backend._last_used_at <= time.monotonic()

    def test_cursor_marks_the_connection_used_even_on_failure(self) -> None:
        backend = _backend_with(_FakeConnection())
        stale = time.monotonic() - 5
        backend._last_used_at = stale

        with pytest.raises(RuntimeError), backend._cursor():
            raise RuntimeError("statement blew up")

        assert backend._last_used_at >= stale

    def test_transaction_marks_the_connection_used(self) -> None:
        backend = _backend_with(_FakeConnection())
        backend._last_used_at = time.monotonic() - 5

        before = time.monotonic()
        with backend.transaction():
            pass

        assert before <= backend._last_used_at <= time.monotonic()


class TestProactiveIdleClose:
    """The connection has to go away while nobody is querying.

    Redial-on-next-use never runs for an agent that was loaded at boot and
    then left alone, which is how ``max_connections`` filled up over weeks.
    """

    def test_idle_connection_is_closed_without_a_following_query(self) -> None:
        conn = _FakeConnection()
        backend = _backend_with(conn, _idle_release_enabled=False)
        backend._last_used_at = time.monotonic() - _IDLE_SESSION_TIMEOUT_S - 1

        backend._release_idle_connection()

        assert conn.closed
        assert backend._conn is conn  # close only; the next operation redials

    def test_recent_connection_is_kept(self) -> None:
        conn = _FakeConnection()
        backend = _backend_with(conn)

        backend._release_idle_connection()

        assert not conn.closed
        assert backend._idle_timer is not None

    def test_in_flight_transaction_is_kept(self) -> None:
        conn = _FakeConnection()
        backend = _backend_with(conn, _in_transaction=True)
        backend._last_used_at = 0.0

        backend._release_idle_connection()

        assert not conn.closed

    def test_close_cancels_the_timer_and_does_not_redial(self) -> None:
        conn = _FakeConnection()
        backend = _backend_with(conn)
        backend._arm_idle_timer()
        assert backend._idle_timer is not None

        backend.close()

        assert conn.closed
        assert backend._idle_timer is None
        assert backend._closed is True


class TestPruneDoesNotOpenPostgresPool:
    def test_postgres_backend_is_rejected_before_ensure_checkpointer(self) -> None:
        from octop_memory.pipeline.lifecycle.checkpoint_gc import _get_checkpointer_conn

        class _Mem:
            def __init__(self) -> None:
                self._backend = object.__new__(PostgresMemoryBackend)
                self.ensured = False

            def _ensure_checkpointer(self) -> None:
                self.ensured = True

        memory = _Mem()
        with pytest.raises(RuntimeError, match="PostgreSQL checkpoint pruning is not yet supported"):
            _get_checkpointer_conn(memory)  # type: ignore[arg-type]

        assert memory.ensured is False


class TestCheckpointerPoolDoesNotPinConnections:
    """``min_size=1`` kept one Postgres connection per loaded agent open for
    the process lifetime; ``min_size=0`` lets the pool release idle ones
    (TencentCloud/Octop#1795).
    """

    def test_pool_is_built_without_a_minimum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import langgraph.checkpoint.postgres as lg_postgres
        import psycopg_pool

        from octop_memory.core import Memory

        captured: dict[str, Any] = {}

        class FakePool:
            # The construction passes the pool's own static checker to ``check=``.
            check_connection = staticmethod(psycopg_pool.ConnectionPool.check_connection)

            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        class FakeSaver:
            def __init__(self, pool: Any) -> None:
                self.pool = pool

            def setup(self) -> None:
                captured["setup"] = True

        monkeypatch.setattr("psycopg_pool.ConnectionPool", FakePool)
        monkeypatch.setattr(lg_postgres, "PostgresSaver", FakeSaver)
        monkeypatch.setattr(
            "octop_memory.pipeline.lifecycle.vacuum.tune_checkpoint_autovacuum",
            lambda dsn: None,
        )

        backend = object.__new__(PostgresMemoryBackend)
        backend._dsn = "postgresql://localhost:5432/octop_memory"
        memory = object.__new__(Memory)
        memory._backend = backend

        saver = memory._create_postgres_checkpointer()

        assert isinstance(saver, FakeSaver)
        assert saver.pool is memory._checkpointer_pool
        assert captured["min_size"] == 0
        assert captured["max_size"] == 4
        assert captured["conninfo"] == backend._dsn
        assert captured["kwargs"]["row_factory"] is dict_row
        assert captured["setup"] is True
