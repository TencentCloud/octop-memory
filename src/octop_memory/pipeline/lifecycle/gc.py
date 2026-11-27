"""Lifecycle GC for old / dead data (5.6, D47-C cron + CLI).

Implements the cleanup rules from design doc §17 plus journal retention
(ADR-028 / RISK-026 ③):

| Data                                | Retention | Action          |
|-------------------------------------|-----------|-----------------|
| ``rejected`` candidate              | 30 days   | DELETE          |
| ``deprecated`` atom (superseded)    | 90 days   | DELETE          |
| Raw event with no atom referencing it | 180 days | DELETE         |
| Pipeline journal (``extract_run``,  | 14 days   | DELETE          |
| ``gc_*``, ``page_regen*``,          |           | (not journaled) |
| ``consolidate``)                    |           |                 |

Decision journal rows (``promote`` / ``reject`` / ``deprecate`` / ``merge``
/ ``conflict`` / ``entity_merge`` / …) are not expired.

D47-C: both **cron mode** (caller drives a scheduler) and **CLI mode**
(``octop-memory gc run``) call into the same :func:`run_gc` entry point so
the behaviour is identical.

Soft-delete vs hard-delete: design doc called for hard delete *and* a
per-row ``gc_*`` journal. ADR-028 drops the per-row journal — those rows
were feeding the table this pass is supposed to shrink (same reason
checkpoint prune does not journal; ADR-022). ``run_gc`` returns counts
instead.

Each pass is bounded by ``--max-rows`` so a never-run-before namespace
doesn't lock the database for minutes when we finally launch the worker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from octop_memory.core import Memory


# Default retention windows per design doc §17. Override per call as
# needed; tests typically pass tiny windows so synthetic fixtures
# trigger cleanup immediately.
DEFAULT_REJECTED_CANDIDATE_DAYS = 30
DEFAULT_DEPRECATED_ATOM_DAYS = 90
DEFAULT_ORPHAN_RAW_DAYS = 180
DEFAULT_JOURNAL_PIPELINE_DAYS = 14
"""Pipeline / heartbeat journal rows older than this are deleted (ADR-028)."""

PIPELINE_JOURNAL_ACTIONS: tuple[str, ...] = (
    "extract_run",
    "gc_rejected_candidate",
    "gc_deprecated_atom",
    "gc_orphan_raw_event",
    "page_regen",
    "page_regen_failed",
    "consolidate",
)
"""Journal actions that are observability, not a user-facing decision audit."""

DEFAULT_MAX_ROWS_PER_PASS = 500
"""Soft cap so a single GC tick can't lock the DB on a huge backlog.
Caller can run multiple passes if needed."""

_GC_BACKEND_METHODS: tuple[str, ...] = (
    "delete_rejected_candidates",
    "delete_deprecated_atoms",
    "delete_orphan_raw_events",
    "delete_journal",
)


@dataclass
class GcStats:
    """Per-action row counts. ``dry_run`` reports what *would* be deleted."""

    rejected_candidates_deleted: int = 0
    deprecated_atoms_deleted: int = 0
    orphan_raw_events_deleted: int = 0
    journal_rows_deleted: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    dry_run: bool = False

    @property
    def total_deleted(self) -> int:
        return (
            self.rejected_candidates_deleted
            + self.deprecated_atoms_deleted
            + self.orphan_raw_events_deleted
            + self.journal_rows_deleted
        )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_gc(
    memory: Memory,
    *,
    rejected_candidate_days: int = DEFAULT_REJECTED_CANDIDATE_DAYS,
    deprecated_atom_days: int = DEFAULT_DEPRECATED_ATOM_DAYS,
    orphan_raw_days: int = DEFAULT_ORPHAN_RAW_DAYS,
    journal_pipeline_days: int = DEFAULT_JOURNAL_PIPELINE_DAYS,
    max_rows_per_pass: int = DEFAULT_MAX_ROWS_PER_PASS,
    include_orphan_raw: bool = True,
    dry_run: bool = False,
    now: datetime | None = None,
) -> GcStats:
    """Run one full GC pass over the namespace.

    Four sub-passes execute in this order, each capped by
    ``max_rows_per_pass``:

    1. Rejected candidates older than ``rejected_candidate_days``.
    2. Deprecated atoms older than ``deprecated_atom_days``.
    3. Orphan raw events (no atom or candidate references them) older than
       ``orphan_raw_days``. Skipped when ``include_orphan_raw`` is
       false (startup compact — that pass used to load every raw row).
    4. Pipeline journal rows older than ``journal_pipeline_days``.
       This pass does not write journal.

    Set ``dry_run=True`` to count without deleting; useful for the
    ``--dry-run`` CLI mode and for the eval harness.

    Both SQLite and PostgreSQL backends implement the bounded retention
    methods on :class:`~octop_memory.storage.backends.MemoryBackend`.
    Custom or partial backends that do not expose those methods no-op
    cleanly without touching underlying connections so periodic
    maintenance ticks keep running.
    """
    backend = getattr(memory, "_backend", None)
    if not _supports_lifecycle_gc(backend):
        logger.debug(
            "run_gc skipped: backend %s does not implement lifecycle retention methods",
            type(backend).__name__,
        )
        now_ts = datetime.now(UTC)
        return GcStats(started_at=now_ts, finished_at=now_ts, dry_run=dry_run)

    stats = GcStats(started_at=datetime.now(UTC), dry_run=dry_run)
    cutoff_now = now or datetime.now(UTC)

    cand_cutoff = cutoff_now - timedelta(days=rejected_candidate_days)
    stats.rejected_candidates_deleted = _gc_rejected_candidates(
        memory,
        cutoff=cand_cutoff,
        limit=max_rows_per_pass,
        dry_run=dry_run,
    )

    atom_cutoff = cutoff_now - timedelta(days=deprecated_atom_days)
    stats.deprecated_atoms_deleted = _gc_deprecated_atoms(
        memory,
        cutoff=atom_cutoff,
        limit=max_rows_per_pass,
        dry_run=dry_run,
    )

    if include_orphan_raw:
        raw_cutoff = cutoff_now - timedelta(days=orphan_raw_days)
        stats.orphan_raw_events_deleted = _gc_orphan_raw_events(
            memory,
            cutoff=raw_cutoff,
            limit=max_rows_per_pass,
            dry_run=dry_run,
        )

    journal_cutoff = cutoff_now - timedelta(days=journal_pipeline_days)
    stats.journal_rows_deleted = _gc_pipeline_journal(
        memory,
        cutoff=journal_cutoff,
        limit=max_rows_per_pass,
        dry_run=dry_run,
    )

    stats.finished_at = datetime.now(UTC)
    return stats


# ---------------------------------------------------------------------------
# Sub-pass helpers
# ---------------------------------------------------------------------------


def _supports_lifecycle_gc(backend: Any) -> bool:
    if backend is None:
        return False
    return all(callable(getattr(backend, name, None)) for name in _GC_BACKEND_METHODS)


def _gc_rejected_candidates(
    memory: Memory,
    *,
    cutoff: datetime,
    limit: int,
    dry_run: bool,
) -> int:
    """Delete candidates with status='rejected' and decided_at < cutoff."""
    return memory.delete_rejected_candidates(
        before=cutoff,
        limit=limit,
        dry_run=dry_run,
    )


def _gc_deprecated_atoms(
    memory: Memory,
    *,
    cutoff: datetime,
    limit: int,
    dry_run: bool,
) -> int:
    """Delete atoms with deprecated_at < cutoff."""
    return memory.delete_deprecated_atoms(
        before=cutoff,
        limit=limit,
        dry_run=dry_run,
    )


def _gc_orphan_raw_events(
    memory: Memory,
    *,
    cutoff: datetime,
    limit: int,
    dry_run: bool,
) -> int:
    """Delete unreferenced raw rows older than ``cutoff`` without hydrating blobs."""
    return memory.delete_orphan_raw_events(
        before=cutoff,
        limit=limit,
        dry_run=dry_run,
    )


def _gc_pipeline_journal(
    memory: Memory,
    *,
    cutoff: datetime,
    limit: int,
    dry_run: bool,
) -> int:
    """Expire observability journal rows. Does not append journal."""
    return memory.delete_journal(
        actions=PIPELINE_JOURNAL_ACTIONS,
        before=cutoff,
        limit=limit,
        dry_run=dry_run,
    )


__all__ = [
    "DEFAULT_DEPRECATED_ATOM_DAYS",
    "DEFAULT_JOURNAL_PIPELINE_DAYS",
    "DEFAULT_MAX_ROWS_PER_PASS",
    "DEFAULT_ORPHAN_RAW_DAYS",
    "DEFAULT_REJECTED_CANDIDATE_DAYS",
    "PIPELINE_JOURNAL_ACTIONS",
    "GcStats",
    "run_gc",
]
