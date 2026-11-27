"""HTTP MCP surface: the same tools clients already use, scoped by space token."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from octop_memory.application.path_projection import raw_to_path
from octop_memory.application.runtime import MemoryRuntime
from octop_memory.core import Memory
from octop_memory.domain.capture import capture_event_key
from octop_memory.domain.codex_review import POLICY as CODEX_REVIEW_POLICY
from octop_memory.domain.raw_search import raw_search_text
from octop_memory.server.registry import SpaceRegistry
from octop_memory.types import CodexFact

_current_space: ContextVar[str] = ContextVar("octop_memory_space", default="")
_current_scopes: ContextVar[tuple[str, ...]] = ContextVar("octop_memory_scopes", default=())
_current_token: ContextVar[str] = ContextVar("octop_memory_token_id", default="")
_current_client_name: ContextVar[str] = ContextVar("octop_memory_client", default="")


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
        event_id: str | None = None,
        quote: Annotated[str | None, Field(min_length=1, max_length=200)] = None,
        kind: Literal["Preference", "Decision", "Fact"] | None = None,
        review_model: Annotated[str | None, Field(max_length=512)] = None,
        facts: Annotated[list[CodexFact] | None, Field(min_length=1, max_length=3)] = None,
    ) -> dict[str, Any]:
        """Submit up to three facts from one Codex turn together. Each assertion states one fact in the
        user's language and neutral third person, preserving scope and qualifiers. Write the assertion independently.
        Each quote is a short exact user excerpt
        (up to 200 characters) supporting that assertion. Omit the call when there are no durable facts.
        Single event_id/quote/kind arguments remain available to older clients."""
        _require("write")
        caller = _current_token.get()

        def _run(memory: Memory) -> dict[str, Any]:
            if facts is not None:
                if event_id is not None or quote is not None or kind is not None:
                    raise ValueError("cannot mix facts with legacy proposal arguments")
                if any(fact.target_id or fact.scope or fact.relation != "new" for fact in facts):
                    _require("read")
                candidates = memory.propose_codex_facts(facts, caller=caller, review_model=review_model)
                states = {c.status for c in candidates}
                status = next(iter(states)) if len(states) == 1 else "processed" if states else "skipped"
                return {"candidate_ids": [c.id for c in candidates], "status": status}
            if event_id is None or quote is None or kind is None:
                raise ValueError("provide facts or all legacy proposal arguments")
            candidate = memory.propose_quote(event_id, quote, caller=caller, kind=kind, review_model=review_model)
            return {"candidate_id": candidate.id, "status": candidate.status}

        return await asyncio.to_thread(registry.call, _current_space.get(), _run)

    @mcp.tool()
    async def memory_codex_terminal_identity() -> dict[str, str]:
        """Identify the calling read/write credential for Codex configuration sync."""
        _require("read")
        _require("write")
        return {"space_id": _current_space.get(), "token_id": _current_token.get()}

    @mcp.tool()
    async def memory_codex_related(
        event_ids: Annotated[list[str], Field(min_length=1, max_length=4)],
    ) -> dict[str, Any]:
        """Read classification rules and possible matching facts for your current captured Codex user evidence."""
        _require("read")
        _require("write")
        caller = _current_token.get()

        def read(memory: Memory) -> dict[str, Any]:
            texts = []
            for event_id in event_ids:
                event = memory.get_raw(event_id)
                if (
                    not event
                    or event.user != caller
                    or event.host != "codex"
                    or event.event_type != "user_message"
                    or event.payload.get("content_redacted")
                    or not event.payload.get("codex_review")
                ):
                    raise ValueError("Codex review evidence is unavailable")
                texts.append(event.content[:8000])
            return {"policy": CODEX_REVIEW_POLICY, "related": memory.related_codex_facts(caller, texts)}

        return await asyncio.to_thread(registry.call, _current_space.get(), read)

    @mcp.tool()
    async def memory_codex_review_evidence(
        session_id: str,
        turn_id: str,
        event_ids: Annotated[list[str], Field(min_length=1, max_length=4)],
        include_related: bool = False,
    ) -> dict[str, Any]:
        """Read this credential's eligible Codex messages for a delegated Octop review."""
        _require("read")
        _require("write")
        caller, space = _current_token.get(), _current_space.get()

        def read(memory: Memory) -> dict[str, Any]:
            messages = []
            for event_id in dict.fromkeys(event_ids):
                event = memory.get_raw(event_id)
                if (
                    event is None
                    or event.user != caller
                    or event.host != "codex"
                    or event.session_id != session_id
                    or event.payload.get("turn_id") != turn_id
                    or event.event_type != "user_message"
                    or event.payload.get("content_redacted")
                    or not event.payload.get("octop_model_review")
                ):
                    raise ValueError("Codex review evidence is unavailable")
                messages.append({"id": event.id, "text": event.content[:8000]})
            if include_related:
                return {
                    "token_id": caller,
                    "space_id": space,
                    "messages": messages,
                    "policy": CODEX_REVIEW_POLICY,
                    "related": memory.related_codex_facts(caller, [item["text"] for item in messages]),
                }
            return {"token_id": caller, "space_id": space, "messages": messages}

        return await asyncio.to_thread(registry.call, space, read)

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
        octop_model_review: bool = False,
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
                event["payload"].pop("octop_model_review", None)
                if (
                    source == "codex"
                    and not extract
                    and evidence_for_turn
                    and event["event_type"] == "user_message"
                    and (
                        event["payload"].get("turn_id") == evidence_for_turn
                        or (octop_model_review and event["payload"].get("turn_id"))
                    )
                ):
                    event["payload"]["octop_model_review" if octop_model_review else "codex_review"] = True
                timestamp_iso = item.get("timestamp_iso")
                if isinstance(timestamp_iso, str) and timestamp_iso:
                    event["timestamp_iso"] = timestamp_iso
                event["payload"]["octop_event_key"] = capture_event_key(event["payload"], event)
                normalized.append(event)
            if not normalized:
                raise ValueError("events is empty after dropping blank entries")
            result = runtime.capture(
                {
                    "session_id": effective_session,
                    "thread_id": thread_id,
                    "user": token_id,
                    "host": source or "mcp",
                    "events": normalized,
                    "evidence_for_turn": evidence_for_turn,
                    "octop_model_review": octop_model_review,
                },
                idempotent=True,
            )
            accepted = int(result["accepted"])
            if extract and accepted:
                with suppress(Exception):
                    run = runtime.extract({"session_id": effective_session, "promote": False})
                    if source == "codex" and run.get("llm_calls") and not run.get("failure_reason"):
                        memory.set_meta(
                            f"codex_terminal_run.{token_id}",
                            json.dumps(
                                {
                                    "mode": "server",
                                    "model": (registry.runtime_config.get("llm") or {}).get("model"),
                                    "timestamp": datetime.now(UTC).isoformat(),
                                }
                            ),
                        )
            if accepted and source == "codex" and (extract or evidence_for_turn):
                mode = "server" if extract else ("octop" if octop_model_review else "codex")
                memory.set_meta(
                    f"codex_terminal_mode.{token_id}",
                    json.dumps(
                        {
                            "mode": mode,
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    ),
                )
            return {
                "marker": "[octop memory capture]",
                "recorded": accepted > 0,
                "session_id": effective_session,
                "accepted": accepted,
                **({"user_event_ids": result["user_event_ids"]} if evidence_for_turn else {}),
                "skipped_duplicates": result["skipped_duplicates"],
                **(
                    {"note": "all events already recorded for this session; retry is idempotent"}
                    if result["skipped_duplicates"] == len(normalized)
                    else {}
                ),
                "skipped": {
                    key.removeprefix("skipped_"): value
                    for key, value in result.items()
                    if key.startswith("skipped_") and key != "skipped_duplicates" and value
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
