"""HTTP MCP surface: the same tools clients already use, scoped by space token."""

from __future__ import annotations

import asyncio
import hashlib
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


def _event_key(payload: dict[str, Any], event: dict[str, Any]) -> str:
    """Stable per-event id inside a space/session scope (client ``src_key`` first)."""
    src_key = str((payload or {}).get("src_key") or "").strip()
    if src_key:
        return f"src:{src_key}"
    digest = hashlib.sha256()
    digest.update(str(event.get("event_type") or "").encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(str(event.get("content") or "").encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(str(event.get("timestamp_iso") or "").encode("utf-8", "replace"))
    return f"hash:{digest.hexdigest()}"


def _raw_key(raw: Any) -> str:
    payload = raw.payload if isinstance(getattr(raw, "payload", None), dict) else {}
    stored = str(payload.get("octop_event_key") or "").strip()
    if stored:
        return stored
    return _event_key(
        payload,
        {
            "event_type": raw.event_type,
            "content": raw.content,
            "timestamp_iso": raw.timestamp.isoformat() if raw.timestamp else "",
        },
    )


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
            base_payload = {"source": source, "source_host": client, "caller": client}
            normalized: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in payload:
                body = item.get("content")
                if not isinstance(body, str) or not body.strip():
                    continue
                event: dict[str, Any] = {
                    "event_type": str(item.get("event_type") or "user_message"),
                    "content": body,
                    "payload": {**(item.get("payload") or {}), **base_payload},
                }
                timestamp_iso = item.get("timestamp_iso")
                if isinstance(timestamp_iso, str) and timestamp_iso:
                    event["timestamp_iso"] = timestamp_iso
                key = _event_key(event["payload"], event)
                if key in seen:
                    continue
                seen.add(key)
                event["payload"]["octop_event_key"] = key
                normalized.append(event)
            if not normalized:
                raise ValueError("events is empty after dropping blank entries")
            known = {
                _raw_key(raw)
                for raw in memory.list_raw(
                    session_id=effective_session, user=client, limit=5000
                )
            }
            fresh = [event for event in normalized if event["payload"]["octop_event_key"] not in known]
            duplicates = len(normalized) - len(fresh)
            if not fresh:
                return {
                    "marker": "[octop memory capture]",
                    "recorded": False,
                    "session_id": effective_session,
                    "accepted": 0,
                    "skipped_duplicates": duplicates,
                    "note": "all events already recorded for this session; retry is idempotent",
                }
            result = runtime.capture(
                {
                    "session_id": effective_session,
                    "thread_id": thread_id,
                    "user": client,
                    "host": source or "mcp",
                    "events": fresh,
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
                "skipped_duplicates": duplicates,
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
    async def memory_extract(
        session_id: str, promote: bool = False, max_candidates: int = 50
    ) -> dict[str, Any]:
        """Run L0 → L1/L2/L3 extraction for one session in this space."""
        _require("review")
        space = _current_space.get()

        def _run(memory: Any) -> dict[str, Any]:
            result = _runtime(memory).extract(
                {
                    "session_id": session_id,
                    "promote": promote,
                    "max_candidates": max_candidates,
                }
            )
            return dict(result or {})

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_promote(candidate_ids: list[str]) -> dict[str, Any]:
        """Promote review candidates through the 5-check worker."""
        _require("review")
        space = _current_space.get()
        decided_by = _current_client.get() or _current_token.get()

        def _run(memory: Any) -> dict[str, Any]:
            candidates = [
                candidate
                for candidate_id in candidate_ids
                if (candidate := memory.get_candidate(candidate_id)) is not None
            ]
            if not candidates:
                raise ValueError("no matching candidates")
            result = memory.promote_candidates(candidates)
            return {
                "requested": len(candidate_ids),
                "promoted": int(getattr(result, "promoted", 0) or 0),
                "merged": int(getattr(result, "merged", 0) or 0),
                "conflicts": int(getattr(result, "conflicts", 0) or 0),
                "needs_review": int(getattr(result, "needs_review", 0) or 0),
                "dropped": int(getattr(result, "dropped", 0) or 0),
                "decided_by": decided_by,
            }

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_reject(candidate_ids: list[str], reason: str = "") -> dict[str, Any]:
        """Reject review candidates (kept in the audit trail)."""
        _require("review")
        space = _current_space.get()
        decided_by = _current_client.get() or _current_token.get()

        def _run(memory: Any) -> dict[str, Any]:
            updated = 0
            for candidate_id in candidate_ids:
                if memory.update_candidate_status(
                    candidate_id,
                    status="rejected",
                    decided_by=decided_by,
                    promotion_reason=reason or None,
                ):
                    updated += 1
            return {"requested": len(candidate_ids), "rejected": updated}

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
