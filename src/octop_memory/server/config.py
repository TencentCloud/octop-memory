"""Service configuration: listening address, control store and space storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from octop_memory._config_io import load_config
from octop_memory.storage.backends.namespace import sqlite_database_path


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8790
    base_url: str = ""
    admin_enabled: bool = True


@dataclass(frozen=True)
class ControlConfig:
    backend: str = "sqlite"
    sqlite_path: str = "~/.octop-memory/control.db"
    postgres_dsn: str = "postgresql://localhost/octop_memory"
    postgres_schema: str = "octop_memory_control"


@dataclass(frozen=True)
class SpaceConfig:
    space_id: str
    display_name: str
    backend: str
    backend_config: dict[str, Any] = field(default_factory=dict)


def load_server_config(config: dict[str, Any] | None = None) -> tuple[ServerConfig, ControlConfig]:
    cfg = config or load_config()
    server = cfg.get("server") or {}
    control = cfg.get("control") or {}
    sqlite = control.get("sqlite") or {}
    postgres = control.get("postgres") or {}
    return (
        ServerConfig(
            host=str(server.get("host") or "127.0.0.1"),
            port=int(server.get("port") or 8790),
            base_url=str(server.get("base_url") or ""),
            admin_enabled=bool(server.get("admin_enabled", True)),
        ),
        ControlConfig(
            backend=str(control.get("backend") or "sqlite"),
            sqlite_path=str(sqlite.get("db_path") or "~/.octop-memory/control.db"),
            postgres_dsn=str(postgres.get("dsn") or "postgresql://localhost/octop_memory"),
            postgres_schema=str(postgres.get("schema") or "octop_memory_control"),
        ),
    )


def resolve_space(
    cfg: dict[str, Any],
    space_id: str,
    display_name: str = "",
    *,
    backend: str | None = None,
    backend_config: dict[str, Any] | None = None,
) -> SpaceConfig:
    raw = (cfg.get("spaces") or {}).get(space_id)
    space = raw if isinstance(raw, dict) else {}
    selected = backend or space.get("backend") or cfg.get("backend") or "sqlite"
    if selected not in ("sqlite", "postgres"):
        raise ValueError("unknown space storage backend")
    settings = {**(cfg.get(selected) or {}), **(space.get(selected) or {})}
    overrides = backend_config or {}
    if selected == "postgres":
        if "db_path" in overrides:
            raise ValueError("db_path requires the sqlite backend")
        config = {**settings, **overrides}
        config.setdefault("dsn", "postgresql://localhost/octop_memory")
        if not isinstance(config["dsn"], str):
            raise ValueError("PostgreSQL dsn must be a string")
    else:
        if overrides.keys() - {"db_path"}:
            raise ValueError("SQLite backend_config accepts only db_path")
        if isinstance(raw, dict):
            path = settings.get("db_path") or f"~/.octop-memory/spaces/{space_id}.db"
        else:
            base = Path(settings.get("db_path") or "~/.octop-memory/memory.db").expanduser()
            path = str(base.parent / "spaces" / f"{space_id}.db")
        db = sqlite_database_path({"db_path": overrides.get("db_path", path)})
        config = {"db_path": str(db) if str(db) == ":memory:" else str(db.resolve())}
    return SpaceConfig(space_id, display_name or space_id, selected, config)
