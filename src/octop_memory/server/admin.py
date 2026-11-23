"""Admin API used by the CLI (remote mode) and the Octop panel."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from octop_memory.server.control import ALL_SPACES, ControlStore, Session, SpaceConflictError, Token

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
    client_name: str | None = None
    scopes: list[str] | None = None
    disabled_clients: list[str] | None = None
    space_id: str | None = None


class TokenImportBody(BaseModel):
    token_hash: str
    token_hint: str
    client_name: str
    scopes: list[str] = ["read", "write"]
    disabled_clients: list[str] = []
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None
    client_stats: dict[str, Any] = {}
    octop_token_id: str = ""
    extra_space_ids: list[str] = []


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
        # Mutating a legacy multi-space token affects every grant it carries.
        for space_id, _ in control.allowed_spaces(token):
            require_space(session, space_id)
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
        if session.space_id != ALL_SPACES:
            require_space(session, body.space_id)
            # Octop checks that its bound space exists through this response.
            if control.get_space(body.space_id) is not None:
                raise HTTPException(status_code=409, detail="space already exists")
        require_space(session, ALL_SPACES)
        backend = body.backend or "sqlite"
        if body.backend_config:
            config = dict(body.backend_config)
        elif backend == "sqlite":
            config = {"db_path": f"~/.octop-memory/spaces/{body.space_id}.db"}
        elif backend == "postgres":
            config = {"dsn": "postgresql://localhost/octop_memory"}
        else:
            raise HTTPException(status_code=400, detail="unknown backend")
        try:
            space = control.create_space(
                body.space_id, body.display_name or body.space_id, backend, config, created_by="admin"
            )
        except SpaceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"space_id": space.space_id, "backend": space.backend}

    @app.get(f"{API_PREFIX}/spaces/{{space_id}}/tokens")
    def list_tokens(space_id: str, session: Session = Depends(require_session)) -> dict[str, Any]:
        require_space(session, space_id)
        return {
            "tokens": [
                {
                    "token_id": t.token_id,
                    "space_id": space_id,
                    "client_name": t.client_name,
                    "scopes": t.scopes,
                    "token_hint": t.token_hint,
                    "created_at": t.created_at,
                    "last_used_at": t.last_used_at,
                    "revoked_at": t.revoked_at,
                    "disabled_clients": t.disabled_clients,
                    "client_stats": t.client_stats,
                }
                for t in control.list_tokens(space_id)
            ]
        }

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
                disabled_clients=body.disabled_clients,
                space_id=body.space_id,
            )
        if updated is None:
            raise HTTPException(status_code=404, detail="token not found")
        return _token_payload(updated)

    @app.post(f"{API_PREFIX}/spaces/{{space_id}}/tokens/import")
    def import_token(
        space_id: str,
        body: TokenImportBody,
        session: Session = Depends(require_session),
    ) -> dict[str, Any]:
        target_spaces = dict.fromkeys([space_id, *body.extra_space_ids])
        for target in target_spaces:
            require_space(session, target)
        with control.write_transaction():
            for target in target_spaces:
                if control.get_space(target) is None:
                    raise HTTPException(status_code=404, detail="space not found")
            if control.token_by_hash(body.token_hash) is not None:
                raise HTTPException(status_code=409, detail="token already migrated")
            token = control.import_token(
                space_id=space_id,
                client_name=body.client_name,
                scopes=body.scopes,
                token_hash=body.token_hash,
                token_hint=body.token_hint,
                created_at=body.created_at,
                last_used_at=body.last_used_at,
                revoked_at=body.revoked_at,
                octop_token_id=body.octop_token_id,
                extra_space_ids=body.extra_space_ids,
                disabled_clients=body.disabled_clients,
                client_stats=body.client_stats,
            )
        return {"token_id": token.token_id, "space_id": space_id}

    @app.post(f"{API_PREFIX}/tokens/{{token_id}}/rotate")
    def rotate(token_id: str, session: Session = Depends(require_session)) -> dict[str, Any]:
        with control.write_transaction():
            managed_token(token_id, session)
            fresh, plaintext = control.rotate(token_id)
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
        bucket: dict[str, dict[str, Any]] = {}
        for token in control.list_tokens(space_id):
            for name, stats in token.client_stats.items():
                entry = bucket.setdefault(
                    name,
                    {"client": name, "count": 0, "last_seen": None, "tokens": []},
                )
                entry["count"] += int(stats.get("count") or 0)
                seen = [entry["last_seen"], stats.get("last_seen")]
                entry["last_seen"] = max([value for value in seen if value] or [""]) or None
                entry["tokens"].append(token.token_id)
        return {"clients": sorted(bucket.values(), key=lambda c: c["last_seen"] or "")}

    return app


def _token_payload(token: Any) -> dict[str, Any]:
    return {
        "token_id": token.token_id,
        "client_name": token.client_name,
        "scopes": token.scopes,
        "token_hint": token.token_hint,
        "space_id": token.space_id,
        "disabled_clients": token.disabled_clients,
        "client_stats": token.client_stats,
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
    }


__all__ = ["build_admin_app"]
