"""Tests for the M2.5b default LLM escalation hook implementation.

We use a deterministic ``MockLLMClient`` so no network is required.
The hook's contract is: never raise, always return ``True / False / str / None``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from octop_memory.core import Memory
from octop_memory.pipeline.promotion.llm_hook import ModelEscalationHook
from octop_memory.ports.llm import MockLLMClient
from octop_memory.ports.llm._protocol import LLMClientError
from octop_memory.storage.backends.sqlite import SqliteMemoryBackend

# ---------------------------------------------------------------------------
# resolve_entity_match
# ---------------------------------------------------------------------------


class TestResolveEntityMatch:
    def test_picks_listed_id_on_clear_match(self) -> None:
        llm = MockLLMClient(default_response='{"entity_id": "ent-2"}')
        hook = ModelEscalationHook(llm)
        picked = hook.resolve_entity_match(
            candidate_subject="Project database",
            candidate_entity_type="Project",
            candidates=[("ent-1", "Apollo navigation"), ("ent-2", "Project")],
        )
        assert picked == "ent-2"

    def test_returns_none_when_llm_says_null(self) -> None:
        llm = MockLLMClient(default_response='{"entity_id": null}')
        hook = ModelEscalationHook(llm)
        picked = hook.resolve_entity_match(
            candidate_subject="Apollo cms",
            candidate_entity_type="Project",
            candidates=[("ent-1", "Apollo navigation")],
        )
        assert picked is None

    def test_rejects_hallucinated_id_not_in_input(self) -> None:
        llm = MockLLMClient(default_response='{"entity_id": "fake-id-zzz"}')
        hook = ModelEscalationHook(llm)
        picked = hook.resolve_entity_match(
            candidate_subject="X",
            candidate_entity_type="Project",
            candidates=[("ent-1", "X-original")],
        )
        assert picked is None  # hallucinated id silently dropped

    def test_empty_candidates_short_circuits_no_llm_call(self) -> None:
        llm = MockLLMClient(default_response='{"entity_id": "ent-1"}')
        hook = ModelEscalationHook(llm)
        picked = hook.resolve_entity_match(
            candidate_subject="X",
            candidate_entity_type="Project",
            candidates=[],
        )
        assert picked is None

    def test_returns_none_on_llm_client_error(self) -> None:
        class _Boom:
            def call_llm(self, *a, **kw) -> str:
                raise LLMClientError("network down")

        hook = ModelEscalationHook(_Boom())  # type: ignore[arg-type]
        picked = hook.resolve_entity_match(
            candidate_subject="X",
            candidate_entity_type="Project",
            candidates=[("ent-1", "X")],
        )
        assert picked is None  # never raises

    def test_returns_none_on_unparseable_json(self) -> None:
        llm = MockLLMClient(default_response="not json")
        hook = ModelEscalationHook(llm)
        picked = hook.resolve_entity_match(
            candidate_subject="X",
            candidate_entity_type="Project",
            candidates=[("ent-1", "X")],
        )
        assert picked is None

    def test_strips_markdown_fences(self) -> None:
        # Some hosts wrap JSON in fences even when asked for json mode.
        llm = MockLLMClient(default_response='```json\n{"entity_id": "ent-1"}\n```')
        hook = ModelEscalationHook(llm)
        picked = hook.resolve_entity_match(
            candidate_subject="X",
            candidate_entity_type="Project",
            candidates=[("ent-1", "X")],
        )
        assert picked == "ent-1"


# ---------------------------------------------------------------------------
# same_assertion / is_contradiction
# ---------------------------------------------------------------------------


class TestSameAssertion:
    def test_returns_true(self) -> None:
        llm = MockLLMClient(default_response='{"same": true}')
        hook = ModelEscalationHook(llm)
        assert (
            hook.same_assertion(
                candidate_assertion="项目用 PostgreSQL 16",
                existing_assertion="项目数据库选择 PostgreSQL 16",
            )
            is True
        )

    def test_returns_false(self) -> None:
        llm = MockLLMClient(default_response='{"same": false}')
        hook = ModelEscalationHook(llm)
        assert (
            hook.same_assertion(
                candidate_assertion="项目用 PostgreSQL",
                existing_assertion="今天天气真好",
            )
            is False
        )

    def test_returns_none_on_uncertain(self) -> None:
        llm = MockLLMClient(default_response='{"same": null}')
        hook = ModelEscalationHook(llm)
        assert hook.same_assertion(candidate_assertion="x", existing_assertion="y") is None

    def test_invalid_value_returns_none(self) -> None:
        llm = MockLLMClient(default_response='{"same": "yes"}')  # not a bool
        hook = ModelEscalationHook(llm)
        assert hook.same_assertion(candidate_assertion="x", existing_assertion="y") is None


class TestIsContradiction:
    def test_returns_true(self) -> None:
        llm = MockLLMClient(default_response='{"contradiction": true}')
        hook = ModelEscalationHook(llm)
        assert (
            hook.is_contradiction(
                candidate_assertion="项目不用 PostgreSQL",
                existing_assertion="项目用 PostgreSQL",
            )
            is True
        )

    def test_returns_false(self) -> None:
        llm = MockLLMClient(default_response='{"contradiction": false}')
        hook = ModelEscalationHook(llm)
        assert hook.is_contradiction(candidate_assertion="x", existing_assertion="y") is False


# ---------------------------------------------------------------------------
# Integration: hook plugged into PromotionWorker fixes the dogfood case
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    backend = SqliteMemoryBackend(namespace="m25b", db_path=tmp_path / "m25b.sqlite")
    return Memory(namespace="m25b", backend=backend)


def _make_candidate(
    cid: str,
    *,
    subject_name: str,
    raw_event_id: str,
    assertion: str = "我们项目用 PostgreSQL 16",
) -> object:
    from octop_memory.types import Candidate

    return Candidate(
        id=cid,
        raw_event_ids=[raw_event_id],
        candidate_type="Fact",
        status="pending",
        title="t",
        assertion=assertion,
        verbatim_quote=assertion,
        quote_event_id=raw_event_id,
        subject_name=subject_name,
        subject_entity_type="Project",
        target_entity_id=None,
        confidence="high",
        importance="high",
        recommended_action="promote",
        promotion_reason="",
        extractor_version="v2.1",
        created_at=_now(),
    )


def test_model_suggestion_requires_explicit_approval(memory: Memory) -> None:
    """A shared name prefix alone cannot move a fact or merge its entities."""
    from octop_memory.pipeline.promotion import PromotionWorker

    ev = memory.add_raw(content="raw", event_type="user_message")

    # First candidate: subject = "Project" — creates entity "Project"
    c1 = _make_candidate("c1", subject_name="Project", raw_event_id=ev.id)
    memory.add_candidate(c1)  # type: ignore[arg-type]

    # The model proposes linking this longer name to the first project.
    # The alias becomes authoritative only after the reviewer confirms it.
    c2 = _make_candidate(
        "c2",
        subject_name="Project database",
        raw_event_id=ev.id,
        assertion="项目数据库版本是 16",
    )
    memory.add_candidate(c2)  # type: ignore[arg-type]

    # First, run worker WITHOUT hook to seed the first entity.
    worker_no_hook = PromotionWorker(memory)
    worker_no_hook.promote([c1])  # type: ignore[arg-type]

    # The single existing entity (Project) is the only candidate. The
    # mock returns its id by name lookup for determinism.
    project_entity = memory.find_entity_by_name("Project", entity_type="Project")
    assert project_entity is not None
    seeded_id = project_entity.id

    # Now hook says: yes, "Project database" is the same as Project.
    llm = MockLLMClient(default_response=f'{{"entity_id": "{seeded_id}"}}')
    from octop_memory.pipeline.promotion.llm_hook import ModelEscalationHook

    hook = ModelEscalationHook(llm)
    worker_with_hook = PromotionWorker(memory, llm_hook=hook)
    result = worker_with_hook.promote([c2])  # type: ignore[arg-type]

    assert result.needs_review == 1
    assert result.llm_calls == 1
    pending = memory.get_candidate("c2")
    assert pending is not None and pending.target_entity_id == seeded_id
    assert len(memory.list_atoms(entity_id=seeded_id)) == 1
    approved = worker_no_hook.approve(pending)
    assert approved.atom is not None and approved.atom.entity_id == seeded_id
    assert any(
        a.alias == "project database" and a.created_by == "user" for a in memory.list_aliases(entity_id=seeded_id)
    )
    assert len(memory.list_entities(entity_type="Project")) == 1


def test_hook_uncertain_falls_through_to_new_entity(memory: Memory) -> None:
    """Hook returns None → rule path keeps its tentative answer (new entity)."""
    from octop_memory.pipeline.promotion import PromotionWorker

    ev = memory.add_raw(content="raw", event_type="user_message")
    c1 = _make_candidate("c1", subject_name="Apollo navigation", raw_event_id=ev.id)
    c2 = _make_candidate("c2", subject_name="Apollo cms", raw_event_id=ev.id)
    memory.add_candidate(c1)  # type: ignore[arg-type]
    memory.add_candidate(c2)  # type: ignore[arg-type]

    PromotionWorker(memory).promote([c1])  # type: ignore[arg-type]

    llm = MockLLMClient(default_response='{"entity_id": null}')
    hook = ModelEscalationHook(llm)
    result = PromotionWorker(memory, llm_hook=hook).promote([c2])  # type: ignore[arg-type]

    # Two distinct entities expected.
    assert result.promoted == 1
    entities = memory.list_entities(entity_type="Project")
    assert len(entities) == 2


def test_hook_failure_does_not_block_promotion(memory: Memory) -> None:
    """LLMClientError → hook returns None → worker continues with new entity."""
    from octop_memory.pipeline.promotion import PromotionWorker

    class _Boom:
        def call_llm(self, *a, **kw) -> str:
            raise LLMClientError("network down")

    ev = memory.add_raw(content="raw", event_type="user_message")
    c1 = _make_candidate("c1", subject_name="A", raw_event_id=ev.id)
    c2 = _make_candidate("c2", subject_name="B", raw_event_id=ev.id)
    memory.add_candidate(c1)  # type: ignore[arg-type]
    memory.add_candidate(c2)  # type: ignore[arg-type]

    PromotionWorker(memory).promote([c1])  # type: ignore[arg-type]

    hook = ModelEscalationHook(_Boom())  # type: ignore[arg-type]
    result = PromotionWorker(memory, llm_hook=hook).promote([c2])  # type: ignore[arg-type]
    # Despite LLM error, c2 still got promoted (with a new entity).
    assert result.promoted == 1
    assert result.llm_calls == 0
