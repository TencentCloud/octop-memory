"""HTTP MCP surface: the same tools clients already use, scoped by space token."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from contextvars import ContextVar
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from octop_memory.application.runtime import MemoryRuntime
from octop_memory.server.control import ControlStore
from octop_memory.server.registry import SpaceRegistry

_current_space: ContextVar[str] = ContextVar("octop_memory_space", default="")
_current_scopes: ContextVar[tuple[str, ...]] = ContextVar("octop_memory_scopes", default=())
_current_token: ContextVar[str] = ContextVar("octop_memory_token_id", default="")
_current_client: ContextVar[str] = ContextVar("octop_memory_client", default="")


def _require(scope: str) -> None:
    if scope not in _current_scopes.get():
        raise PermissionError(f"token is missing required scope {scope!r}")


def build_mcp(registry: SpaceRegistry, control: ControlStore) -> FastMCP:
    mcp = FastMCP(
        "octop-memory",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    mcp.settings.streamable_http_path = "/"

    def _runtime(memory: Any) -> MemoryRuntime:
        return MemoryRuntime(memory)

    @mcp.tool()
    async def memory_search(
        query: str, corpus: str = "all", max_results: int = 8
    ) -> dict[str, Any]:
        """Search this space's memory (atoms, sessions, wiki)."""
        _require("read")
        space = _current_space.get()

        def _run(memory: Any) -> dict[str, Any]:
            return _runtime(memory).memory_search(
                {"query": query, "corpus": corpus, "maxResults": max_results}
            )

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_get(path: str, from_line: int | None = None, lines: int | None = None) -> dict[str, Any]:
        """Read one memory document by virtual path."""
        _require("read")
        space = _current_space.get()

        def _run(memory: Any) -> dict[str, Any]:
            params: dict[str, Any] = {"path": path}
            if from_line is not None:
                params["from"] = from_line
            if lines is not None:
                params["lines"] = lines
            return _runtime(memory).memory_get(params)

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_save(
        content: str, source: str, topic: str | None = None, supersedes: str | None = None
    ) -> dict[str, Any]:
        """Save an explicit durable fact into this space."""
        _require("write")
        space = _current_space.get()
        client = _current_client.get() or _current_token.get()
        metadata = {"source": source, "source_host": client, "caller": client}
        if supersedes:
            metadata["supersedes"] = supersedes

        def _run(memory: Any) -> dict[str, Any]:
            from octop_memory.service import MemoryService

            node = MemoryService(memory).save(content, topic=topic, metadata=metadata)
            return {"node_id": node.id, "content": node.content, "source": source}

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_capture(
        content: str = "",
        source: str = "",
        session_id: str | None = None,
        assistant: str | None = None,
        thread_id: str | None = None,
        dry_run: bool = False,
        extract: bool = True,
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Write raw events (L0) parsed by a client; extraction stays opt-in."""
        _require("write")
        space = _current_space.get()
        client = _current_client.get() or _current_token.get()
        effective_session = session_id or f"ext:{source or client}:{client}"
        payload: list[dict[str, Any]] = []
        if events is not None:
            if len(events) > 200:
                raise ValueError(
                    f"[batch_too_large] events={len(events)} limit=200; send smaller batches"
                )
            payload = [dict(event) for event in events]
        else:
            base = {"source": source, "source_host": client, "caller": client}
            if content.strip():
                payload.append({"event_type": "user_message", "content": content, "payload": base})
            if assistant:
                payload.append(
                    {"event_type": "assistant_message", "content": assistant, "payload": base}
                )
        if not payload:
            raise ValueError("content and assistant are both empty; nothing to record")
        if dry_run:
            return {
                "marker": "[octop memory capture]",
                "recorded": False,
                "dry_run": True,
                "session_id": effective_session,
                "events": payload,
            }

        def _run(memory: Any) -> dict[str, Any]:
            runtime = _runtime(memory)
            result = runtime.capture(
                {
                    "session_id": effective_session,
                    "thread_id": thread_id,
                    "user": client,
                    "host": source or "mcp",
                    "events": payload,
                }
            )
            if extract:
                with suppress(Exception):
                    runtime.extract({"session_id": effective_session, "promote": False})
            accepted = int(result.get("accepted") or 0)
            return {
                "marker": "[octop memory capture]",
                "recorded": accepted > 0,
                "session_id": effective_session,
                "accepted": accepted,
                "skipped": {
                    key.removeprefix("skipped_"): value
                    for key, value in result.items()
                    if key.startswith("skipped_") and value
                },
            }

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_raws(
        query: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List or FTS-search raw L0 events in this space."""
        _require("read")
        space = _current_space.get()

        def _run(memory: Any) -> dict[str, Any]:
            rows = (
                memory.search_raw(query, limit=limit)
                if query
                else memory.list_raw(session_id=session_id, limit=limit)
            )
            return {
                "events": [
                    {
                        "event_id": row.id,
                        "timestamp": row.timestamp.isoformat(),
                        "session_id": row.session_id,
                        "event_type": row.event_type,
                        "content": row.content,
                    }
                    for row in rows
                ],
                "count": len(rows),
            }

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_candidates(
        status: str | None = None, session_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """List review candidates in this space."""
        _require("read")
        space = _current_space.get()

        def _run(memory: Any) -> dict[str, Any]:
            rows = memory.list_candidates(status=status, limit=limit)
            return {
                "candidates": [
                    {
                        "candidate_id": row.id,
                        "status": row.status,
                        "content": row.content,
                    }
                    for row in rows
                ],
                "count": len(rows),
            }

        return await asyncio.to_thread(registry.call, space, _run)

    return mcp


__all__ = [
    "_current_client",
    "_current_scopes",
    "_current_space",
    "_current_token",
    "build_mcp",
]
