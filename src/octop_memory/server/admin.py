"""Admin API used by the CLI (remote mode) and the Octop panel."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from octop_memory.server.control import ALL_SPACES, ControlStore, Session, SpaceConflictError, Token
from octop_memory.server.registry import SpaceRegistry

API_PREFIX = "/api/admin"


class SpaceCreate(BaseModel):
    space_id: str
    display_name: str = ""
    backend: str | None = None
    backend_config: dict[str, Any] | None = None


class TokenCreate(BaseModel):
    client_name: str
    scopes: list[str] = ["read", "write"]


class TokenUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: str | None = None
    scopes: list[str] | None = None
    space_id: str | None = None


class CodeExchange(BaseModel):
    code: str


def _bearer(request: Request) -> str:
    auth = str(request.headers.get("authorization", "") or "")
    if auth.lower().startswith("bearer "):
        return str(auth[7:].strip())
    return ""


def build_admin_app(control: ControlStore, lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="octop-memory admin", docs_url=None, redoc_url=None, lifespan=lifespan)

    def require_session(request: Request) -> Session:
        session = control.verify_session(_bearer(request))
        if session is None or "admin" not in session.scopes:
            raise HTTPException(status_code=401, detail="admin session required")
        return session

    def require_space(session: Session, space_id: str) -> None:
        if session.space_id not in {ALL_SPACES, space_id}:
            raise HTTPException(status_code=403, detail="session is bound to another space")

    def managed_token(token_id: str, session: Session) -> Token:
        token = control.get_token(token_id)
        if token is None:
            raise HTTPException(status_code=404, detail="token not found")
        require_space(session, token.space_id)
        return token

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "spaces": len(control.list_spaces())}

    @app.post(f"{API_PREFIX}/session/exchange")
    def exchange(body: CodeExchange) -> dict[str, Any]:
        try:
            plaintext, session = control.redeem_admin_code(body.code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "session_token": plaintext,
            "space_id": session.space_id,
            "scopes": session.scopes,
            "expires_at": session.expires_at,
        }

    @app.get(f"{API_PREFIX}/session")
    def current_session(session: Session = Depends(require_session)) -> dict[str, Any]:
        return {"space_id": session.space_id, "scopes": session.scopes, "expires_at": session.expires_at}

    @app.get(f"{API_PREFIX}/spaces")
    def list_spaces(session: Session = Depends(require_session)) -> dict[str, Any]:
        if session.space_id == ALL_SPACES:
            spaces = control.list_spaces()
        else:
            space = control.get_space(session.space_id)
            spaces = [space] if space is not None else []
        return {
            "spaces": [{"space_id": s.space_id, "display_name": s.display_name, "backend": s.backend} for s in spaces]
        }

    @app.post(f"{API_PREFIX}/spaces")
    def create_space(body: SpaceCreate, session: Session = Depends(require_session)) -> dict[str, Any]:
        require_space(session, body.space_id)
        try:
            with control.write_transaction():
                if session.space_id != ALL_SPACES and control.get_space(body.space_id) is None:
                    raise HTTPException(status_code=403, detail="creating a space requires a global admin session")
                space = control.create_space(
                    body.space_id, body.display_name, body.backend, body.backend_config, created_by="admin"
                )
        except SpaceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"space_id": space.space_id, "backend": space.backend}

    @app.get(f"{API_PREFIX}/spaces/{{space_id}}/tokens")
    def list_tokens(space_id: str, session: Session = Depends(require_session)) -> dict[str, Any]:
        require_space(session, space_id)
        return {"tokens": [_token_payload(t) for t in control.list_tokens(space_id)]}

    @app.post(f"{API_PREFIX}/spaces/{{space_id}}/tokens")
    def create_token(space_id: str, body: TokenCreate, session: Session = Depends(require_session)) -> dict[str, Any]:
        require_space(session, space_id)
        if control.get_space(space_id) is None:
            raise HTTPException(status_code=404, detail="space not found")
        token, plaintext = control.create_token(space_id, body.client_name, body.scopes)
        return {
            "token_id": token.token_id,
            "client_name": token.client_name,
            "scopes": token.scopes,
            "token": plaintext,
            "token_hint": token.token_hint,
        }

    @app.get(f"{API_PREFIX}/spaces/{{space_id}}/tokens/{{token_id}}/codex-status")
    def codex_status(
        space_id: str, token_id: str, request: Request, session: Session = Depends(require_session)
    ) -> dict[str, Any]:
        require_space(session, space_id)
        if managed_token(token_id, session).space_id != space_id:
            raise HTTPException(status_code=404, detail="token not found in space")

        registry: SpaceRegistry = request.app.state.registry
        return registry.call(space_id, lambda memory: memory.get_codex_terminal_status(token_id))

    @app.patch(f"{API_PREFIX}/tokens/{{token_id}}")
    def patch_token(token_id: str, body: TokenUpdate, session: Session = Depends(require_session)) -> dict[str, Any]:
        with control.write_transaction():
            token = managed_token(token_id, session)
            if body.space_id is not None and body.space_id != token.space_id:
                require_space(session, body.space_id)
                if control.get_space(body.space_id) is None:
                    raise HTTPException(status_code=404, detail="target space not found")
            updated = control.update_token(
                token_id,
                client_name=body.client_name,
                scopes=body.scopes,
                space_id=body.space_id,
            )
        if updated is None:
            raise HTTPException(status_code=404, detail="token not found")
        return _token_payload(updated)

    @app.post(f"{API_PREFIX}/tokens/{{token_id}}/rotate")
    def rotate(token_id: str, session: Session = Depends(require_session)) -> dict[str, Any]:
        with control.write_transaction():
            managed_token(token_id, session)
            try:
                fresh, plaintext = control.rotate(token_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "token_id": fresh.token_id,
            "client_name": fresh.client_name,
            "token": plaintext,
            "token_hint": fresh.token_hint,
        }

    @app.delete(f"{API_PREFIX}/tokens/{{token_id}}")
    def revoke(token_id: str, session: Session = Depends(require_session)) -> dict[str, Any]:
        with control.write_transaction():
            managed_token(token_id, session)
            return {"revoked": control.revoke(token_id)}

    @app.get(f"{API_PREFIX}/spaces/{{space_id}}/clients")
    def clients(space_id: str, session: Session = Depends(require_session)) -> dict[str, Any]:
        require_space(session, space_id)
        tokens = sorted(control.list_tokens(space_id), key=lambda token: token.last_used_at or "", reverse=True)
        return {
            "clients": [
                {
                    "client": token.client_name,
                    "token_id": token.token_id,
                    "tokens": [token.token_id],
                    "count": token.request_count,
                    "last_seen": token.last_used_at,
                    "revoked_at": token.revoked_at,
                }
                for token in tokens
            ]
        }

    return app


def _token_payload(token: Token) -> dict[str, Any]:
    return {
        "token_id": token.token_id,
        "client_name": token.client_name,
        "scopes": token.scopes,
        "token_hint": token.token_hint,
        "space_id": token.space_id,
        "client_stats": {
            token.client_name: {
                "first_seen": token.first_used_at,
                "last_seen": token.last_used_at,
                "count": token.request_count,
            }
        }
        if token.request_count
        else {},
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
    }


__all__ = ["build_admin_app"]
