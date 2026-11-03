"""Tests for Memory core class."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from octop_memory.core import Memory


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    return Memory(namespace="test", backend_config={"db_path": str(tmp_path / "test.sqlite")})


class TestStore:
    def test_store_creates_leaf_node(self, memory: Memory) -> None:
        node = memory.store("User prefers Python over Java")
        assert node.level == "leaf"
        assert "Python" in node.content

    def test_store_with_topic(self, memory: Memory) -> None:
        node = memory.store("Likes TDD", topic="preferences")
        assert node.topic == "preferences"


class TestRecall:
    def test_recall_finds_stored_memory(self, memory: Memory) -> None:
        memory.store("User works on octop-harness project")
        memory.store("User likes sushi for lunch")
        results = memory.recall("octop-harness")
        assert len(results) >= 1
        assert any("octop-harness" in r.content for r in results)

    def test_recall_empty_returns_empty(self, memory: Memory) -> None:
        results = memory.recall("nonexistent topic")
        assert results == []


class TestGetTree:
    def test_get_tree_initially_empty(self, memory: Memory) -> None:
        assert memory.get_tree() == []

    def test_get_tree_after_store(self, memory: Memory) -> None:
        memory.store("Some fact")
        tree = memory.get_tree()
        assert len(tree) >= 1


class TestLegacyPageBackendCompatibility:
    @pytest.mark.parametrize("expected_version", [None, 0])
    def test_regen_preserves_legacy_calls_but_never_drops_a_guard(
        self, memory: Memory, monkeypatch: pytest.MonkeyPatch, expected_version: int | None
    ) -> None:
        calls: list[str] = []

        def legacy_apply(
            entity_id: str, *, summary_markdown: str, headline: str, topics: list[str], when: datetime
        ) -> bool:
            calls.append(summary_markdown)
            return True

        monkeypatch.setattr(memory.backend, "apply_entity_page_regen", legacy_apply)
        if expected_version is None:
            assert memory.apply_entity_page_regen("entity", summary_markdown="body", headline="head", topics=[])
            assert calls == ["body"]
        else:
            with pytest.raises(TypeError, match="expected_mutation_version"):
                memory.apply_entity_page_regen(
                    "entity",
                    summary_markdown="body",
                    headline="head",
                    topics=[],
                    expected_mutation_version=expected_version,
                )
            assert calls == []

    @pytest.mark.parametrize("expected_version", [None, 0])
    def test_failure_preserves_legacy_calls_but_never_drops_a_guard(
        self, memory: Memory, monkeypatch: pytest.MonkeyPatch, expected_version: int | None
    ) -> None:
        calls: list[str] = []

        def legacy_failure(entity_id: str, *, when: datetime) -> bool:
            calls.append(entity_id)
            return True

        monkeypatch.setattr(memory.backend, "record_entity_page_regen_failure", legacy_failure)
        if expected_version is None:
            assert memory.record_entity_page_regen_failure("entity")
            assert calls == ["entity"]
        else:
            with pytest.raises(TypeError, match="expected_mutation_version"):
                memory.record_entity_page_regen_failure("entity", expected_mutation_version=expected_version)
            assert calls == []
