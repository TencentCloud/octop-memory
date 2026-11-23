"""Storage namespace naming conventions."""

import re


def normalize_postgres_namespace(namespace: str) -> str:
    """Return the namespace value used to scope PostgreSQL rows."""
    return re.sub(r"[^a-z0-9_]", "_", namespace.lower())
