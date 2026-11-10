"""Backend dispatch for explicit checkpoint maintenance; never boots an agent."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from octop_memory.storage.backends.sqlite_checkpoint_pins import pin_sqlite_checkpoints

if TYPE_CHECKING:
    from octop_memory.core import Memory
    from octop_memory.pipeline.lifecycle.checkpoint_gc import CheckpointGcStats

__all__ = ["maintain_checkpoints", "pin_sqlite_checkpoints", "run_checkpoint_retention", "slim_live_checkpoints"]


def run_checkpoint_retention(memory: Memory, *, keep_last: int) -> CheckpointGcStats | None:
    """Run dependency-aware host retention on completed SQLite threads.

    Preserve pinned checkpoints and their dependencies. Return None for
    other backends, whose checkpoint stores are outside this maintenance.
    """
    from octop_memory.storage.backends.sqlite import SqliteMemoryBackend

    if not isinstance(getattr(memory, "backend", None), SqliteMemoryBackend):
        return None

    from octop_memory.pipeline.lifecycle.checkpoint_gc import prune_checkpoints

    return prune_checkpoints(memory, keep_last=keep_last, skip_pending=True)


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
