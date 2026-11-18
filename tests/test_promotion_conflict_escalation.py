"""Check-5 (conflict) semantic confirmation + chronological-update handling.

``check_conflict`` used to be rule-only: "≥3 shared content tokens" plus
"opposite negation polarity" was enough to park a candidate as a conflict.
For Chinese that prefilter is far too loose — the negation tokens 不/没/未
appear inside ordinary words (不当值 / 未配置 / 不用) and any two assertions
that merely name the same entity share ≥3 characters — so real facts were
parked en masse. It also had no way to express "the value changed", which is
an update (newer replaces older), not a conflict.

These tests pin the behaviour added on top of the rule prefilter:

* the rule stays the *prefilter*; the host hook decides;
* ``unrelated`` releases the candidate, ``update`` promotes-and-supersedes,
  ``contradiction`` / ``None`` still park it (fail closed);
* without a usable hook the old rule-only behaviour is byte-for-byte intact;
* the tokenizer no longer glues ASCII tokens to CJK punctuation.

No network: the hook is exercised through ``MockLLMClient``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from octop_memory.core import Memory
from octop_memory.pipeline.promotion import PromotionWorker
from octop_memory.pipeline.promotion.checks import (
    _MAX_CONTRADICTION_CHECKS,
    _shares_meaningful_overlap,
    check_conflict,
)
from octop_memory.pipeline.promotion.llm_hook import ModelEscalationHook
from octop_memory.ports.llm import MockLLMClient
from octop_memory.ports.llm._protocol import LLMClientError
from octop_memory.storage.backends.sqlite import SqliteMemoryBackend
from octop_memory.types import AtomCard, Candidate, RawEvent


def _now() -> datetime:
    return datetime.now(UTC)


def _atom(assertion: str, *, aid: str = "atom-1", entity_id: str = "ent-1") -> AtomCard:
    return AtomCard(
        id=aid,
        entity_id=entity_id,
        candidate_id="c0",
        raw_event_ids=["raw-1"],
        assertion=assertion,
        verbatim_quote=assertion,
        quote_event_id="raw-1",
        search_terms=[],
        occurred_at=_now(),
        confidence="high",
        importance="high",
        created_at=_now(),
    )


def _candidate(assertion: str, *, cid: str = "c1", subject: str = "项目") -> Candidate:
    return Candidate(
        id=cid,
        raw_event_ids=["raw-1"],
        candidate_type="Fact",
        status="pending",
        title=assertion[:40],
        assertion=assertion,
        verbatim_quote=assertion,
        quote_event_id="raw-1",
        subject_name=subject,
        subject_entity_type="Project",
        target_entity_id=None,
        confidence="high",
        importance="high",
        recommended_action="promote",
        promotion_reason="",
        extractor_version="test",
        created_at=_now(),
        decided_at=None,
        decided_by=None,
        session_id="s1",
        payload={},
    )


@dataclass
class _FakeAtom:
    id: str
    assertion: str


class _FakeLookup:
    """Minimal ``AtomLookup``: same entity, fixed atom list."""

    def __init__(self, *assertions: str) -> None:
        self._atoms = [_FakeAtom(f"atom-{i:03d}", a) for i, a in enumerate(assertions)]

    def list_atoms(self, *, entity_id: str, include_deprecated: bool, limit: int) -> list[_FakeAtom]:
        return list(self._atoms)


class _Hook:
    """Configurable escalation hook; can be built missing either method."""

    def __init__(
        self,
        *,
        relation: str | None = None,
        two_way: bool | None = None,
        raises: bool = False,
        with_classify: bool = True,
        with_two_way: bool = True,
    ) -> None:
        self.relation, self.two_way, self.raises = relation, two_way, raises
        self.calls = 0
        if not with_classify:
            object.__setattr__(self, "classify_pair", None)
        if not with_two_way:
            object.__setattr__(self, "is_contradiction", None)

    # Stubs so the fake satisfies the ``LLMEscalationHook`` protocol; check 5
    # never calls these.
    def resolve_entity_match(
        self, *, candidate_subject: str, candidate_entity_type: str, candidates: list[tuple[str, str]]
    ) -> str | None:
        return None

    def same_assertion(self, *, candidate_assertion: str, existing_assertion: str) -> bool | None:
        return None

    def classify_pair(self, *, candidate_assertion: str, existing_assertion: str) -> str | None:
        self.calls += 1
        if self.raises:
            raise RuntimeError("hook exploded")
        return self.relation

    def is_contradiction(self, *, candidate_assertion: str, existing_assertion: str) -> bool | None:
        self.calls += 1
        if self.raises:
            raise RuntimeError("hook exploded")
        return self.two_way


# ---------------------------------------------------------------------------
# Rule prefilter + hook routing
# ---------------------------------------------------------------------------


class TestConflictEscalation:
    NEW = "项目不用 PostgreSQL"
    OLD = "项目用 PostgreSQL"  # opposite polarity, ample overlap → suspect

    def test_no_hook_keeps_rule_only_behaviour(self) -> None:
        out = check_conflict(_candidate(self.NEW), entity_id="ent-1", atom_lookup=_FakeLookup(self.OLD))
        assert out is not None and out.kind == "conflict"
        assert out.matched_atom_id == "atom-000"

    def test_hook_without_conflict_methods_falls_back_to_rule(self) -> None:
        hook = _Hook(with_classify=False, with_two_way=False)
        out = check_conflict(
            _candidate(self.NEW), entity_id="ent-1", atom_lookup=_FakeLookup(self.OLD), llm_hook=hook
        )
        assert out is not None and out.kind == "conflict"

    def test_hook_exception_falls_back_to_rule(self) -> None:
        hook = _Hook(raises=True)
        out = check_conflict(
            _candidate(self.NEW), entity_id="ent-1", atom_lookup=_FakeLookup(self.OLD), llm_hook=hook
        )
        assert out is not None and out.kind == "conflict"

    def test_unrelated_releases_candidate(self) -> None:
        hook = _Hook(relation="unrelated")
        out = check_conflict(
            _candidate(self.NEW), entity_id="ent-1", atom_lookup=_FakeLookup(self.OLD), llm_hook=hook
        )
        assert out is None

    def test_update_returns_promote_with_supersede(self) -> None:
        hook = _Hook(relation="update")
        out = check_conflict(
            _candidate(self.NEW), entity_id="ent-1", atom_lookup=_FakeLookup(self.OLD), llm_hook=hook
        )
        assert out is not None
        assert out.kind == "promote"
        assert out.supersede_atom_id == "atom-000"
        assert out.matched_atom_id == "atom-000"

    def test_contradiction_parks_candidate(self) -> None:
        hook = _Hook(relation="contradiction")
        out = check_conflict(
            _candidate(self.NEW), entity_id="ent-1", atom_lookup=_FakeLookup(self.OLD), llm_hook=hook
        )
        assert out is not None and out.kind == "conflict"

    def test_uncertain_verdict_parks_candidate(self) -> None:
        """``None`` means "cannot decide" — fail closed, never silently release."""
        hook = _Hook(relation=None)
        out = check_conflict(
            _candidate(self.NEW), entity_id="ent-1", atom_lookup=_FakeLookup(self.OLD), llm_hook=hook
        )
        assert out is not None and out.kind == "conflict"

    def test_two_way_hook_still_supported(self) -> None:
        released = check_conflict(
            _candidate(self.NEW),
            entity_id="ent-1",
            atom_lookup=_FakeLookup(self.OLD),
            llm_hook=_Hook(with_classify=False, two_way=False),
        )
        parked = check_conflict(
            _candidate(self.NEW),
            entity_id="ent-1",
            atom_lookup=_FakeLookup(self.OLD),
            llm_hook=_Hook(with_classify=False, two_way=True),
        )
        assert released is None
        assert parked is not None and parked.kind == "conflict"


class TestConfirmationBudget:
    def test_calls_are_bounded_and_capped_suspects_fail_closed(self) -> None:
        hook = _Hook(relation="unrelated")
        lookup = _FakeLookup(*["项目用 PostgreSQL"] * 4)  # 4 suspects, all "unrelated"
        calls: list[int] = []
        out = check_conflict(_candidate("项目不用 PostgreSQL"), entity_id="ent-1", atom_lookup=lookup, llm_hook=hook, calls=calls)
        assert hook.calls == _MAX_CONTRADICTION_CHECKS  # never unbounded
        assert out is not None and out.kind == "conflict"  # remaining suspect parked
        assert calls == [_MAX_CONTRADICTION_CHECKS]

    def test_calls_out_param_reports_zero_without_suspects(self) -> None:
        calls: list[int] = []
        out = check_conflict(
            _candidate("完全无关的一句话"),
            entity_id="ent-1",
            atom_lookup=_FakeLookup("项目用 PostgreSQL"),
            llm_hook=_Hook(relation="update"),
            calls=calls,
        )
        assert out is None
        assert calls == [0]

    def test_conflict_outcome_carries_call_count(self) -> None:
        hook = _Hook(relation="contradiction")
        out = check_conflict(
            _candidate("项目不用 PostgreSQL"),
            entity_id="ent-1",
            atom_lookup=_FakeLookup("项目用 PostgreSQL"),
            llm_hook=hook,
        )
        assert out is not None and out.llm_calls == 1


class TestOverlapPrefilter:
    def test_ascii_token_is_not_glued_to_following_cjk(self) -> None:
        """``"8791,改用 8790"`` must not collapse into one token.

        ``normalize_alias`` folds full-width punctuation to ASCII, so the old
        whitespace split kept ``8791,改用`` as a single token and the genuine
        update pair never reached the prefilter at all.
        """
        assert _shares_meaningful_overlap("端口不用 8791，改用 8790", "网关端口用 8791")

    def test_cjk_punctuation_only_still_overlaps(self) -> None:
        assert _shares_meaningful_overlap("项目用 PostgreSQL 更快", "项目用 PostgreSQL 更稳")


# ---------------------------------------------------------------------------
# ModelEscalationHook.classify_pair
# ---------------------------------------------------------------------------


class TestClassifyPair:
    @pytest.mark.parametrize("relation", ["contradiction", "update", "unrelated"])
    def test_returns_relation(self, relation: str) -> None:
        hook = ModelEscalationHook(MockLLMClient(default_response=f'{{"relation": "{relation}"}}'))
        assert hook.classify_pair(candidate_assertion="a", existing_assertion="b") == relation

    def test_null_is_none(self) -> None:
        hook = ModelEscalationHook(MockLLMClient(default_response='{"relation": null}'))
        assert hook.classify_pair(candidate_assertion="a", existing_assertion="b") is None

    def test_unknown_value_is_none(self) -> None:
        hook = ModelEscalationHook(MockLLMClient(default_response='{"relation": "same"}'))
        assert hook.classify_pair(candidate_assertion="a", existing_assertion="b") is None

    def test_never_raises_on_llm_error(self) -> None:
        class _Boom:
            def call_llm(self, *a, **kw) -> str:
                raise LLMClientError("network down")

        hook = ModelEscalationHook(_Boom())  # type: ignore[arg-type]
        assert hook.classify_pair(candidate_assertion="a", existing_assertion="b") is None

    def test_is_contradiction_still_available_for_two_way_hosts(self) -> None:
        hook = ModelEscalationHook(MockLLMClient(default_response='{"contradiction": true}'))
        assert hook.is_contradiction(candidate_assertion="a", existing_assertion="b") is True


# ---------------------------------------------------------------------------
# Worker wiring: update → promote + supersede
# ---------------------------------------------------------------------------


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    return Memory(namespace="promo_conflict", backend=SqliteMemoryBackend(namespace="promo_conflict", db_path=tmp_path / "c.sqlite"))


def _seed_via_worker(memory: Memory, assertion: str, cid: str) -> Candidate:
    """Promote one candidate through the worker so its atom lands on the entity.

    Mirrors ``test_promotion.py``: the entity is resolved from the candidate's
    subject, so seeding through the worker (not by hand) is what makes the
    second promotion see the first atom on the same entity.
    """
    memory.add_raw(content="seed", event_type="user_message", host="test")
    event = memory.list_raw(limit=1)[0]
    cand = _candidate(assertion, cid=cid)
    cand = Candidate(**{**cand.__dict__, "raw_event_ids": [event.id], "quote_event_id": event.id})
    memory.add_candidate(cand)
    PromotionWorker(memory, llm_hook=_Hook()).promote([cand])  # no atoms yet → hook never called
    return cand


def test_update_promotes_new_atom_and_supersedes_old(memory: Memory) -> None:
    _seed_via_worker(memory, "环比重由九张卡", "c-seed")

    memory.add_raw(content="更新", event_type="user_message", host="test")
    event = memory.list_raw(limit=5)[0]
    cand = _candidate("环比重不再是九张卡，已改为十张卡", cid="c-update")
    cand = Candidate(**{**cand.__dict__, "raw_event_ids": [event.id], "quote_event_id": event.id})
    memory.add_candidate(cand)

    before = memory.list_atoms(include_deprecated=False)
    assert [a.assertion for a in before] == ["环比重由九张卡"]

    result = PromotionWorker(memory, llm_hook=_Hook(relation="update")).promote([cand])

    assert result.promoted == 1 and result.conflicts == 0
    live = memory.list_atoms(include_deprecated=False)
    assert [a.assertion for a in live] == ["环比重不再是九张卡，已改为十张卡"]
    superseded = next(a for a in memory.list_atoms(include_deprecated=True) if a.assertion == "环比重由九张卡")
    assert superseded.superseded_by == live[0].id
    assert superseded.deprecated_at is not None


def test_contradiction_still_parks_candidate(memory: Memory) -> None:
    _seed_via_worker(memory, "项目用 PostgreSQL", "c-seed")

    memory.add_raw(content="反驳", event_type="user_message", host="test")
    event = memory.list_raw(limit=5)[0]
    cand = _candidate("项目不用 PostgreSQL", cid="c-conflict")
    cand = Candidate(**{**cand.__dict__, "raw_event_ids": [event.id], "quote_event_id": event.id})
    memory.add_candidate(cand)

    result = PromotionWorker(memory, llm_hook=_Hook(relation="contradiction")).promote([cand])

    assert result.conflicts == 1 and result.promoted == 0
    loaded = memory.get_candidate(cand.id)
    assert loaded is not None and loaded.status == "conflict"
