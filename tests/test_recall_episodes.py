"""Tests for the explicit episode-corpus recall path (issue #18).

The router never selects the ``episode`` source, so episodes were
write-only through the documented entry points. These tests pin the
opt-in surface: ``recall_episodes()`` and ``memory_search``
``corpus="episodes"`` — while asserting default recall behaviour is
unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from octop_memory.application.config import DEFAULT_RECALL_CFG
from octop_memory.core import Memory
from octop_memory.pipeline.recall import recall_episodes, recall_for_prompt
from octop_memory.service import MemoryService
from octop_memory.types import Episode


def _make_memory(tmp_path: Path) -> Memory:
    db = tmp_path / "ep_recall.sqlite"
    return Memory(namespace="ep_recall_test", backend="sqlite", backend_config={"db_path": str(db)})


def _seed_episode(
    memory: Memory,
    *,
    episode_id: str = "ep-1",
    summary: str = "用户和老婆吵架，气炸了",
) -> Episode:
    episode = Episode(
        id=episode_id,
        raw_event_ids=["raw-1"],
        occurred_at=datetime(2026, 6, 20, 22, 0, tzinfo=UTC),
        summary=summary,
        verbatim_quote="今天和老婆吵架了",
        quote_event_id="raw-1",
        emotion="angry",
        intensity=4,
        people=["老婆"],
        topics=["家庭"],
        extractor_version="episode/v1",
        session_id=None,
        digest_ids=[],
        created_at=datetime.now(UTC),
    )
    memory.add_episodes([episode])
    return episode


class TestRecallEpisodes:
    def test_returns_episode_snippets_for_matching_query(self, tmp_path: Path) -> None:
        memory = _make_memory(tmp_path)
        _seed_episode(memory)

        snippets = recall_episodes(memory, "老婆")

        assert [s.layer for s in snippets] == ["episode"]
        assert snippets[0].source_id == "ep-1"
        assert snippets[0].role_hint == "episode"
        assert "吵架" in snippets[0].text

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        memory = _make_memory(tmp_path)
        _seed_episode(memory)

        assert recall_episodes(memory, "完全不相关的量子术语") == []

    def test_empty_query_returns_empty(self, tmp_path: Path) -> None:
        memory = _make_memory(tmp_path)
        _seed_episode(memory)

        assert recall_episodes(memory, "   ") == []

    def test_limit_caps_results(self, tmp_path: Path) -> None:
        memory = _make_memory(tmp_path)
        summaries = [
            "用户和老婆吵架了，因为加班太多没时间陪伴家人",
            "用户和老婆去武当山旅行，凌晨看了日出",
            "用户和老婆一起做晚饭，聊到了换工作的打算",
            "用户和老婆看了场电影，感动得哭了",
        ]
        for idx, summary in enumerate(summaries):
            _seed_episode(memory, episode_id=f"ep-{idx}", summary=summary)

        assert len(recall_episodes(memory, "老婆", limit=2)) == 2

    def test_default_recall_still_excludes_episodes(self, tmp_path: Path) -> None:
        """Default behaviour is unchanged: the router never selects episodes."""
        memory = _make_memory(tmp_path)
        _seed_episode(memory)

        result = recall_for_prompt(memory, "老婆")

        assert all(s.layer != "episode" for s in result.snippets)


class TestMemorySearchEpisodesCorpus:
    def test_episodes_corpus_returns_diary_hits(self, tmp_path: Path) -> None:
        memory = _make_memory(tmp_path)
        _seed_episode(memory)
        svc = MemoryService(memory)

        result = svc.search("老婆", corpus="episodes")

        assert result["total"] == 1
        hit = result["hits"][0]
        assert hit["layer"] == "episode"
        assert hit["path"] == "episode/ep-1.md"
        assert hit["source_id"] == "ep-1"
        assert "吵架" in hit["snippet"]

    def test_default_corpus_does_not_surface_episodes(self, tmp_path: Path) -> None:
        memory = _make_memory(tmp_path)
        _seed_episode(memory)
        svc = MemoryService(memory)

        result = svc.search("老婆")

        assert all(hit["layer"] != "episode" for hit in result["hits"])

    def test_unknown_corpus_still_rejected(self, tmp_path: Path) -> None:
        svc = MemoryService(_make_memory(tmp_path))

        with pytest.raises(ValueError, match="unknown corpus"):
            svc.search("老婆", corpus="diary")

    def test_layer_order_includes_episode(self) -> None:
        assert "episode" in DEFAULT_RECALL_CFG["layer_order"]
