"""Per-space Memory registry: open, cache and serialize access per space."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypeVar

from octop_memory._config_io import load_config
from octop_memory.core import Memory
from octop_memory.server.config import resolve_space
from octop_memory.server.control import ControlStore, Space

T = TypeVar("T")


class SpaceRegistry:
    """One Memory instance per space, guarded by a lock (Memory is single-threaded)."""

    def __init__(self, control: ControlStore, config: dict[str, Any] | None = None) -> None:
        self._control = control
        self._config = config or load_config()
        self._memories: dict[str, Memory] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def space(self, space_id: str) -> Space:
        space = self._control.get_space(space_id)
        if space is not None:
            return space
        fallback = resolve_space(self._config, space_id)
        return Space(
            space_id=fallback.space_id,
            display_name=fallback.display_name,
            backend=fallback.backend,
            backend_config=fallback.backend_config,
            created_by="local",
            created_at="",
            disabled_at=None,
        )

    def _open(self, space: Space) -> Memory:
        return Memory(
            namespace=space.space_id,
            backend=space.backend,
            backend_config=space.backend_config,
        )

    def call(self, space_id: str, fn: Callable[[Memory], T]) -> T:
        with self._guard:
            lock = self._locks.setdefault(space_id, threading.Lock())
        with lock:
            memory = self._memories.get(space_id)
            if memory is None:
                memory = self._open(self.space(space_id))
                self._memories[space_id] = memory
            return fn(memory)

    def close(self) -> None:
        with self._guard:
            memories = list(self._memories.values())
            self._memories.clear()
        for memory in memories:
            with suppress(Exception):
                memory.close()


__all__ = ["SpaceRegistry"]
