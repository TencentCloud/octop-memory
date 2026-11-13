"""Exercise native PG checkpoint pruning on a dedicated PostgreSQL database."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Event
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
    args = {"thread_id": thread, "keep_last": 5, "messages_channel": channel, "offline": True}
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


def test_age_window_pending_and_subgraph_policy(store):
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
    subgraph = put(_config(thread, f"tools:{uuid4()}"), 15)
    args = {"thread_id": thread, "keep_last": 2, "keep_days": 7, "now": now, "offline": True}
    assert prune_checkpoints(memory, **args).checkpoints_deleted == 0
    assert prune_checkpoints(memory, **args, drop_subgraph_streams=True).checkpoints_deleted == 0
    with pytest.raises(ValueError, match="only on SQLite"):
        prune_checkpoints(memory, **args, drop_subgraph_streams=True, force_drop_subgraphs=True)
    memory.put_writes(root, [("__interrupt__", [])], "paused")
    args.update(keep_last=1, keep_days=0)
    assert prune_checkpoints(memory, **args, drop_subgraph_streams=True).parent_prunes_skipped == 1
    assert len(list(memory.list(_config(thread)))) == 5
    root = put(root, -1)
    preview = prune_checkpoints(memory, **args, drop_subgraph_streams=True, dry_run=True)
    # The age window retains the two newest checkpoints even with keep_last=1.
    assert preview.checkpoints_deleted == 0
    args["keep_days"] = None
    preview = prune_checkpoints(memory, **args, drop_subgraph_streams=True, dry_run=True)
    stats = prune_checkpoints(memory, **args, drop_subgraph_streams=True)
    assert stats.checkpoints_deleted == preview.checkpoints_deleted == 6
    assert stats.streams_dropped == 1
    assert memory.get_tuple(subgraph) is None
    assert len(list(memory.list(_config(thread)))) == 1


def test_defers_when_a_writer_holds_a_checkpoint_table(store, postgres_dsn):
    import psycopg
    from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint

    memory, thread = store
    config = _config(thread)
    for step in range(3):
        config = memory.put(config, create_checkpoint(empty_checkpoint(), None, step), {}, {})
    with closing(psycopg.connect(postgres_dsn)) as other, other.transaction():
        other.execute("LOCK TABLE checkpoint_writes IN ROW EXCLUSIVE MODE")
        result = prune_checkpoints(memory, thread_id=thread, keep_last=1, offline=True)
        assert result.checkpoints_deleted == 0
        assert result.parent_prunes_skipped == 1
    assert prune_checkpoints(memory, thread_id=thread, keep_last=1, offline=True).checkpoints_deleted == 2


def test_pruning_leaves_shared_integer_versions_available_to_a_retained_branch(store):
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.serde.types import _DeltaSnapshot
    from langgraph.pregel._checkpoint import channels_from_checkpoint

    memory, thread = store
    delta = pytest.importorskip("langgraph.channels.delta").DeltaChannel
    reducer = pytest.importorskip("langgraph.graph.message")._messages_delta_reducer
    channel = delta(reducer, list)
    earlier = HumanMessage(content="original", id="a")
    branch = HumanMessage(content="branch", id="b")
    followup = HumanMessage(content="followup", id="c")

    def put(parent, version, messages=None):
        cp = empty_checkpoint()
        cp["v"] = 4
        cp["channel_versions"]["messages"] = version
        if messages is not None:
            cp["channel_values"]["messages"] = _DeltaSnapshot(messages)
        return memory.put(parent, cp, {}, {"messages": version})

    base = put(_config(thread), 1, [earlier])
    put(base, 3)
    alternate = put(base, 2, [branch])
    result = prune_checkpoints(memory, thread_id=thread, keep_last=2, messages_channel=channel, offline=True)
    assert result.checkpoints_deleted == 0
    assert memory.get_tuple(base) is not None
    memory.put_writes(alternate, [("messages", [followup])], "next")
    target = put(alternate, memory.get_next_version(2, None))
    saved = memory.get_tuple(target)
    channels, _ = channels_from_checkpoint({"messages": channel}, saved.checkpoint, saver=memory, config=target)
    assert channels["messages"].get() == [branch, followup]


def test_shared_store_requires_an_explicit_thread_and_leaves_other_agent_untouched(store, postgres_dsn):
    import psycopg
    from click.testing import CliRunner
    from langgraph.checkpoint.base import empty_checkpoint

    from octop_memory.adapters.cli.thread_cmd import thread_group
    from octop_memory.application.checkpoint_maintenance import run_checkpoint_retention
    from octop_memory.pipeline.lifecycle.maintenance import run_idle_maintenance

    memory, thread = store
    other = Memory(f"other_agent_{uuid4().hex}", backend="postgres", backend_config={"dsn": postgres_dsn})
    other_thread = uuid4().hex

    def rows(conn, tid):
        return [
            conn.execute(f"SELECT * FROM {table} WHERE thread_id = %s ORDER BY 1, 2, 3, 4", (tid,)).fetchall()
            for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs")
        ]

    try:
        for agent, tid in ((memory, thread), (other, other_thread)):
            config = _config(tid)
            for version in range(4):
                cp = empty_checkpoint()
                cp["channel_values"]["value"] = [version]
                cp["channel_versions"]["value"] = version
                config = agent.put(config, cp, {}, {"value": version})
                agent.put_writes(config, [("value", str(version))], "task")

        with psycopg.connect(postgres_dsn, autocommit=True) as conn:
            before = {tid: rows(conn, tid) for tid in (thread, other_thread)}
            assert all(len(table) == 4 for agent_rows in before.values() for table in agent_rows)
            assert run_checkpoint_retention(memory, keep_last=1) is None
            idle = run_idle_maintenance(memory)
            assert idle.prune_error is None
            assert idle.prune.threads_scanned == 0
            assert prune_checkpoints(memory, keep_last=1, dry_run=True).threads_scanned == 0
            with pytest.raises(ValueError, match="explicit thread_id"):
                prune_checkpoints(memory, keep_last=1, offline=True)
            runner = CliRunner()
            cli_config = {"backend": "postgres", "namespace": memory.namespace, "dsn": postgres_dsn, "db": ""}
            result = runner.invoke(thread_group, ["prune", "--dry-run"], obj=cli_config)
            assert result.exit_code == 2
            assert "requires --thread-id" in result.output
            result = runner.invoke(thread_group, ["prune", "--thread-id", thread], obj=cli_config)
            assert result.exit_code == 2
            assert "requires --offline" in result.output
            assert {tid: rows(conn, tid) for tid in before} == before
            actual = prune_checkpoints(memory, keep_last=1, thread_id=thread, offline=True)
            assert actual.threads_scanned == 1
            assert actual.checkpoints_deleted == 3
            assert [len(table) for table in rows(conn, thread)] == [1, 1, 1]
            assert rows(conn, other_thread) == before[other_thread]
    finally:
        try:
            other.delete_thread(other_thread)
        finally:
            if other._checkpointer_pool is not None:
                other._checkpointer_pool.close()
            other.backend.close()


def test_live_fork_keeps_its_parent_chain(store):
    from langgraph.graph import END, START, StateGraph

    memory, thread = store
    started = Event()
    resume = Event()

    class State(TypedDict):
        value: str

    def reply(state: State) -> State:
        if state["value"] == "fork":
            started.set()
            assert resume.wait(10)
        return {"value": state["value"] + "!"}

    graph = StateGraph(State).add_node("reply", reply).add_edge(START, "reply").add_edge("reply", END)
    app = graph.compile(checkpointer=memory)
    config = _config(thread)
    app.invoke({"value": "base"}, config, durability="sync")
    base = memory.get_tuple(config)
    for value in ("second", "third", "fourth"):
        app.invoke({"value": value}, config, durability="sync")
    history = list(memory.list(config))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(app.invoke, {"value": "fork"}, base.config, durability="exit")
        try:
            assert started.wait(10)
            preview = prune_checkpoints(memory, thread_id=thread, keep_last=1, dry_run=True)
            assert preview.checkpoints_deleted > 0
            with pytest.raises(ValueError, match="offline=True"):
                prune_checkpoints(memory, thread_id=thread, keep_last=1)
            assert list(memory.list(config)) == history
        finally:
            resume.set()
        assert future.result(timeout=10) == {"value": "fork!"}
    checkpoints = list(memory.list(config))
    ids = {cp.checkpoint["id"] for cp in checkpoints}
    parents = {cp.parent_config["configurable"]["checkpoint_id"] for cp in checkpoints if cp.parent_config}
    assert len(checkpoints) > len(history)
    assert base.checkpoint["id"] in parents
    assert parents <= ids


def test_persistent_subgraph_keeps_state_across_turns(store):
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, StateGraph

    memory, thread = store

    class State(TypedDict, total=False):
        count: int
        messages: list

    child = (
        StateGraph(State)
        .add_node("increment", lambda state: {"count": state.get("count", 0) + 1})
        .add_edge(START, "increment")
        .add_edge("increment", END)
        .compile(checkpointer=True)
    )
    graph = (
        StateGraph(State)
        .add_node("child", child, input_schema=TypedDict("ChildInput", {"messages": list}))
        .add_node("reply", lambda state: {"messages": [AIMessage(content="done")]})
        .add_edge(START, "child")
        .add_edge("child", "reply")
        .add_edge("reply", END)
        .compile(checkpointer=memory)
    )
    config = _config(thread)
    for count in (1, 2):
        assert graph.invoke({"messages": []}, config, durability="sync")["count"] == count
    state = list(memory.list(_config(thread, "child")))
    assert state
    opts = {"thread_id": thread, "keep_last": 1, "drop_subgraph_streams": True, "offline": True}
    assert prune_checkpoints(memory, **opts, dry_run=True).streams_dropped == 0
    assert prune_checkpoints(memory, **opts).checkpoints_deleted == 0
    assert list(memory.list(_config(thread, "child"))) == state
    assert graph.invoke({"messages": []}, config, durability="sync")["count"] == 3


def test_completed_task_subgraphs_prune_with_delta_messages(store):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, StateGraph

    memory, thread = store
    delta = pytest.importorskip("langgraph.channels.delta").DeltaChannel
    reducer = pytest.importorskip("langgraph.graph.message")._messages_delta_reducer
    channel = delta(reducer, list, snapshot_frequency=200)
    schema = TypedDict("State", {"messages": Annotated[list, channel]})  # noqa: UP013 - runtime channel definition
    child = (
        StateGraph(schema)
        .add_node("reply", lambda state: {"messages": [AIMessage(content="done")]})
        .add_edge(START, "reply")
        .add_edge("reply", END)
        .compile()
    )
    graph = (
        StateGraph(schema)
        .add_node("child", child)
        .add_edge(START, "child")
        .add_edge("child", END)
        .compile(checkpointer=memory)
    )
    config = _config(thread)
    for i in range(3):
        graph.invoke({"messages": [HumanMessage(content=f"turn {i}")]}, config, durability="sync")
    head = memory.get_tuple(config)
    assert "messages" not in head.checkpoint["channel_values"]
    before = graph.get_state(config)
    assert isinstance(before.values["messages"][-1], AIMessage)
    opts = {
        "thread_id": thread,
        "keep_last": 1,
        "drop_subgraph_streams": True,
        "messages_channel": channel,
        "offline": True,
    }
    preview = prune_checkpoints(memory, **opts, dry_run=True)
    assert preview.streams_dropped == 3
    assert preview.checkpoints_deleted > 0
    assert memory.get_tuple(config) == head
    actual = prune_checkpoints(memory, **opts)
    assert actual.streams_dropped == preview.streams_dropped
    assert actual.checkpoints_deleted == preview.checkpoints_deleted
    assert actual.writes_deleted == preview.writes_deleted
    assert len(list(memory.list(config))) == 1
    after = graph.get_state(config)
    assert (after.values, after.next, after.config) == (before.values, before.next, before.config)
    output = graph.invoke({"messages": [HumanMessage(content="next")]}, config, durability="sync")
    assert output["messages"][: len(before.values["messages"])] == before.values["messages"]
    assert [message.content for message in output["messages"][-2:]] == ["next", "done"]
