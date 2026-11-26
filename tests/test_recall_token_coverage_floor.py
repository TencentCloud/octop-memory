"""Tests for the recall token-coverage relevance floor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from octop_memory.application.config import DEFAULT_RECALL_CFG
from octop_memory.core import Memory
from octop_memory.pipeline.recall import recall_for_prompt
from octop_memory.pipeline.recall.cache import RecallCache
from octop_memory.storage.backends.sqlite import SqliteMemoryBackend
from octop_memory.types import AtomCard, Entity, RawEvent

_NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def overlapping(tmp_path: Path) -> Memory:
    """Two atoms that both match a shared word but differ in the entity word."""
    backend = SqliteMemoryBackend(namespace="v2", db_path=tmp_path / "v2.sqlite")
    mem = Memory(namespace="v2", backend=backend)
    seed = [
        ("Memory seed for the floor tests", "ent-hermes", "Hermes checklist owner is alice"),
        ("Memory seed for the floor tests", "ent-openclaw", "OpenClaw checklist owner is bob"),
    ]
    for idx, (search, entity_id, assertion) in enumerate(seed):
        when = _NOW - timedelta(days=idx)
        mem.add_entity(
            Entity(
                id=entity_id,
                entity_type="Project",
                canonical_name=entity_id,
                aliases=[],
                atom_count=0,
                created_at=when,
            )
        )
        raw_id = str(uuid.uuid4())
        mem.add_raw_batch(
            [
                RawEvent(
                    id=raw_id,
                    host="t",
                    session_id="s",
                    thread_id=None,
                    user=None,
                    timestamp=when,
                    event_type="user_message",
                    content=assertion,
                    payload={},
                )
            ]
        )
        mem.add_atom(
            AtomCard(
                id=str(uuid.uuid4()),
                entity_id=entity_id,
                candidate_id=str(uuid.uuid4()),
                raw_event_ids=[raw_id],
                assertion=assertion,
                verbatim_quote=assertion,
                quote_event_id=raw_id,
                search_terms=search.split(),
                occurred_at=when,
                confidence="high",
                importance="high",
                created_at=when,
            )
        )
    return mem


def test_floor_defaults_to_off() -> None:
    assert DEFAULT_RECALL_CFG["min_token_coverage"] == 0.0


def test_floor_zero_keeps_historical_behaviour(overlapping: Memory) -> None:
    result = recall_for_prompt(overlapping, "OpenClaw checklist", limit=5, now=_NOW)
    texts = [s.text for s in result.snippets]
    assert texts
    assert any("Hermes" in t for t in texts), "candidate pool should include the low-coverage hit"


def test_floor_drops_low_coverage_hits(overlapping: Memory) -> None:
    loose = recall_for_prompt(overlapping, "OpenClaw checklist", limit=5, now=_NOW)
    strict = recall_for_prompt(
        overlapping,
        "OpenClaw checklist",
        limit=5,
        now=_NOW,
        min_token_coverage=0.75,
    )
    assert len(strict.snippets) < len(loose.snippets)
    assert strict.snippets, "the on-topic hit must survive the floor"
    assert all("OpenClaw" in s.text for s in strict.snippets)


def test_floor_does_not_blank_untokenizable_query(overlapping: Memory) -> None:
    # Punctuation-only query carries no tokens; the floor must not decide.
    result = recall_for_prompt(overlapping, "？？", limit=5, now=_NOW, min_token_coverage=0.9)
    assert isinstance(result.snippets, list)


@pytest.mark.parametrize(("first", "second"), [(0.0, 0.75), (0.75, 0.0), (0.75, 0.5)])
def test_cached_recall_uses_the_requested_floor(overlapping: Memory, first: float, second: float) -> None:
    cache = RecallCache()

    def recall(floor: float, reuse: RecallCache) -> list[str]:
        result = recall_for_prompt(
            overlapping, "OpenClaw checklist", thread_id="thread", now=_NOW, cache=reuse, min_token_coverage=floor
        )
        return [snippet.source_id for snippet in result.snippets]

    initial = recall(first, cache)
    fresh = recall(second, RecallCache())
    assert initial != fresh
    assert recall(second, cache) == fresh
    assert recall(first, cache) == initial
