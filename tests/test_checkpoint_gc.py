"""Tests for checkpoint retention pruning (octop_memory.pipeline.lifecycle.checkpoint_gc)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from octop_memory.core import Memory
from octop_memory.pipeline.lifecycle.checkpoint_gc import (
    DEFAULT_KEEP_LAST_CHECKPOINTS,
    _cutoff_checkpoint_id,
    prune_checkpoints,
)


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    return Memory(namespace="test", backend_config={"db_path": str(tmp_path / "test.sqlite")})


def _store_checkpoint(memory: Memory, thread_id: str, step: int = 0) -> None:
    """Helper to store a real checkpoint for a given thread via the public API."""
    from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    cp = create_checkpoint(empty_checkpoint(), None, step + 1)
    memory.put(config, cp, {"source": "input", "step": step}, {})


def _checkpoint_ids(memory: Memory, thread_id: str, checkpoint_ns: str | None = None) -> list[str]:
    conn = memory._checkpointer.conn
    if checkpoint_ns is None:
        rows = conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id DESC",
            (thread_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? "
            "ORDER BY checkpoint_id DESC",
            (thread_id, checkpoint_ns),
        ).fetchall()
    return [r[0] for r in rows]


def _store_parent_with_messages(memory: Memory, thread_id: str, *, tool_calls: bool = False) -> None:
    """Write a parent checkpoint whose channel_values look like a real turn."""
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    calls = [{"id": "call-1", "name": "bash", "args": {"command": "ls"}}] if tool_calls else []
    base = empty_checkpoint()
    base["channel_values"] = {"messages": [AIMessage(content="done", tool_calls=calls)]}
    cp = create_checkpoint(base, None, 1)
    memory.put(config, cp, {"source": "loop", "step": 1}, {})


def _insert_raw_checkpoint(memory: Memory, thread_id: str, checkpoint_id: str, checkpoint_ns: str = "") -> None:
    """Insert a bare checkpoints/writes row directly, bypassing serialization.

    Used to control``checkpoint_id`` precisely for age-based (``keep_days``)
    tests, where we need rows that look like they were created at a specific
    past instant without waiting in real time.
    """
    from langgraph.checkpoint.base import empty_checkpoint

    saver = memory._checkpointer
    conn = saver.conn
    cp = empty_checkpoint()
    cp["id"] = checkpoint_id
    kind, payload = saver.serde.dumps_typed(cp)
    conn.execute(
        "INSERT INTO checkpoints "
        "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
        "VALUES (?, ?, ?, NULL, ?, ?, '{}')",
        (thread_id, checkpoint_ns, checkpoint_id, kind, payload),
    )
    conn.execute(
        "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
        "VALUES (?, ?, ?, 'task-1', 0, 'chan', 'json', X'00')",
        (thread_id, checkpoint_ns, checkpoint_id),
    )
    conn.commit()


class TestKeepLast:
    @pytest.mark.parametrize("snapshot_messages", [False, True])
    def test_retained_graph_checkpoints_restore_all_delta_channels(
        self, memory: Memory, snapshot_messages: bool
    ) -> None:
        from typing import Annotated, TypedDict

        from langchain_core.messages import HumanMessage
        from langgraph.graph import END, START, StateGraph

        delta_channel = pytest.importorskip("langgraph.channels.delta").DeltaChannel
        message_reducer = pytest.importorskip("langgraph.graph.message")._messages_delta_reducer
        messages = delta_channel(message_reducer, list, snapshot_frequency=3)
        state_schema = TypedDict(  # noqa: UP013 - class annotations cannot resolve function-local imports.
            "State",
            {
                "prompt": str,
                "messages": Annotated[list, messages],
                "audit": Annotated[list, delta_channel(lambda seed, writes: seed + writes, snapshot_frequency=4)],
            },
        )

        graph = (
            StateGraph(state_schema)
            .add_node(
                "record",
                lambda state: {
                    "messages": [HumanMessage(content=state["prompt"], id=state["prompt"])],
                    "audit": state["prompt"],
                },
            )
            .add_edge(START, "record")
            .add_edge("record", END)
            .compile(checkpointer=memory._checkpointer)
        )
        config = {"configurable": {"thread_id": "graph"}}
        for i in range(18):
            graph.invoke({"prompt": f"turn {i}"}, config)

        def states() -> dict[str, Any]:
            result = {}
            for checkpoint in list(memory.list(config)):
                item = graph.get_state(checkpoint.config)
                result[item.config["configurable"]["checkpoint_id"]] = (
                    item.values,
                    item.next,
                    item.tasks,
                    item.interrupts,
                )
            return result

        before = states()
        keep_last = 5
        options = {
            "keep_last": keep_last,
            "thread_id": "graph",
            "messages_channel": messages if snapshot_messages else None,
        }
        preview = prune_checkpoints(memory, **options, dry_run=True)
        assert states() == before
        stats = prune_checkpoints(memory, **options)
        after = states()

        assert stats.checkpoints_deleted > 0
        assert preview.checkpoints_deleted == stats.checkpoints_deleted
        assert preview.writes_deleted == stats.writes_deleted
        assert set(list(before)[:keep_last]) <= after.keys()
        assert next(iter(after)) == next(iter(before))
        assert after == {cid: before[cid] for cid in after}
        assert prune_checkpoints(memory, **options).checkpoints_deleted == 0

    def test_keeps_only_newest_n_checkpoints(self, memory: Memory) -> None:
        for step in range(5):
            _store_checkpoint(memory, "thread-1", step=step)
        before = _checkpoint_ids(memory, "thread-1")
        assert len(before) == 5

        stats = prune_checkpoints(memory, keep_last=2, thread_id="thread-1")

        after = _checkpoint_ids(memory, "thread-1")
        assert after == before[:2]
        assert stats.checkpoints_deleted == 3
        # _store_checkpoint only calls memory.put(), never put_writes(), so
        # the writes table stays empty in this scenario.
        assert stats.writes_deleted == 0
        assert stats.dry_run is False

    def test_never_deletes_below_one_survivor(self, memory: Memory) -> None:
        _store_checkpoint(memory, "thread-1", step=0)
        stats = prune_checkpoints(memory, keep_last=1, thread_id="thread-1")
        assert stats.checkpoints_deleted == 0
        assert len(_checkpoint_ids(memory, "thread-1")) == 1

    def test_only_affects_target_thread(self, memory: Memory) -> None:
        for step in range(3):
            _store_checkpoint(memory, "thread-1", step=step)
        for step in range(3):
            _store_checkpoint(memory, "thread-2", step=step)

        prune_checkpoints(memory, keep_last=1, thread_id="thread-1")

        assert len(_checkpoint_ids(memory, "thread-1")) == 1
        assert len(_checkpoint_ids(memory, "thread-2")) == 3

    def test_scans_all_threads_when_thread_id_omitted(self, memory: Memory) -> None:
        for step in range(3):
            _store_checkpoint(memory, "thread-1", step=step)
        for step in range(3):
            _store_checkpoint(memory, "thread-2", step=step)

        stats = prune_checkpoints(memory, keep_last=1)

        assert stats.threads_scanned == 2
        assert len(_checkpoint_ids(memory, "thread-1")) == 1
        assert len(_checkpoint_ids(memory, "thread-2")) == 1

    def test_dry_run_deletes_nothing(self, memory: Memory) -> None:
        for step in range(5):
            _store_checkpoint(memory, "thread-1", step=step)

        stats = prune_checkpoints(memory, keep_last=2, thread_id="thread-1", dry_run=True)

        assert stats.dry_run is True
        assert stats.checkpoints_deleted == 3
        assert len(_checkpoint_ids(memory, "thread-1")) == 5

    def test_dry_run_counts_writes_rows_too(self, memory: Memory) -> None:
        """dry_run must predict writes_deleted, not silently report 0.

        The real path gets the number from DELETE's rowcount, which a
        preview never executes — so dry_run has to COUNT explicitly.
        Regression test: this used to always report 0, under-reporting
        the blast radius of a prune.
        """
        for i in range(5):
            _insert_raw_checkpoint(memory, "thread-w", f"00000000-0000-6000-8000-00000000000{i}")

        preview = prune_checkpoints(memory, keep_last=2, thread_id="thread-w", dry_run=True)

        assert preview.checkpoints_deleted == 3
        assert preview.writes_deleted == 3, "one writes row per checkpoint in this fixture"

        # The prediction matches what a real prune actually deletes.
        real = prune_checkpoints(memory, keep_last=2, thread_id="thread-w")
        assert real.checkpoints_deleted == preview.checkpoints_deleted
        assert real.writes_deleted == preview.writes_deleted

    def test_get_thread_state_still_works_after_prune(self, memory: Memory) -> None:
        for step in range(5):
            _store_checkpoint(memory, "thread-1", step=step)

        prune_checkpoints(memory, keep_last=2, thread_id="thread-1")

        state = memory.get_thread_state("thread-1")
        assert state is not None
        assert state.metadata["step"] == 4


class TestKeepDays:
    def test_keeps_checkpoints_newer_than_cutoff(self, memory: Memory) -> None:
        now = datetime.now(UTC)
        old_id = _cutoff_checkpoint_id(now - timedelta(days=10))
        recent_id = _cutoff_checkpoint_id(now - timedelta(hours=1))
        _insert_raw_checkpoint(memory, "thread-1", old_id)
        _insert_raw_checkpoint(memory, "thread-1", recent_id)

        stats = prune_checkpoints(memory, keep_days=7, thread_id="thread-1", now=now)

        remaining = _checkpoint_ids(memory, "thread-1")
        assert remaining == [recent_id]
        assert stats.checkpoints_deleted == 1
        # _insert_raw_checkpoint always seeds one writes row per checkpoint.
        assert stats.writes_deleted == 1

    def test_never_deletes_the_last_survivor_even_if_stale(self, memory: Memory) -> None:
        now = datetime.now(UTC)
        old_id = _cutoff_checkpoint_id(now - timedelta(days=365))
        _insert_raw_checkpoint(memory, "thread-1", old_id)

        stats = prune_checkpoints(memory, keep_days=7, thread_id="thread-1", now=now)

        assert stats.checkpoints_deleted == 0
        assert _checkpoint_ids(memory, "thread-1") == [old_id]


class TestCombinedAndValidation:
    def test_or_semantics_between_keep_last_and_keep_days(self, memory: Memory) -> None:
        now = datetime.now(UTC)
        # 3 old checkpoints (beyond keep_days) but within keep_last rank -> survive via rank.
        ids = [_cutoff_checkpoint_id(now - timedelta(days=30 - i)) for i in range(3)]
        for cid in ids:
            _insert_raw_checkpoint(memory, "thread-1", cid)

        stats = prune_checkpoints(memory, keep_last=2, keep_days=1, thread_id="thread-1", now=now)

        remaining = set(_checkpoint_ids(memory, "thread-1"))
        # newest 2 by keep_last survive even though all are older than keep_days=1
        assert remaining == set(sorted(ids, reverse=True)[:2])
        assert stats.checkpoints_deleted == 1

    def test_requires_at_least_one_retention_knob(self, memory: Memory) -> None:
        with pytest.raises(ValueError, match="keep_last and/or keep_days"):
            prune_checkpoints(memory, thread_id="thread-1")

    def test_rejects_keep_last_below_one(self, memory: Memory) -> None:
        with pytest.raises(ValueError, match="keep_last must be >= 1"):
            prune_checkpoints(memory, keep_last=0, thread_id="thread-1")

    def test_rejects_negative_keep_days(self, memory: Memory) -> None:
        with pytest.raises(ValueError, match="keep_days must be >= 0"):
            prune_checkpoints(memory, keep_days=-1, thread_id="thread-1")


class TestCutoffCheckpointIdOrdering:
    def test_cutoff_ids_sort_chronologically(self) -> None:
        base = datetime.now(UTC)
        earlier = _cutoff_checkpoint_id(base - timedelta(days=1))
        later = _cutoff_checkpoint_id(base)
        assert earlier < later


class TestDropSubgraphStreams:
    def test_default_keep_last_is_one(self) -> None:
        assert DEFAULT_KEEP_LAST_CHECKPOINTS == 1

    def test_force_drop_deletes_tools_stream_keeps_latest_parent(self, memory: Memory) -> None:
        for step in range(3):
            _store_checkpoint(memory, "thread-1", step=step)
        for i in range(4):
            _insert_raw_checkpoint(memory, "thread-1", f"00000000-0000-6000-8000-00000000000{i}", "tools:abc")

        stats = prune_checkpoints(
            memory,
            keep_last=1,
            thread_id="thread-1",
            drop_subgraph_streams=True,
            force_drop_subgraphs=True,
        )

        assert len(_checkpoint_ids(memory, "thread-1", "")) == 1
        assert _checkpoint_ids(memory, "thread-1", "tools:abc") == []
        assert stats.streams_dropped == 1
        assert stats.checkpoints_deleted == 2 + 4
        assert memory.get_thread_state("thread-1") is not None

    def test_skips_drop_when_parent_has_tool_calls(self, memory: Memory) -> None:
        _store_parent_with_messages(memory, "hitl-1", tool_calls=True)
        _insert_raw_checkpoint(memory, "hitl-1", "00000000-0000-6000-8000-000000000001", "tools:hitl")

        stats = prune_checkpoints(memory, keep_last=1, thread_id="hitl-1", drop_subgraph_streams=True)

        assert _checkpoint_ids(memory, "hitl-1", "tools:hitl")
        assert stats.streams_dropped == 0

    def test_drops_when_parent_is_final_ai(self, memory: Memory) -> None:
        _store_parent_with_messages(memory, "done-1", tool_calls=False)
        # Older than the real parent put() id so the subgraph-ahead guard
        # does not treat this as an in-flight tool stream.
        _insert_raw_checkpoint(memory, "done-1", "00000000-0000-6000-8000-000000000001", "tools:done")

        stats = prune_checkpoints(memory, keep_last=1, thread_id="done-1", drop_subgraph_streams=True)

        assert _checkpoint_ids(memory, "done-1", "tools:done") == []
        assert stats.streams_dropped == 1
        assert memory.get_thread_state("done-1") is not None

    def test_skips_drop_when_subgraph_is_newer_than_parent(self, memory: Memory) -> None:
        _store_parent_with_messages(memory, "live-1", tool_calls=False)
        _insert_raw_checkpoint(
            memory,
            "live-1",
            "ffffffff-ffff-6fff-8000-ffffffffffff",
            "tools:live",
        )

        stats = prune_checkpoints(memory, keep_last=1, thread_id="live-1", drop_subgraph_streams=True)

        assert _checkpoint_ids(memory, "live-1", "tools:live")
        assert stats.streams_dropped == 0

    def test_dry_run_counts_dropped_stream(self, memory: Memory) -> None:
        _store_checkpoint(memory, "thread-d", step=0)
        _insert_raw_checkpoint(memory, "thread-d", "00000000-0000-6000-8000-000000000001", "tools:dry")

        preview = prune_checkpoints(
            memory,
            keep_last=1,
            thread_id="thread-d",
            drop_subgraph_streams=True,
            force_drop_subgraphs=True,
            dry_run=True,
        )

        assert preview.dry_run is True
        assert preview.streams_dropped == 1
        assert preview.checkpoints_deleted == 1
        assert preview.writes_deleted == 1
        assert _checkpoint_ids(memory, "thread-d", "tools:dry")


def _parent_messages(memory: Memory, thread_id: str) -> list[Any]:
    tup = memory.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    assert tup is not None
    values = tup.checkpoint.get("channel_values") or {}
    if "messages" in values:
        raw = values["messages"]
    else:
        history = memory._checkpointer.get_delta_channel_history(config=tup.config, channels=["messages"])
        raw = history["messages"].get("seed", [])
    assert isinstance(raw, list)
    return raw


def _store_delta_parent_chain(
    memory: Memory,
    thread_id: str,
) -> list[Any]:
    """Parent row 0 holds (or omits) messages; row 1 is a delta omit of messages."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint

    messages = [
        HumanMessage(content="hi", id="u1"),
        AIMessage(
            content="",
            id="a1",
            additional_kwargs={"reasoning_content": "search first"},
            tool_calls=[{"id": "c1", "name": "search", "args": {"q": "x"}}],
        ),
        ToolMessage(content="result", tool_call_id="c1", name="search", id="t1"),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "enough evidence"},
                {"type": "text", "text": "done"},
            ],
            id="a2",
            additional_kwargs={"reasoning_content": "enough evidence"},
        ),
    ]
    config0 = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    base = empty_checkpoint()
    versions = {"messages": "0000000000000001", "skills_metadata": "0000000000000001"}
    base["channel_values"] = {"messages": messages, "skills_metadata": ["s"]}
    base["channel_versions"] = dict(versions)
    cp0 = create_checkpoint(base, None, 1)
    saved0 = memory.put(config0, cp0, {"source": "loop", "step": 0}, {})

    cp1 = create_checkpoint(cp0, None, 2)
    cp1["channel_values"] = {"skills_metadata": ["s"]}
    cp1["channel_versions"] = dict(versions)
    memory.put(saved0, cp1, {"source": "loop", "step": 1}, {})
    return messages


