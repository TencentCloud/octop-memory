"""Exercise native PG checkpoint pruning on a dedicated PostgreSQL database."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict
from uuid import uuid4

import pytest

from octop_memory.core import Memory
from octop_memory.pipeline.lifecycle.checkpoint_gc import _cutoff_checkpoint_id, prune_checkpoints


@pytest.fixture
def store(postgres_dsn):
    pytest.importorskip("langgraph.checkpoint.postgres")
    memory = Memory(f"retention_{uuid4().hex}", backend="postgres", backend_config={"dsn": postgres_dsn})
    thread = uuid4().hex
    try:
        yield memory, thread
    finally:
        try:
            memory.delete_thread(thread)
        finally:
            if memory._checkpointer_pool is not None:
                memory._checkpointer_pool.close()
            memory.backend.close()


def _config(thread: str, ns: str = "") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread, "checkpoint_ns": ns}}


def test_snapshots_messages_without_changing_native_versions(store):
    from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
    from langgraph.checkpoint.serde.types import _DeltaSnapshot
    from langgraph.graph import END, START, StateGraph
    from langgraph.pregel._checkpoint import channels_from_checkpoint

    memory, thread = store
    delta = pytest.importorskip("langgraph.channels.delta").DeltaChannel
    reducer = pytest.importorskip("langgraph.graph.message")._messages_delta_reducer
    channel = delta(reducer, list, snapshot_frequency=200)
    schema = TypedDict("State", {"messages": Annotated[list, channel]})  # noqa: UP013 - runtime channel definition
    graph = (
        StateGraph(schema)
        .add_node("reply", lambda state: {"messages": [AIMessage(content="reply", id="answer")]})
        .add_edge(START, "reply")
        .add_edge("reply", END)
        .compile(checkpointer=memory)
    )
    config = _config(thread)
    for i in range(10):
        graph.invoke({"messages": [HumanMessage(content=f"turn {i}", id=str(i))]}, config)
    graph.invoke({"messages": [RemoveMessage(id="0")]}, config)
    original = list(memory.list(config))
    retained = original[:5]
    boundary = retained[-1]
    assert "messages" not in boundary.checkpoint["channel_values"]

    def states():
        snaps = [graph.get_state(row.config) for row in retained] + [graph.get_state(config)]
        return [(s.values, s.next, s.tasks, s.interrupts, s.metadata) for s in snaps]

    before = states()
    args = {"thread_id": thread, "keep_last": 5, "messages_channel": channel}
    preview = prune_checkpoints(memory, **args, dry_run=True)
    assert preview.checkpoints_deleted == len(original) - 5
    assert list(memory.list(config)) == original
    actual = prune_checkpoints(memory, **args)
    assert actual.checkpoints_deleted == preview.checkpoints_deleted
    assert actual.writes_deleted == preview.writes_deleted
    assert states() == before
    for row in retained:
        updated = memory.get_tuple(row.config)
        assert updated.checkpoint["channel_versions"] == row.checkpoint["channel_versions"]
        assert updated.pending_writes == row.pending_writes
    updated = memory.get_tuple(boundary.config)
    assert updated.parent_config is None
    assert isinstance(updated.checkpoint["channel_values"]["messages"], _DeltaSnapshot)
    # Simulate a reader obtaining the old boundary tuple, then crossing the
    # commit before it asks the saver for ancestor replay.
    channels, _ = channels_from_checkpoint(graph.channels, boundary.checkpoint, saver=memory, config=boundary.config)
    assert channels["messages"].get() == before[-2][0]["messages"]
    assert prune_checkpoints(memory, **args).checkpoints_deleted == 0
    output = graph.invoke({"messages": [HumanMessage(content="next", id="new")]}, config)
    assert [m.content for m in output["messages"] if isinstance(m, HumanMessage)] == [
        *(f"turn {i}" for i in range(1, 10)),
        "next",
    ]


def test_age_window_pending_and_subgraph_force_policy(store):
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.base import empty_checkpoint

    memory, thread = store
    now = datetime.now(UTC)

    def put(config, days):
        cp = empty_checkpoint()
        cp["id"] = _cutoff_checkpoint_id(now - timedelta(days=days))
        cp["channel_values"]["messages"] = [AIMessage(content="done", id="answer")]
        cp["channel_versions"]["messages"] = days
        return memory.put(config, cp, {}, {"messages": days})

    root = _config(thread)
    for age in (30, 20, 10, 5, 0):
        root = put(root, age)
    subgraph = put(_config(thread, "tools:child"), 15)
    args = {"thread_id": thread, "keep_last": 2, "keep_days": 7, "now": now}
    assert prune_checkpoints(memory, **args).checkpoints_deleted == 0
    assert prune_checkpoints(memory, **args, drop_subgraph_streams=True).checkpoints_deleted == 0
    preview = prune_checkpoints(memory, **args, drop_subgraph_streams=True, force_drop_subgraphs=True, dry_run=True)
    stats = prune_checkpoints(memory, **args, drop_subgraph_streams=True, force_drop_subgraphs=True)
    assert stats.checkpoints_deleted == preview.checkpoints_deleted == 4
    assert stats.streams_dropped == 1
    assert memory.get_tuple(subgraph) is None
    memory.put_writes(root, [("__interrupt__", [])], "paused")
    assert prune_checkpoints(memory, thread_id=thread, keep_last=1, skip_pending=True).parent_prunes_skipped == 1
    assert len(list(memory.list(_config(thread)))) == 2


def test_defers_when_a_writer_holds_a_checkpoint_table(store, postgres_dsn):
    import psycopg
    from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint

    memory, thread = store
    config = _config(thread)
    for step in range(3):
        config = memory.put(config, create_checkpoint(empty_checkpoint(), None, step), {}, {})
    with closing(psycopg.connect(postgres_dsn)) as other, other.transaction():
        other.execute("LOCK TABLE checkpoint_writes IN ROW EXCLUSIVE MODE")
        result = prune_checkpoints(memory, thread_id=thread, keep_last=1)
        assert result.checkpoints_deleted == 0
        assert result.parent_prunes_skipped == 1
    assert prune_checkpoints(memory, thread_id=thread, keep_last=1).checkpoints_deleted == 2
