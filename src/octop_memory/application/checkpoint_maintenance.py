"""Backend dispatch for explicit checkpoint maintenance; never boots an agent."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from octop_memory.pipeline.lifecycle.checkpoint_gc import _postgres_backend

if TYPE_CHECKING:
    from octop_memory.core import Memory
    from octop_memory.pipeline.lifecycle.checkpoint_gc import CheckpointGcStats

__all__ = [
    "backup_postgres_checkpoints",
    "maintain_checkpoints",
    "postgres_checkpoint_backup_key",
    "run_checkpoint_retention",
    "slim_live_checkpoints",
]


def run_checkpoint_retention(memory: Memory, *, keep_last: int) -> CheckpointGcStats | None:
    """Run dependency-aware host retention on completed SQLite or PostgreSQL threads.

    Return None for other backends, whose checkpoint stores are outside this maintenance.
    """
    from octop_memory.storage.backends.sqlite import SqliteMemoryBackend

    if not isinstance(getattr(memory, "backend", None), SqliteMemoryBackend) and _postgres_backend(memory) is None:
        return None

    from octop_memory.pipeline.lifecycle.checkpoint_gc import prune_checkpoints

    return prune_checkpoints(memory, keep_last=keep_last, skip_pending=True)


def postgres_checkpoint_backup_key(memory: Memory) -> str | None:
    """Identify the PG checkpoint store without exposing connection credentials."""
    backend = _postgres_backend(memory)
    if backend is None:
        return None
    from psycopg.conninfo import conninfo_to_dict

    options = conninfo_to_dict(backend._dsn)
    identity = tuple(
        backend._connect_kwargs.get(key, options.get(key))
        for key in ("host", "hostaddr", "port", "dbname", "service", "options", "user")
    )
    return hashlib.sha256(repr(identity).encode()).hexdigest()


def backup_postgres_checkpoints(memory: Memory, output: BinaryIO) -> None:
    """Stream checkpoint rows as SQL for a matching initialized database."""
    backend = _postgres_backend(memory)
    if backend is None:
        raise ValueError("PostgreSQL checkpoint backup requires a PostgreSQL backend")
    from octop_memory.storage.backends.postgres_checkpoint_retention import backup_checkpoint_tables

    backup_checkpoint_tables(backend, output)


def maintain_checkpoints(db_path: Path, *, backend: str = "sqlite", **options: Any) -> dict[str, Any]:
    """Inspect/migrate SQLite checkpoints; reject unsupported backends explicitly.

    Options and offline guarantees are defined by the SQLite maintenance job.
    Import it lazily so the core/CLI remain usable without LangGraph extras.
    """
    if backend != "sqlite":
        raise ValueError("Checkpoint field migration is SQLite-only; PostgreSQL keeps its existing saver")
    from octop_memory.storage.backends.sqlite_checkpoint_job import maintain_checkpoints as run

    return run(db_path, **options)


def slim_live_checkpoints(db_path: Path, *, backend: str = "sqlite", **options: Any) -> dict[str, Any]:
    """Host-only live maintenance; the host supplies compatible readers and a pause gate."""
    if backend != "sqlite":
        raise ValueError("Live checkpoint maintenance is SQLite-only")
    from octop_memory.storage.backends.sqlite_checkpoint_live import slim_live_checkpoints as run

    return run(db_path, **options)
