"""HTTP MCP surface: the same tools clients already use, scoped by space token."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from contextvars import ContextVar
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from octop_memory.application.path_projection import raw_to_path
from octop_memory.application.runtime import MemoryRuntime
from octop_memory.core import Memory
from octop_memory.domain.raw_search import raw_search_text
from octop_memory.server.registry import SpaceRegistry

_current_space: ContextVar[str] = ContextVar("octop_memory_space", default="")
_current_scopes: ContextVar[tuple[str, ...]] = ContextVar("octop_memory_scopes", default=())
_current_token: ContextVar[str] = ContextVar("octop_memory_token_id", default="")
_current_client_name: ContextVar[str] = ContextVar("octop_memory_client", default="")


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


def build_mcp(registry: SpaceRegistry) -> FastMCP:
    mcp = FastMCP(
        "octop-memory",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    mcp.settings.streamable_http_path = "/"

    def _runtime(memory: Any) -> MemoryRuntime:
        return MemoryRuntime(memory, config=registry.runtime_config)

    @mcp.tool()
    async def memory_search(
        query: str,
        corpus: Annotated[
            str,
            Field(description="Search atoms by default. Use raw for original events, or all, memory, wiki, sessions."),
        ] = "atom",
        max_results: Annotated[int, Field(ge=1)] = 8,
    ) -> dict[str, Any]:
        """Search this space and return paths that memory_get can read."""
        _require("read")
        space = _current_space.get()
        corpus = corpus.strip().lower() or "atom"
        if not query.strip():
            raise ValueError("memory_search: 'query' must be a non-empty string")

        def _run(memory: Memory) -> dict[str, Any]:
            if corpus == "raw":
                hits = [
                    {
                        "path": raw_to_path(event),
                        "score": None,
                        "snippet": raw_search_text(event.content)[:200],
                        "layer": "raw",
                        "role_hint": event.payload.get("role") or event.event_type,
                        "occurred_at": event.timestamp.isoformat(),
                        "source_id": event.id,
                    }
                    for event in memory.search_raw(query, limit=max_results)
                ]
                return {
                    "hits": hits,
                    "total": len(hits),
                    "empty_reason": None if hits else "no_matches",
                    "corpus": corpus,
                }

            atom_only = corpus == "atom"
            result = _runtime(memory).memory_search(
                {
                    "query": query,
                    "corpus": "memory" if atom_only else corpus,
                    "atomOnly": atom_only,
                    "maxResults": max_results,
                }
            )
            return {**result, "corpus": corpus}

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_get(
        path: str, from_line: int | None = None, lines: int | None = None, *, start: int | None = None
    ) -> dict[str, Any]:
        """Read a memory_search hit's path. Set start and lines to read a range of lines."""
        _require("read")
        space = _current_space.get()

        def _run(memory: Any) -> dict[str, Any]:
            result = _runtime(memory).memory_get(
                {"path": path, "from": start if start is not None else from_line, "lines": lines}
            )
            return {**result, "content": result["excerpt"]}

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_save(
        content: str, source: str, topic: str | None = None, supersedes: str | None = None
    ) -> dict[str, Any]:
        """Save an explicit durable fact into this space."""
        _require("write")
        space = _current_space.get()
        token_id = _current_token.get()
        metadata = {"source": source, "source_host": _current_client_name.get() or token_id, "caller": token_id}
        if supersedes:
            metadata["supersedes"] = supersedes

        def _run(memory: Any) -> dict[str, Any]:
            from octop_memory.service import MemoryService

            node = MemoryService(memory).save(content, topic=topic, metadata=metadata)
            return {"node_id": node.id, "content": node.content, "source": source}

        return await asyncio.to_thread(registry.call, space, _run)

    @mcp.tool()
    async def memory_propose(
        event_id: str,
        quote: Annotated[str, Field(min_length=1, max_length=200)],
        kind: Literal["Preference", "Decision", "Fact"],
    ) -> dict[str, Any]:
        """Propose an exact quote from your own captured user message for human review."""
        _require("write")
        caller = _current_token.get()

        def _run(memory: Memory) -> dict[str, Any]:
            candidate = memory.propose_quote(event_id, quote, caller=caller, kind=kind)
            return {"candidate_id": candidate.id, "status": candidate.status}

        return await asyncio.to_thread(registry.call, _current_space.get(), _run)

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
        evidence_for_turn: str | None = None,
        completed_review_turn: str | None = None,
    ) -> dict[str, Any]:
        """Write raw events (L0) parsed by a token_id; extraction stays opt-in."""
        _require("write")
        space = _current_space.get()
        token_id = _current_token.get()
        if completed_review_turn is not None:
            if (
                source != "codex"
                or not session_id
                or not completed_review_turn.strip()
                or extract
                or content
                or assistant
                or events is not None
                or evidence_for_turn
                or thread_id
            ):
                raise ValueError("A Codex review receipt requires only a session, completed turn and extract=false")
            if not dry_run:
                await asyncio.to_thread(
                    registry.call,
                    space,
                    lambda memory: memory.finish_codex_review(
                        session_id=session_id, turn_id=completed_review_turn, caller=token_id
                    ),
                )
            return {"recorded": False, "accepted": 0, "dry_run": dry_run, "session_id": session_id}
        effective_session = session_id or f"ext:{source or token_id}:{token_id}"
        base_payload = {"source": source, "source_host": _current_client_name.get() or token_id, "caller": token_id}
        payload: list[dict[str, Any]] = []
        if events is not None:
            if len(events) > 200:
                raise ValueError(f"[batch_too_large] events={len(events)} limit=200; send smaller batches")
            payload = [dict(event) for event in events]
        else:
            if content.strip():
                payload.append({"event_type": "user_message", "content": content, "payload": base_payload})
            if assistant:
                payload.append({"event_type": "assistant_message", "content": assistant, "payload": base_payload})
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
                event["payload"].pop("codex_review", None)
                if (
                    source == "codex"
                    and not extract
                    and evidence_for_turn
                    and event["event_type"] == "user_message"
                    and event["payload"].get("turn_id") == evidence_for_turn
                ):
                    event["payload"]["codex_review"] = True
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
            previous = memory.list_raw(session_id=effective_session, user=token_id, limit=5000)
            known = {_raw_key(raw) for raw in previous}
            evidence = {}
            if evidence_for_turn:
                evidence["user_event_ids"] = [
                    raw.id
                    for raw in reversed(previous)
                    if raw.event_type == "user_message"
                    and not raw.payload.get("content_redacted")
                    and raw.payload.get("turn_id") == evidence_for_turn
                    and _raw_key(raw) in seen
                ]
            fresh = [event for event in normalized if event["payload"]["octop_event_key"] not in known]
            duplicates = len(normalized) - len(fresh)
            if not fresh:
                return {
                    "marker": "[octop memory capture]",
                    "recorded": False,
                    "session_id": effective_session,
                    "accepted": 0,
                    **evidence,
                    "skipped_duplicates": duplicates,
                    "note": "all events already recorded for this session; retry is idempotent",
                }
            result = runtime.capture(
                {
                    "session_id": effective_session,
                    "thread_id": thread_id,
                    "user": token_id,
                    "host": source or "mcp",
                    "events": fresh,
                    "evidence_for_turn": evidence_for_turn,
                }
            )
            if extract:
                with suppress(Exception):
                    runtime.extract({"session_id": effective_session, "promote": False})
            accepted = int(result.get("accepted") or 0)
            if evidence_for_turn:
                evidence["user_event_ids"].extend(result.get("user_event_ids", []))
            return {
                "marker": "[octop memory capture]",
                "recorded": accepted > 0,
                "session_id": effective_session,
                "accepted": accepted,
                **evidence,
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
                memory.search_raw(query, limit=limit) if query else memory.list_raw(session_id=session_id, limit=limit)
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
    async def memory_extract(session_id: str, promote: bool = False, max_candidates: int = 50) -> dict[str, Any]:
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
        decided_by = _current_token.get()

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
        decided_by = _current_token.get()

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
                        "title": row.title,
                        "assertion": row.assertion,
                        "type": row.candidate_type,
                        "importance": row.importance,
                        "confidence": row.confidence,
                        "session_id": row.session_id,
                    }
                    for row in rows
                ],
                "count": len(rows),
            }

        return await asyncio.to_thread(registry.call, space, _run)

    return mcp


__all__ = [
    "_current_client_name",
    "_current_scopes",
    "_current_space",
    "_current_token",
    "build_mcp",
]
