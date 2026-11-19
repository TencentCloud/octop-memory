"""Tests for configurable keep/skip extraction rules (TencentCloud/Octop#2963)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from octop_memory import Memory, MemoryService
from octop_memory.application.config import (
    DEFAULT_EXTRACTION_CFG,
    MAX_EXTRACTION_RULE_CHARS,
    normalize_extraction_rule,
)
from octop_memory.application.runtime import MemoryRuntime
from octop_memory.pipeline.episode import EpisodeExtractor
from octop_memory.pipeline.episode.prompts import render_episode_prompt
from octop_memory.pipeline.extractor import CandidateExtractor
from octop_memory.pipeline.extractor.prompts import render_prompt
from octop_memory.ports.llm import LLMClient
from octop_memory.types import RawEvent


class _RecordingLLM(LLMClient):
    """Records every prompt sent during extraction while returning empty valid JSON."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def call_llm(
        self,
        prompt: str,
        *,
        tier: str = "light",
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: str | None = None,
    ) -> str:
        _ = (tier, temperature, max_tokens, response_format)
        self.prompts.append(prompt)
        if "Episode Extractor" in prompt:
            return '{"episodes": []}'
        return '{"candidates": []}'


def test_service_advertises_versioned_extraction_rules() -> None:
    assert MemoryService.supports_extraction_rules
    llm = _RecordingLLM()
    assert CandidateExtractor(llm=llm, keep_rules="keep").extractor_version == "v2.6"
    assert EpisodeExtractor(llm=llm, skip_rules="skip").extractor_version == "episode/v2"


def test_default_extraction_cfg_has_unset_rules() -> None:
    assert DEFAULT_EXTRACTION_CFG["keep_rules"] is None
    assert DEFAULT_EXTRACTION_CFG["skip_rules"] is None


def test_normalize_extraction_rule() -> None:
    assert normalize_extraction_rule(None) is None
    assert normalize_extraction_rule(123) is None
    assert normalize_extraction_rule("   \n\t  ") is None
    assert normalize_extraction_rule("  keep stable preferences  ") == "keep stable preferences"

    long_text = "a" * (MAX_EXTRACTION_RULE_CHARS + 500)
    normalized = normalize_extraction_rule(long_text)
    assert normalized is not None
    assert len(normalized) == MAX_EXTRACTION_RULE_CHARS


def test_render_prompt_unchanged_when_rules_empty() -> None:
    base = render_prompt("[]", max_candidates=10)
    assert render_prompt("[]", max_candidates=10, keep_rules=None, skip_rules=None) == base
    assert render_prompt("[]", max_candidates=10, keep_rules="   ", skip_rules="\n") == base
    assert "Additional retention rules" not in base


def test_render_prompt_embeds_keep_and_skip_rules_safely() -> None:
    prompt = render_prompt(
        "[]",
        max_candidates=10,
        keep_rules="Keep domain decisions with {braces} and {'json': 1}",
        skip_rules="Skip casual greetings and {weather}",
    )
    assert "Additional retention rules" in prompt
    assert "- Prefer keeping as long-term memory:" in prompt
    assert "Keep domain decisions with {braces} and {'json': 1}" in prompt
    assert "- Skip (do NOT store as long-term memory):" in prompt
    assert "Skip casual greetings and {weather}" in prompt
    assert prompt.index("Additional retention rules") < prompt.index("Anti-dilution rules (CRITICAL")


def test_render_prompt_keep_only_and_skip_only() -> None:
    keep_only = render_prompt("[]", keep_rules="Keep architecture notes")
    assert "- Prefer keeping as long-term memory:" in keep_only
    assert "- Skip (do NOT store as long-term memory):" not in keep_only

    skip_only = render_prompt("[]", skip_rules="Skip ephemeral build logs")
    assert "- Prefer keeping as long-term memory:" not in skip_only
    assert "- Skip (do NOT store as long-term memory):" in skip_only


def test_render_episode_prompt_unchanged_when_rules_empty() -> None:
    base = render_episode_prompt("[]", max_episodes=5)
    assert render_episode_prompt("[]", max_episodes=5, keep_rules=" ", skip_rules="") == base
    assert "Additional retention rules" not in base


def test_render_episode_prompt_embeds_rules_safely() -> None:
    prompt = render_episode_prompt(
        "[]",
        max_episodes=5,
        keep_rules="Keep project milestone reflections {v1}",
        skip_rules="Skip routine standups {daily}",
    )
    assert "Additional retention rules" in prompt
    assert "Keep project milestone reflections {v1}" in prompt
    assert "Skip routine standups {daily}" in prompt


def test_runtime_extract_forwards_keep_and_skip_rules(tmp_path: Path) -> None:
    mem = Memory(namespace="rules_test", backend="sqlite", backend_config={"db_path": str(tmp_path / "mem.db")})
    try:
        mem.add_raw_batch(
            [
                RawEvent(
                    id="raw-1",
                    host="test",
                    session_id="sess-1",
                    thread_id="thread-1",
                    user="u1",
                    timestamp=datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
                    event_type="user_message",
                    content="I prefer concise engineering summaries and never want weather chatter saved.",
                    payload={},
                )
            ]
        )
        llm = _RecordingLLM()
        cfg: dict[str, Any] = {
            "extraction": {
                "keep_rules": "Prefer keeping stable preferences and project conventions.",
                "skip_rules": "Skip weather chatter and one-off greetings.",
            }
        }
        runtime = MemoryRuntime(mem, config=cfg, llm=llm)
        result = runtime.extract({"session_id": "sess-1", "promote": False, "regen_pages": False})

        assert result["failure_reason"] is None
        assert len(llm.prompts) == 2  # candidate extractor + episode extractor
        for sent_prompt in llm.prompts:
            assert "Prefer keeping stable preferences and project conventions." in sent_prompt
            assert "Skip weather chatter and one-off greetings." in sent_prompt
    finally:
        mem.backend.close()
