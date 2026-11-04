"""The extracted-event cursor survives a runtime restart (issue #25)."""

from __future__ import annotations

from pathlib import Path

from octop_memory import Memory
from octop_memory.application.runtime import MemoryRuntime
from octop_memory.ports.llm import MockLLMClient


def _runtime(memory: Memory, llm: MockLLMClient) -> MemoryRuntime:
    return MemoryRuntime(
        memory,
        llm=llm,
        config={
            "capture": {"min_message_chars": 0},
            "extraction": {"promote": False, "regen_pages": False},
        },
    )


def test_extracted_cursor_survives_restart(tmp_path: Path) -> None:
    memory = Memory(
        namespace="cursor",
        backend="sqlite",
        backend_config={"db_path": str(tmp_path / "cursor.sqlite")},
    )
    try:
        for i in range(3):
            memory.add_raw(
                f"decided to use Augment first, NOT replace {i}",
                event_type="user_message",
                session_id="s1",
            )
        llm = MockLLMClient(default_response='{"candidates": []}')

        first = _runtime(memory, llm).extract({"session_id": "s1"})
        assert first["events_extracted"] == 3
        assert first["failure_reason"] is None

        # Simulate a process restart: a brand-new runtime over the same DB.
        # Without a persisted cursor the whole backlog is re-fed here.
        second = _runtime(memory, llm).extract({"session_id": "s1"})
        assert second["events_extracted"] == 0
        assert second["llm_calls"] == 0
        assert second["failure_reason"] is None
    finally:
        memory.backend.close()


def test_new_events_are_extracted_after_restart(tmp_path: Path) -> None:
    memory = Memory(
        namespace="cursor2",
        backend="sqlite",
        backend_config={"db_path": str(tmp_path / "cursor2.sqlite")},
    )
    try:
        memory.add_raw("first fact", event_type="user_message", session_id="s1")
        llm = MockLLMClient(default_response='{"candidates": []}')

        assert _runtime(memory, llm).extract({"session_id": "s1"})["events_extracted"] == 1

        # A new event appended after the restart is still picked up.
        memory.add_raw("second fact", event_type="user_message", session_id="s1")
        second = _runtime(memory, llm).extract({"session_id": "s1"})
        assert second["events_extracted"] == 1
    finally:
        memory.backend.close()
