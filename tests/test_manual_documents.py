"""Manual documents outrank ordinary memory, with auditable review candidates."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from octop_memory import Memory, MemoryService
from octop_memory.application.dashboard_data import promote_candidate
from octop_memory.pipeline.recall import recall_for_prompt


class ManualLLM:
    def __init__(self, *, broken: bool = False) -> None:
        self.calls = 0
        self.broken = broken

    def call_llm(self, prompt: str, **kwargs: Any) -> str:
        self.calls += 1
        data = json.loads(prompt)
        matches = []
        for atom in data["atoms"]:
            relation = "conflict" if atom["assertion"] == "The user prefers English replies." else "unrelated"
            matches.append(
                {
                    "atom_id": atom["id"],
                    "relation": relation,
                    "document": "USER.md",
                    "quote": "The user prefers Chinese replies." if not self.broken else "invented evidence",
                    "confidence": "high",
                    "same_subject": True,
                    "same_scope": True,
                }
            )
        return json.dumps({"matches": matches})


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    return Memory(namespace="manual_docs", backend_config={"db_path": str(tmp_path / "memory.sqlite")})


def test_conflict_is_quarantined_and_replacement_is_a_review_candidate(memory: Memory) -> None:
    memory.store("The user prefers English replies.")
    old = memory.list_atoms()[0]
    llm = ManualLLM()
    service = MemoryService(memory, llm=llm)
    docs = {"USER.md": "The user prefers Chinese replies.", "MEMORY.md": ""}
    result = service.reconcile_manual_documents(docs)
    assert result["status"] == "complete"
    assert result["conflicts"] == 1
    assert memory.get_atom(old.id).deprecated_at is not None
    assert memory.get_raw(old.quote_event_id) is not None
    candidates = memory.list_candidates(status="conflict")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.assertion == docs["USER.md"]
    assert candidate.payload["manual_document"] == "USER.md"
    assert candidate.payload["demoted_atom_id"] == old.id
    assert not recall_for_prompt(memory, "English", raw_policy="always").snippets
    again = service.reconcile_manual_documents(docs)
    assert again["conflicts"] == 1 and llm.calls == 1
    assert len(memory.list_candidates(status="conflict")) == 1
    promote_candidate(memory, {"candidate_id": candidate.id})
    assert [a.assertion for a in memory.list_atoms()] == [docs["USER.md"]]


def test_exact_duplicates_are_suppressed_only_with_the_matching_session_context(memory: Memory) -> None:
    memory.store("Aurora uses PostgreSQL.")
    service = MemoryService(memory)
    docs = {"USER.md": "", "MEMORY.md": "# Project\n- Aurora uses PostgreSQL."}
    report = service.reconcile_manual_documents(docs)
    assert report["duplicates"] == 1
    assert len(memory.list_atoms()) == 1
    assert not service.recall("PostgreSQL", manual_context=docs).snippets
    assert service.recall("PostgreSQL").snippets
    assert service.recall("PostgreSQL", manual_context={"MEMORY.md": "different snapshot"}).snippets


def test_missing_model_and_invalid_evidence_never_deprecate(memory: Memory) -> None:
    memory.store("The user prefers English replies.")
    docs = {"USER.md": "The user prefers Chinese replies."}
    result = MemoryService(memory).reconcile_manual_documents(docs)
    assert result["status"] == "unavailable"
    assert len(memory.list_atoms()) == 1
    result = MemoryService(memory, llm=ManualLLM(broken=True)).reconcile_manual_documents(docs)
    assert result["status"] == "failed"
    assert len(memory.list_atoms()) == 1
    assert memory.list_candidates(status="conflict") == []


def test_failure_rolls_back_candidate_evidence_and_deprecation(memory: Memory, monkeypatch: pytest.MonkeyPatch) -> None:
    memory.store("The user prefers English replies.")
    old = memory.list_atoms()[0]
    raw_before = {event.id for event in memory.list_raw()}

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(memory, "add_candidate", fail)
    with pytest.raises(RuntimeError, match="write failed"):
        MemoryService(memory, llm=ManualLLM()).reconcile_manual_documents(
            {"USER.md": "The user prefers Chinese replies."}
        )
    assert memory.get_atom(old.id).deprecated_at is None
    assert memory.list_candidates(status="conflict") == []
    assert {event.id for event in memory.list_raw()} == raw_before


def test_candidate_from_an_older_manual_revision_cannot_be_approved(memory: Memory) -> None:
    memory.store("The user prefers English replies.")
    service = MemoryService(memory, llm=ManualLLM())
    service.reconcile_manual_documents({"USER.md": "The user prefers Chinese replies."})
    candidate = memory.list_candidates(status="conflict")[0]
    service.reconcile_manual_documents({"USER.md": "The user prefers French replies."})
    with pytest.raises(ValueError, match="manual document"):
        promote_candidate(memory, {"candidate_id": candidate.id})


def test_save_persists_without_waiting_for_manual_review(memory: Memory) -> None:
    llm = ManualLLM()
    scheduled: list[str] = []
    service = MemoryService(
        memory, llm=llm, manual_documents_provider=lambda: {"USER.md": "The user prefers Chinese replies."}
    )
    service.set_manual_review_scheduler(lambda: scheduled.append("review"))
    node = service.save("The user prefers English replies.")
    assert scheduled == ["review"]
    assert llm.calls == 0
    assert "manual_document_review" not in node.metadata
    assert len(memory.list_atoms()) == 1
    review = service.review_new_memory()
    assert review is not None and review["conflicts"] == 1
    assert memory.list_atoms() == []


def test_save_survives_a_broken_review_scheduler(memory: Memory) -> None:
    service = MemoryService(memory)

    def boom() -> None:
        raise RuntimeError("scheduler down")

    service.set_manual_review_scheduler(boom)
    node = service.save("The user prefers English replies.")
    assert node.atom_id is not None
    assert len(memory.list_atoms()) == 1


def test_automatic_promotion_leaves_manual_review_to_the_host(memory: Memory) -> None:
    memory.store("The user prefers English replies.")
    original = memory.list_candidates()[0]
    memory.add_candidate(replace(original, id="pending-copy", status="pending", decided_by=None, decided_at=None))
    service = MemoryService(
        memory, llm=ManualLLM(), manual_documents_provider=lambda: {"USER.md": "The user prefers Chinese replies."}
    )
    result = service._runtime.promote({"regen_pages": False})
    assert "manual_documents" not in result
    assert memory.list_atoms()
    review = service.review_new_memory()
    assert review is not None and review["conflicts"] == 1
    assert memory.list_atoms() == []


@pytest.mark.parametrize("max_atoms", [7, 20])
def test_background_review_checks_one_batch(memory: Memory, max_atoms: int) -> None:
    for number in range(25):
        memory.store(f"Project {number} has a verified troubleshooting history.")
    llm = ManualLLM()
    service = MemoryService(
        memory, llm=llm, manual_documents_provider=lambda: {"USER.md": "The user prefers Chinese replies."}
    )
    report = service.review_new_memory(max_atoms=max_atoms)
    assert report is not None
    assert llm.calls == 1
    assert report["status"] == "partial"
    assert report["remaining"] == 25 - max_atoms


@pytest.mark.parametrize("documents", [None, {"USER.md": "", "MEMORY.md": ""}])
def test_background_review_without_manual_documents_is_a_noop(memory: Memory, documents: Any) -> None:
    llm = ManualLLM()
    service = MemoryService(
        memory, llm=llm, manual_documents_provider=(lambda: documents) if documents is not None else None
    )
    service.save("The user prefers English replies.")
    assert service.review_new_memory() is None
    assert llm.calls == 0
    assert len(memory.list_atoms()) == 1


def test_background_review_without_a_model_preserves_the_fact(memory: Memory) -> None:
    service = MemoryService(memory, manual_documents_provider=lambda: {"USER.md": "The user prefers Chinese replies."})
    service.save("The user prefers English replies.")
    report = service.review_new_memory()
    assert report is not None and report["status"] == "unavailable"
    assert len(memory.list_atoms()) == 1
    assert not memory.list_candidates(status="conflict")


def test_failed_comparison_is_deferred_until_later(memory: Memory) -> None:
    class Boom(ManualLLM):
        def call_llm(self, prompt: str, **kwargs: Any) -> str:
            self.calls += 1
            raise RuntimeError("model down")

    memory.store("The user prefers English replies.")
    llm = Boom()
    service = MemoryService(memory, llm=llm)
    documents = {"USER.md": "The user prefers Chinese replies."}
    first = service.reconcile_manual_documents(documents)
    assert first["status"] == "failed"
    assert llm.calls == 1
    assert len(memory.list_atoms()) == 1
    second = service.reconcile_manual_documents(documents)
    assert llm.calls == 1
    assert second["status"] == "partial"
    assert len(memory.list_atoms()) == 1


def test_repeated_comparison_failures_are_skipped_without_retiring(
    memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Boom(ManualLLM):
        def call_llm(self, prompt: str, **kwargs: Any) -> str:
            self.calls += 1
            raise RuntimeError("model down")

    monkeypatch.setattr("octop_memory.operations.retention._COMPARE_DEFER_SECONDS", -1)
    memory.store("The user prefers English replies.")
    llm = Boom()
    service = MemoryService(memory, llm=llm)
    documents = {"USER.md": "The user prefers Chinese replies."}
    result = None
    for _ in range(3):
        result = service.reconcile_manual_documents(documents)
    assert result is not None and result["status"] == "uncertain"
    assert llm.calls == 3
    assert len(memory.list_atoms()) == 1
    assert memory.list_candidates(status="conflict") == []
    again = service.reconcile_manual_documents(documents)
    assert llm.calls == 3
    assert again["remaining"] == 0


def test_concurrent_file_edit_cancels_the_audit(memory: Memory) -> None:
    memory.store("The user prefers English replies.")
    result = MemoryService(memory, llm=ManualLLM()).reconcile_manual_documents(
        {"USER.md": "The user prefers Chinese replies."}, is_current=lambda: False
    )
    assert result["status"] == "stale"
    assert len(memory.list_atoms()) == 1 and memory.list_candidates(status="conflict") == []


@pytest.mark.parametrize("mutation", ["unknown_id", "wrong_scope", "wrong_subject", "uncertain", "missing_rows"])
def test_unverified_classification_never_quarantines(memory: Memory, mutation: str) -> None:
    class Model(ManualLLM):
        def call_llm(self, prompt: str, **kwargs: Any) -> str:
            data = json.loads(super().call_llm(prompt, **kwargs))
            row = data["matches"][0]
            if mutation == "unknown_id":
                row["atom_id"] = "invented"
            elif mutation == "wrong_scope":
                row["same_scope"] = False
            elif mutation == "wrong_subject":
                row["same_subject"] = False
            elif mutation == "uncertain":
                row["confidence"] = "uncertain"
            else:
                data["matches"] = []
            return json.dumps(data)

    memory.store("The user prefers English replies.")
    result = MemoryService(memory, llm=Model()).reconcile_manual_documents(
        {"USER.md": "The user prefers Chinese replies."}
    )
    assert result["status"] in {"failed", "uncertain"}
    assert len(memory.list_atoms()) == 1
    assert not memory.list_candidates(status="conflict")


def test_audit_invalidates_previously_cached_raw_recall(memory: Memory) -> None:
    from octop_memory.pipeline.recall.cache import RecallCache

    memory.store("The user prefers English replies.")
    cache = RecallCache()
    assert recall_for_prompt(memory, "English", thread_id="t", raw_policy="always", cache=cache).snippets
    MemoryService(memory, llm=ManualLLM()).reconcile_manual_documents({"USER.md": "The user prefers Chinese replies."})
    assert not recall_for_prompt(memory, "English", thread_id="t", raw_policy="always", cache=cache).snippets


def test_recall_cache_distinguishes_context_even_before_the_first_audit(memory: Memory) -> None:
    from octop_memory.pipeline.recall.cache import RecallCache

    memory.store("Aurora uses PostgreSQL.")
    cache = RecallCache()
    assert not recall_for_prompt(
        memory, "PostgreSQL", thread_id="t", cache=cache, manual_context={"MEMORY.md": "Aurora uses PostgreSQL."}
    ).snippets
    assert recall_for_prompt(
        memory, "PostgreSQL", thread_id="t", cache=cache, manual_context={"MEMORY.md": "New session context."}
    ).snippets


def test_approval_checks_live_documents_even_before_another_audit(memory: Memory) -> None:
    memory.store("The user prefers English replies.")
    MemoryService(memory, llm=ManualLLM()).reconcile_manual_documents({"USER.md": "The user prefers Chinese replies."})
    candidate = memory.list_candidates(status="conflict")[0]
    with pytest.raises(ValueError, match="manual document changed"):
        promote_candidate(
            memory,
            {
                "candidate_id": candidate.id,
                "manual_documents": {"USER.md": "The user prefers French replies."},
            },
        )
    assert memory.list_atoms() == []


def test_partial_audit_continues_without_rechecking_finished_facts(memory: Memory) -> None:
    for number in range(101):
        memory.store(f"Project {number} has a verified troubleshooting history.")
    llm = ManualLLM()
    service = MemoryService(memory, llm=llm)
    documents = {"USER.md": "The user prefers Chinese replies."}
    first = service.reconcile_manual_documents(documents)
    assert first["status"] == "partial" and first["remaining"] == 1
    assert first["conflicts"] == 0
    second = service.reconcile_manual_documents(documents)
    assert second["status"] == "complete" and second["remaining"] == 0
    assert llm.calls == 6
    assert len(memory.list_atoms(limit=200)) == 101


def test_semantic_duplicate_is_filtered_only_for_a_matching_snapshot(memory: Memory) -> None:
    class Model(ManualLLM):
        def call_llm(self, prompt: str, **kwargs: Any) -> str:
            data = json.loads(super().call_llm(prompt, **kwargs))
            data["matches"][0]["relation"] = "duplicate"
            return json.dumps(data)

    memory.store("The user likes Chinese responses.")
    service = MemoryService(memory, llm=Model())
    documents = {"USER.md": "The user prefers Chinese replies."}
    assert service.reconcile_manual_documents(documents)["duplicates"] == 1
    assert not service.recall("Chinese", manual_context=documents).snippets
    assert service.recall("Chinese", manual_context={"USER.md": "A different profile."}).snippets


@pytest.mark.parametrize("other", ["Mode A is disabled.", "mode A is enabled.", "Mode A2 is enabled."])
def test_duplicate_normalization_preserves_negation_case_and_numbers(memory: Memory, other: str) -> None:
    memory.store("Mode A is enabled.")
    result = MemoryService(memory).reconcile_manual_documents({"MEMORY.md": other})
    assert result["duplicates"] == 0 and result["status"] == "unavailable"
    assert len(memory.list_atoms()) == 1
