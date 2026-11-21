"""Service configuration: listening address, control store and space storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from octop_memory._config_io import load_config


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


def _default_space(cfg: dict[str, Any], space_id: str, display_name: str = "") -> SpaceConfig:
    backend = str(cfg.get("backend") or "sqlite")
    if backend == "postgres":
        section = cfg.get("postgres") or {}
        return SpaceConfig(
            space_id=space_id,
            display_name=display_name or space_id,
            backend="postgres",
            backend_config={"dsn": section.get("dsn", "postgresql://localhost/octop_memory")},
        )
    section = cfg.get("sqlite") or {}
    base = str(section.get("db_path") or "~/.octop-memory/memory.db")
    # SQLite isolates spaces by file; keep the default single file untouched and
    # put every other space next to it.
    folder = Path(base).expanduser().parent
    return SpaceConfig(
        space_id=space_id,
        display_name=display_name or space_id,
        backend="sqlite",
        backend_config={"db_path": str(folder / "spaces" / f"{space_id}.db")},
    )


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


def resolve_space(cfg: dict[str, Any], space_id: str, display_name: str = "") -> SpaceConfig:
    spaces = cfg.get("spaces") or {}
    raw = spaces.get(space_id)
    if not isinstance(raw, dict):
        return _default_space(cfg, space_id, display_name)
    backend = str(raw.get("backend") or cfg.get("backend") or "sqlite")
    if backend == "postgres":
        section = {**(cfg.get("postgres") or {}), **(raw.get("postgres") or {})}
        return SpaceConfig(
            space_id=space_id,
            display_name=display_name or space_id,
            backend="postgres",
            backend_config={"dsn": section.get("dsn", "postgresql://localhost/octop_memory")},
        )
    section = {**(cfg.get("sqlite") or {}), **(raw.get("sqlite") or {})}
    base = str(section.get("db_path") or f"~/.octop-memory/spaces/{space_id}.db")
    return SpaceConfig(
        space_id=space_id,
        display_name=display_name or space_id,
        backend="sqlite",
        backend_config={"db_path": base},
    )
