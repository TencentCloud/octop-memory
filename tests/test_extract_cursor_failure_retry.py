"""Failure and retry semantics for the incremental extracted-id cursor (#25 review).

These lock in the transactional guarantee added during the #25 convergence:
candidate writes and the cursor write must commit in ONE transaction, so a
failed candidate INSERT or a failed (strict) cursor write rolls back the
whole batch. The events then stay un-extracted and a later ``extract`` call
retries them instead of being silently skipped.

This is the formal home of the failure/retry assertions the reviewer ran by
hand on temp SQLite / PostgreSQL 16 (first/second candidate INSERT failure,
cursor write failure, same-process retry, reopen-after-close retry, and
no-duplicate-after-normal-commit).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from octop_memory import Memory
from octop_memory.application.runtime import MemoryRuntime
from octop_memory.ports.llm import MockLLMClient
from octop_memory.storage.backends.sqlite import SqliteMemoryBackend

PG_DSN = os.environ.get("TEST_POSTGRES_DSN", "postgresql://localhost/octop_memory_test")
_CURSOR_PREFIX = "extracted_ids/v1/"


def _runtime(memory: Memory, llm: MockLLMClient) -> MemoryRuntime:
    return MemoryRuntime(
        memory,
        llm=llm,
        config={
            "capture": {"min_message_chars": 0},
            "extraction": {"promote": False, "regen_pages": False},
        },
    )


def _seed(memory: Memory, n: int, session_id: str = "s1") -> list[str]:
    ids: list[str] = []
    for i in range(n):
        ev = memory.add_raw(
            content=f"fact number {i}",
            event_type="user_message",
            host="manual",
            session_id=session_id,
        )
        ids.append(ev.id)
    return ids


def _candidate(quote_event_id: str, source_refs: list[str], title: str) -> dict:
    return {
        "candidate_id": "",
        "candidate_type": "Decision",
        "status": "pending",
        "title": title,
        "assertion": title,
        "verbatim_quote": title,
        "quote_event_id": quote_event_id,
        "subject": {"name": "Project X", "entity_type": "Project", "entity_id_hint": ""},
        "target_entities": [],
        "source_refs": source_refs,
        "confidence": "high",
        "importance": "high",
        "recommended_action": "promote",
        "promotion_reason": "explicit",
    }


def _payload(candidates: list[dict]) -> str:
    # Include a top-level "episodes" array so the episode extractor (which runs
    # on the same mock response on the success path) never raises a parse error.
    return json.dumps({"candidates": candidates, "episodes": []}, ensure_ascii=False)


def _one_candidate(quote_event_id: str, source_refs: list[str]) -> str:
    return _payload([_candidate(quote_event_id, source_refs, "mode decision")])


def _two_candidates(quote_event_id: str, source_refs: list[str]) -> str:
    return _payload(
        [
            _candidate(quote_event_id, source_refs, "mode decision"),
            _candidate(quote_event_id, source_refs, "deadline decision"),
        ]
    )


@pytest.fixture(params=["sqlite", "postgres"])
def memory(request: pytest.FixtureRequest, tmp_path: Path) -> Memory:
    """Each backend in turn; postgres skips when no server answers."""
    if request.param == "sqlite":
        be = SqliteMemoryBackend(namespace="curfail", db_path=tmp_path / "curfail.sqlite")
        yield Memory(namespace="curfail", backend=be)
        be.close()
        return
    request.getfixturevalue("postgres_dsn")
    from octop_memory.storage.backends.postgres import PostgresMemoryBackend

    ns = f"curfail_{uuid4().hex[:12]}"
    be = PostgresMemoryBackend(namespace=ns, dsn=PG_DSN)
    try:
        yield Memory(namespace=ns, backend=be)
    finally:
        if not be._closed:
            be.purge_namespace()
        be.close()


@pytest.fixture
def memory_sqlite(tmp_path: Path) -> Memory:
    be = SqliteMemoryBackend(namespace="curfail2", db_path=tmp_path / "curfail2.sqlite")
    yield Memory(namespace="curfail2", backend=be)
    be.close()


def _reopen(memory: Memory) -> Memory:
    be = memory._backend
    if isinstance(be, SqliteMemoryBackend):
        db_path = be._db_path
        ns = be._ns
        be.close()
        return Memory(namespace=ns, backend=SqliteMemoryBackend(namespace=ns, db_path=db_path))
    from octop_memory.storage.backends.postgres import PostgresMemoryBackend

    ns = be._ns
    dsn = be._dsn
    be.close()
    return Memory(namespace=ns, backend=PostgresMemoryBackend(namespace=ns, dsn=dsn))


# ---------------------------------------------------------------------------
# 1. First candidate INSERT fails -> whole batch rolls back, retry recovers.
# ---------------------------------------------------------------------------
def test_first_candidate_insert_failure_rolls_back_and_retries(memory: Memory) -> None:
    ids = _seed(memory, 3)
    llm = MockLLMClient(default_response=_one_candidate(ids[0], ids))
    rt = _runtime(memory, llm)

    orig_add = memory.add_candidates

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("simulated first candidate INSERT failure")

    memory.add_candidates = boom
    first = rt.extract({"session_id": "s1"})
    assert first["failure_reason"] is not None
    assert first["events_extracted"] == 0
    assert first["candidates"] == 0
    # Nothing persisted: the failed batch must not leave partial candidates.
    assert memory.list_candidates(session_id="s1") == []

    # Same-process retry after the transient failure clears.
    memory.add_candidates = orig_add
    second = rt.extract({"session_id": "s1"})
    assert second["failure_reason"] is None
    assert second["events_extracted"] == 3
    assert second["candidates"] == 1
    # The events were extracted exactly once (no duplicate candidate).
    assert len(memory.list_candidates(session_id="s1")) == 1


# ---------------------------------------------------------------------------
# 2. Second candidate INSERT fails -> the already-written first row must also
#    roll back (transaction, not best-effort per row).
# ---------------------------------------------------------------------------
def test_second_candidate_insert_failure_rolls_back(memory: Memory) -> None:
    ids = _seed(memory, 3)
    llm = MockLLMClient(default_response=_two_candidates(ids[0], ids))
    rt = _runtime(memory, llm)

    calls = {"n": 0}
    orig_save = memory._backend.save_candidate

    def flaky_save(c):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("simulated second candidate INSERT failure")
        return orig_save(c)

    memory._backend.save_candidate = flaky_save
    first = rt.extract({"session_id": "s1"})
    assert first["failure_reason"] is not None
    # The first candidate's write is undone by the rollback too.
    assert memory.list_candidates(session_id="s1") == []

    memory._backend.save_candidate = orig_save
    second = rt.extract({"session_id": "s1"})
    assert second["failure_reason"] is None
    assert second["candidates"] == 2
    assert len(memory.list_candidates(session_id="s1")) == 2


# ---------------------------------------------------------------------------
# 3. Strict cursor write failure rolls back the candidate writes as well (#25 P1).
# ---------------------------------------------------------------------------
def test_cursor_write_failure_rolls_back_candidates(memory: Memory) -> None:
    ids = _seed(memory, 3)
    llm = MockLLMClient(default_response=_one_candidate(ids[0], ids))
    rt = _runtime(memory, llm)

    orig_set = memory.set_meta

    def flaky_set(key: str, value: str) -> None:
        if key.startswith(_CURSOR_PREFIX):
            raise sqlite3.OperationalError("simulated cursor write failure")
        return orig_set(key, value)

    memory.set_meta = flaky_set
    first = rt.extract({"session_id": "s1"})
    assert first["failure_reason"] is not None
    # The strict cursor write must roll back the candidate writes too, so the
    # events are NOT silently marked extracted.
    assert memory.list_candidates(session_id="s1") == []

    memory.set_meta = orig_set
    second = rt.extract({"session_id": "s1"})
    assert second["failure_reason"] is None
    assert second["candidates"] == 1
    assert len(memory.list_candidates(session_id="s1")) == 1


# ---------------------------------------------------------------------------
# 4. Normal commit then reopen: cursor in meta resumes, no re-extraction.
# ---------------------------------------------------------------------------
def test_normal_commit_then_reopen_does_not_duplicate(memory_sqlite: Memory) -> None:
    ids = _seed(memory_sqlite, 3)
    llm = MockLLMClient(default_response=_one_candidate(ids[0], ids))
    rt = _runtime(memory_sqlite, llm)

    first = rt.extract({"session_id": "s1"})
    assert first["failure_reason"] is None
    assert first["events_extracted"] == 3

    reopened = _reopen(memory_sqlite)
    rt2 = _runtime(reopened, llm)
    second = rt2.extract({"session_id": "s1"})
    # The persisted cursor resumes the session; nothing is re-extracted.
    assert second["events_extracted"] == 0
    assert second["candidates"] == 0


# ---------------------------------------------------------------------------
# 5. Failed extract then reopen: the events are retried, not skipped forever.
# ---------------------------------------------------------------------------
def test_failure_then_reopen_retries(memory_sqlite: Memory) -> None:
    ids = _seed(memory_sqlite, 3)
    llm = MockLLMClient(default_response=_one_candidate(ids[0], ids))
    rt = _runtime(memory_sqlite, llm)

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("simulated failure before commit")

    memory_sqlite.add_candidates = boom
    first = rt.extract({"session_id": "s1"})
    assert first["failure_reason"] is not None

    # A fresh runtime over the same store (process restart) must recover.
    reopened = _reopen(memory_sqlite)
    rt2 = _runtime(reopened, llm)
    second = rt2.extract({"session_id": "s1"})
    assert second["failure_reason"] is None
    assert second["events_extracted"] == 3
    assert len(reopened.list_candidates(session_id="s1")) == 1
