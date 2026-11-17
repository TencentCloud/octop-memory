"""Backend-independent checkpoint dependency planning."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# ID, parent ID, format version, stored channels, messages channel version exists.
type CheckpointRetentionRow = tuple[str, str | None, int, Iterable[str], bool]


def select_victim_ids(ids: list[str], *, keep_last: int | None, cutoff_id: str | None) -> list[str]:
    """Select from newest-first IDs. Always retain the newest checkpoint."""
    survivors = set(ids[: keep_last or 1])
    if cutoff_id is not None:
        survivors.update(cid for cid in ids if cid >= cutoff_id)
    return [cid for cid in ids if cid not in survivors]


def checkpoint_has_pending_work(checkpoint: Mapping[str, Any]) -> bool:
    values = checkpoint["channel_values"]
    return bool(
        any(name.startswith("branch:to:") for name in values)
        or "__start__" in values
        or values.get("__pregel_tasks")
        or values.get("__interrupt__")
        or checkpoint.get("pending_sends")
    )


def messages_end_with_final_reply(messages: Any) -> bool:
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    if isinstance(last, dict):
        return str(last.get("type") or "") in {"ai", "AIMessage", "AIMessageChunk"} and not last.get("tool_calls")
    return (
        type(last).__name__ in {"AIMessage", "AIMessageChunk"} or getattr(last, "type", None) == "ai"
    ) and not getattr(last, "tool_calls", None)


def _can_replay_channel(name: str) -> bool:
    # LangGraph uses these names for scheduling and task bookkeeping, not
    # DeltaChannel data. Consumed branch triggers routinely disappear from
    # channel_values while remaining in channel_versions.
    return not name.startswith("branch:to:") and name not in {
        "__start__",
        "__pregel_tasks",
        "__error__",
        "__interrupt__",
        "__resume__",
        "__return__",
        "__no_writes__",
    }


@dataclass
class CheckpointPrunePlan:
    victims: list[str]
    detach: list[str]
    message_snapshot_ids: list[str]


def plan_checkpoint_dependencies(
    checkpoints: Iterable[CheckpointRetentionRow],
    write_channels: Iterable[tuple[str, str]],
    candidates: list[str],
    *,
    snapshot_messages: bool = False,
) -> CheckpointPrunePlan:
    """Find safe cuts and message-only boundaries over an ordered parent graph."""
    if not candidates:
        return CheckpointPrunePlan([], [], [])
    candidate_ids = set(candidates)
    writes: dict[str, set[str]] = defaultdict(set)
    sends: set[str] = set()
    for cid, channel in write_channels:
        if channel == "__pregel_tasks":
            sends.add(cid)
        if _can_replay_channel(channel):
            writes[cid].add(channel)

    channels_through: dict[str, set[str]] = {}
    parents: dict[str, str | None] = {}
    needs_parent: set[str] = set()
    message_boundaries: set[str] = set()
    for cid, parent, version, stored, messages_versioned in checkpoints:
        if parent is not None and parent not in parents:
            raise ValueError("Missing or out-of-order checkpoint ancestor")
        values = {name for name in stored if _can_replay_channel(name)}
        inherited = channels_through[parent] if parent is not None else set()
        missing = inherited - values
        if (
            snapshot_messages
            and version == 4
            and missing == {"messages"}
            and messages_versioned
            and cid not in candidate_ids
            and parent in candidate_ids
        ):
            message_boundaries.add(cid)
        # Older readers can reconstruct pending sends from the parent's
        # writes. An unknown newer format may have additional dependencies.
        elif missing or (version < 4 and parent in sends) or version > 4:
            needs_parent.add(cid)
        parents[cid] = parent
        channels_through[cid] = inherited | values | writes.get(cid, set())

    keep = parents.keys() - candidate_ids
    for cid in reversed(parents):
        parent = parents[cid]
        if parent is not None and cid in keep and cid in needs_parent:
            keep.add(parent)
    victims = [cid for cid in candidates if cid not in keep]
    detach = [cid for cid in keep if parents[cid] is not None and parents[cid] not in keep]
    return CheckpointPrunePlan(victims, detach, sorted(message_boundaries.intersection(detach)))
