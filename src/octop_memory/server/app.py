"""ASGI app: MCP at ``/mcp`` (legacy ``/mcp/memory``) + admin API at ``/api/admin``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from contextvars import Token as CtxToken
from typing import Any

from starlette.types import Receive, Scope, Send

from octop_memory._config_io import load_config
from octop_memory.server.admin import build_admin_app
from octop_memory.server.config import load_server_config
from octop_memory.server.control import ControlStore
from octop_memory.server.mcp import (
    _current_client,
    _current_scopes,
    _current_space,
    _current_token,
    build_mcp,
)
from octop_memory.server.registry import SpaceRegistry

_current_touch: ContextVar[Any] = ContextVar("octop_memory_touch", default=None)


class TokenAuthMiddleware:
    """Resolve ``Authorization: Bearer`` to a space token before the MCP app."""

    def __init__(self, app: Any, control: ControlStore) -> None:
        self._app = app
        self._control = control

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        plaintext = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not plaintext:
            await _json(send, 401, {"error": "missing bearer token"})
            return
        token = await asyncio.to_thread(self._control.authenticate, plaintext)
        if token is None:
            await _json(send, 401, {"error": "invalid or revoked token"})
            return
        # Legacy multi-expert tokens keep access through grants: a request may
        # pick one of its granted spaces explicitly, otherwise the primary one.
        allowed = await asyncio.to_thread(self._control.allowed_spaces, token)
        requested = headers.get("x-octop-space", "").strip()
        # Backward compatibility: legacy Octop clients sent the expert id, and
        # each migrated expert lives in ``octop-expert-<agent_id>``.
        agent_hint = headers.get("x-octop-agent-id", "").strip()
        if not requested and agent_hint:
            requested = f"octop-expert-{agent_hint}"
        space_id, scopes = token.space_id, token.scopes
        if requested:
            match = next((item for item in allowed if item[0] == requested), None)
            if match is None:
                await _json(send, 403, {"error": "token has no grant for this space"})
                return
            space_id, scopes = match
        if await asyncio.to_thread(self._control.get_space, space_id) is None:
            await _json(send, 403, {"error": "token space is not registered"})
            return
        client = headers.get("x-octop-client", "").strip()[:64] or token.client_name
        if client and client in token.disabled_clients:
            await _json(
                send,
                403,
                {"error": f"client {client!r} is disabled for this token"},
            )
            return
        if client:
            await asyncio.to_thread(self._control.record_client, token.token_id, client)
        values: list[tuple[ContextVar[Any], CtxToken[Any]]] = [
            (_current_space, _current_space.set(space_id)),
            (_current_scopes, _current_scopes.set(tuple(scopes))),
            (_current_token, _current_token.set(token.token_id)),
            (_current_client, _current_client.set(client)),
        ]
        try:
            await self._app(scope, receive, send)
        finally:
            for var, ctx_token in values:
                var.reset(ctx_token)


async def _json(send: Send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_app(config: dict[str, Any] | None = None) -> Any:
    cfg = config or load_config()
    _server_cfg, control_cfg = load_server_config(cfg)
    control = ControlStore(control_cfg.sqlite_path)
    registry = SpaceRegistry(control, cfg)

    mcp = build_mcp(registry, control)
    inner = mcp.streamable_http_app()
    protected = TokenAuthMiddleware(inner, control)
    manager = getattr(mcp, "_session_manager", None)

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if manager is not None:
                await stack.enter_async_context(manager.run())
            try:
                yield
            finally:
                registry.close()
                control.close()

    app = build_admin_app(control, lifespan=lifespan)
    app.mount("/mcp", protected)
    app.mount("/mcp/memory", protected)
    app.state.control = control
    app.state.registry = registry
    app.state.mcp = mcp
    return app


__all__ = ["TokenAuthMiddleware", "build_app"]
