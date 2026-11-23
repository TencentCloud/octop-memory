"""Storage namespace naming conventions."""

import re
from pathlib import Path
from typing import Any

DEFAULT_SQLITE_DB_PATH = "~/.octop-memory/session.sqlite"


def normalize_namespace(namespace: str) -> str:
    """Return the SQLite table prefix or PostgreSQL row namespace."""
    return re.sub(r"[^a-z0-9_]", "_", namespace.lower())


def sqlite_database_path(config: dict[str, Any]) -> Path:
    """Resolve the same configured or default path used to open SQLite."""
    path = config.get("db_path", DEFAULT_SQLITE_DB_PATH)
    try:
        return Path(path).expanduser()
    except TypeError as exc:
        raise ValueError("SQLite db_path must be a filesystem path or ':memory:'") from exc