class TestRetentionDependencies:
    def test_materializes_messages_at_oldest_retained_checkpoint(self, memory: Memory) -> None:
        from typing import Annotated, TypedDict

        from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
        from langgraph.graph import END, START, StateGraph

        delta_channel = pytest.importorskip("langgraph.channels.delta").DeltaChannel
        reducer = pytest.importorskip("langgraph.graph.message")._messages_delta_reducer
        snapshot_type = pytest.importorskip("langgraph.checkpoint.serde.types")._DeltaSnapshot
        channel = delta_channel(reducer, list, snapshot_frequency=200)
        schema = TypedDict(  # noqa: UP013 - class annotations cannot resolve function-local imports.
            "State", {"messages": Annotated[list, channel]}
        )
        graph = (
            StateGraph(schema)
            .add_node("reply", lambda state: {"messages": [AIMessage(content="reply", id="answer")]})
            .add_edge(START, "reply")
            .add_edge("reply", END)
            .compile(checkpointer=memory)
        )
        thread = {"configurable": {"thread_id": "snapshot", "checkpoint_ns": ""}}
        for index in range(10):
            graph.invoke({"messages": [HumanMessage(content=f"turn {index}", id=str(index))]}, thread)
        graph.invoke({"messages": [RemoveMessage(id="0")]}, thread)
        saved = list(memory.list(thread))
        keep_last = 5
        window = saved[:keep_last]
        boundary = window[-1]
        assert "messages" not in boundary.checkpoint["channel_values"]
        assert boundary.parent_config is not None

        def states() -> list[tuple[Any, ...]]:
            snapshots = [graph.get_state(row.config) for row in window] + [graph.get_state(thread)]
            return [(snap.values, snap.next, snap.tasks, snap.interrupts) for snap in snapshots]

        before = states()
        options = {"thread_id": "snapshot", "keep_last": keep_last, "messages_channel": channel}
        preview = prune_checkpoints(memory, **options, dry_run=True)
        assert preview.checkpoints_deleted == len(saved) - keep_last
        assert list(memory.list(thread)) == saved
        conn = memory._checkpointer.conn
        conn.execute(
            "CREATE TEMP TRIGGER fail_prune BEFORE DELETE ON checkpoints BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            prune_checkpoints(memory, **options)
        assert list(memory.list(thread)) == saved
        conn.execute("DROP TRIGGER fail_prune")

        stats = prune_checkpoints(memory, **options)
        assert stats.checkpoints_deleted == preview.checkpoints_deleted
        assert stats.writes_deleted == preview.writes_deleted
        actual_boundary = memory.get_tuple(boundary.config)
        assert actual_boundary.parent_config is None
        assert actual_boundary.pending_writes == boundary.pending_writes
        assert actual_boundary.metadata == boundary.metadata
        assert isinstance(actual_boundary.checkpoint["channel_values"]["messages"], snapshot_type)
        assert states() == before
        assert prune_checkpoints(memory, **options).checkpoints_deleted == 0
        output = graph.invoke({"messages": [HumanMessage(content="next turn", id="next")]}, thread)
        assert [m.content for m in output["messages"] if isinstance(m, HumanMessage)] == [
            *(f"turn {i}" for i in range(1, 10)),
            "next turn",
        ]

    def test_scheduled_sqlite_retention_does_not_require_postgres(
        self, memory: Memory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from octop_memory.application.checkpoint_maintenance import run_checkpoint_retention

        monkeypatch.setitem(sys.modules, "octop_memory.storage.backends.postgres", None)
        for step in range(3):
            _store_checkpoint(memory, "sqlite-only", step)

        result = run_checkpoint_retention(memory, keep_last=1)

        assert result is not None
        assert result.checkpoints_deleted == 2

    def test_delta_omit_keeps_ancestor_and_message_details(self, memory: Memory) -> None:
        expected = _store_delta_parent_chain(memory, "thr-delta")
        before = _checkpoint_ids(memory, "thr-delta", "")

        stats = prune_checkpoints(memory, keep_last=1, thread_id="thr-delta")

        assert stats.checkpoints_deleted == 0
        assert _checkpoint_ids(memory, "thr-delta", "") == before
        assert _parent_messages(memory, "thr-delta") == expected

    def test_missing_ancestor_skips_prune(self, memory: Memory) -> None:
        _store_delta_parent_chain(memory, "thr-broken")
        before = _checkpoint_ids(memory, "thr-broken", "")
        conn = memory._checkpointer.conn
        conn.execute("UPDATE checkpoints SET parent_checkpoint_id = 'missing' WHERE checkpoint_id = ?", (before[-1],))
        conn.commit()

        stats = prune_checkpoints(memory, keep_last=1, thread_id="thr-broken")

        assert stats.parent_prunes_skipped == 1
        assert stats.checkpoints_deleted == 0
        assert _checkpoint_ids(memory, "thr-broken", "") == before

    def test_prune_rolls_back_boundary_and_writes_together(self, memory: Memory) -> None:
        from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint

        config = {"configurable": {"thread_id": "rollback"}}
        first = create_checkpoint(empty_checkpoint(), None, 0)
        first["channel_values"]["counter"] = 1
        first_config = memory.put(config, first, {}, {})
        memory.put_writes(first_config, [("counter", 2)], "task")
        last = create_checkpoint(first, None, 1)
        last_config = memory.put(first_config, last, {}, {})
        conn = memory._checkpointer.conn
        conn.execute(
            "CREATE TEMP TRIGGER fail_prune BEFORE DELETE ON checkpoints BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            prune_checkpoints(memory, keep_last=1)

        assert memory.get_tuple(last_config).parent_config == first_config
        assert memory.get_tuple(first_config).pending_writes == [("task", "counter", 2)]
        conn.execute("DROP TRIGGER fail_prune")
        stats = prune_checkpoints(memory, keep_last=1)
        assert (stats.checkpoints_deleted, stats.writes_deleted) == (1, 1)
        assert memory.get_tuple(last_config).parent_config is None

    @pytest.mark.parametrize("drop_subgraphs", [False, True])
    def test_preserves_tools_stream_with_retained_parent_history(self, memory: Memory, drop_subgraphs: bool) -> None:
        expected = _store_delta_parent_chain(memory, "thr-tools")
        _insert_raw_checkpoint(memory, "thr-tools", "00000000-0000-6000-8000-000000000001", "tools:x")

        stats = prune_checkpoints(memory, keep_last=1, thread_id="thr-tools", drop_subgraph_streams=drop_subgraphs)

        assert _checkpoint_ids(memory, "thr-tools", "tools:x")
        assert stats.streams_dropped == 0
        assert _parent_messages(memory, "thr-tools") == expected

    def test_prune_then_vacuum_keeps_tools_and_thinking(self, memory: Memory) -> None:
        from langgraph.checkpoint.base import empty_checkpoint

        from octop_memory.pipeline.lifecycle.vacuum import nudge_vacuum

        cp = empty_checkpoint()
        cp["channel_values"] = {"large_value": b"x" * 80_000}
        memory.put({"configurable": {"thread_id": "thr-vac"}}, cp, {"source": "input", "step": -1}, {})
        expected = _store_delta_parent_chain(memory, "thr-vac")

        stats = prune_checkpoints(memory, keep_last=1, thread_id="thr-vac")
        assert stats.checkpoints_deleted == 1

        conn = memory._checkpointer.conn
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 0
        vac = nudge_vacuum(memory)

        assert _parent_messages(memory, "thr-vac") == expected
        assert vac.auto_vacuum_enabled is True
        assert vac.pages_reclaimed > 0
