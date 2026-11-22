"""``octop-memory serve|space|token|auth`` — standalone service management."""

from __future__ import annotations

import json

import click

from octop_memory.server.config import load_server_config
from octop_memory.server.control import ControlStore


def _control() -> ControlStore:
    _server, control_cfg = load_server_config()
    return ControlStore(control_cfg.sqlite_path)


@click.group("space")
def space_group() -> None:
    """Manage memory spaces."""


@space_group.command("create")
@click.argument("space_id")
@click.option("--name", default="", help="Display name.")
@click.option("--backend", type=click.Choice(["sqlite", "postgres"]), default="sqlite")
@click.option("--dsn", default=None, help="PostgreSQL DSN (backend=postgres).")
def space_create(space_id: str, name: str, backend: str, dsn: str | None) -> None:
    control = _control()
    if control.get_space(space_id):
        raise click.ClickException(f"space {space_id!r} already exists")
    if backend == "postgres":
        cfg = {"dsn": dsn or "postgresql://localhost/octop_memory"}
    else:
        cfg = {"db_path": f"~/.octop-memory/spaces/{space_id}.db"}
    control.create_space(space_id, name or space_id, backend, cfg, created_by="cli")
    click.echo(json.dumps({"space_id": space_id, "backend": backend}))


@space_group.command("list")
def space_list() -> None:
    control = _control()
    click.echo(
        json.dumps(
            [
                {"space_id": s.space_id, "display_name": s.display_name, "backend": s.backend}
                for s in control.list_spaces()
            ],
            ensure_ascii=False,
        )
    )


@click.group("token")
def token_group() -> None:
    """Manage space tokens."""


@token_group.command("create")
@click.option("--space", "space_id", required=True)
@click.option("--name", "client_name", required=True)
@click.option("--scope", "scopes", default="read,write")
def token_create(space_id: str, client_name: str, scopes: str) -> None:
    control = _control()
    if control.get_space(space_id) is None:
        raise click.ClickException(f"space {space_id!r} not found")
    token, plaintext = control.create_token(space_id, client_name, scopes.split(","))
    click.echo(
        json.dumps(
            {
                "token_id": token.token_id,
                "space_id": space_id,
                "client_name": client_name,
                "scopes": token.scopes,
                "token": plaintext,
            }
        )
    )


@token_group.command("list")
@click.option("--space", "space_id", required=True)
def token_list(space_id: str) -> None:
    control = _control()
    click.echo(
        json.dumps(
            [
                {
                    "token_id": t.token_id,
                    "client_name": t.client_name,
                    "scopes": t.scopes,
                    "token_hint": t.token_hint,
                    "last_used_at": t.last_used_at,
                    "revoked_at": t.revoked_at,
                }
                for t in control.list_tokens(space_id)
            ],
            ensure_ascii=False,
        )
    )


@token_group.command("rotate")
@click.argument("token_id")
def token_rotate(token_id: str) -> None:
    control = _control()
    token, plaintext = control.rotate(token_id)
    click.echo(json.dumps({"token_id": token_id, "token": plaintext, "token_hint": token.token_hint}))


@token_group.command("revoke")
@click.argument("token_id")
def token_revoke(token_id: str) -> None:
    control = _control()
    click.echo(json.dumps({"revoked": control.revoke(token_id)}))


@click.group("auth")
def auth_group() -> None:
    """One-time admin codes for the Octop panel."""


@auth_group.command("code")
@click.option("--space", "space_id", default=None)
@click.option("--all", "all_spaces", is_flag=True, help="Manage every space (terminal access).")
@click.option("--ttl", "ttl", default=600, type=int, help="Code lifetime in seconds.")
def auth_code(space_id: str | None, all_spaces: bool, ttl: int) -> None:
    control = _control()
    if all_spaces:
        space_id = "*"
    if not space_id:
        raise click.ClickException("pass --space <id> or --all")
    if space_id != "*" and control.get_space(space_id) is None:
        raise click.ClickException(f"space {space_id!r} not found")
    click.echo(json.dumps({"code": control.create_admin_code(space_id, ttl), "space_id": space_id}))


@click.group("control")
def control_group() -> None:
    """Optional maintenance for the control store."""


@control_group.command("cleanup-groups")
@click.option("--dry-run", is_flag=True)
def cleanup_groups(dry_run: bool) -> None:
    """Drop access-group leftovers from an older control DB."""
    control = _control()
    click.echo(
        json.dumps(control.cleanup_legacy_groups(dry_run=dry_run), ensure_ascii=False)
    )


@click.command("serve")
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
def serve(host: str | None, port: int | None) -> None:
    """Run the MCP + admin HTTP service."""
    import uvicorn

    from octop_memory.server.app import build_app

    server_cfg, _control_cfg = load_server_config()
    uvicorn.run(
        build_app(),
        host=host or server_cfg.host,
        port=port or server_cfg.port,
        log_level="info",
    )


__all__ = ["auth_group", "control_group", "serve", "space_group", "token_group"]
