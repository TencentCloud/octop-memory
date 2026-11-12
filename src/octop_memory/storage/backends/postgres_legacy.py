"""Bring memory written before the 1.0 rename into the current schema.

harness-memory 0.9.6 to 0.9.11 kept the shared tables in schema
``harness_memory``. 1.0.0 renamed the constant to ``octop_memory`` without
moving any rows, so a namespace that predates the rename reads as empty.
Checkpoints are unaffected: their tables were created on ``public``.

A namespace whose current tables are still empty is copied on first open.
One that already has new rows is only reported by :func:`status`; the host
decides whether to :func:`merge` it. Both write a marker in ``meta`` so a
namespace is never copied twice — otherwise purging memory would bring the
old rows back on the next open. The ``harness_memory`` schema is shared by
every namespace and other installs, so it is never changed here.

Every function runs on the caller's cursor (``dict_row``) inside the
caller's transaction, under the DDL advisory lock.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from psycopg import sql

logger = logging.getLogger(__name__)

LEGACY_SCHEMA = "harness_memory"
CURRENT_SCHEMA = "octop_memory"
MARKER_KEY = "legacy_schema_migrated"

_warned: set[str] = set()


def _table_exists(cur: Any, schema: str, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS oid", (f"{schema}.{table}",))
    row = cur.fetchone()
    return row is not None and row["oid"] is not None


def _ident(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _marker(cur: Any, ns: str) -> str | None:
    cur.execute(
        sql.SQL("SELECT value FROM {} WHERE key = %s").format(_ident(CURRENT_SCHEMA, "meta")),
        (f"{ns}:{MARKER_KEY}",),
    )
    row = cur.fetchone()
    return str(row["value"]) if row else None


def _legacy_tables(cur: Any, tables: Sequence[str]) -> list[str]:
    return [table for table in tables if _table_exists(cur, LEGACY_SCHEMA, table)]


def _row_count(cur: Any, schema: str, table: str, ns: str) -> int:
    cur.execute(
        sql.SQL("SELECT count(*) AS n FROM {} WHERE namespace = %s").format(_ident(schema, table)),
        (ns,),
    )
    return int(cur.fetchone()["n"])


def _meta_like(ns: str) -> str:
    escaped = ns.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}:%"


def _legacy_meta_count(cur: Any, ns: str, *, new_only: bool) -> int:
    if not _table_exists(cur, LEGACY_SCHEMA, "meta"):
        return 0
    query = sql.SQL("SELECT count(*) AS n FROM {} s WHERE s.key LIKE %s AND s.key <> %s").format(
        _ident(LEGACY_SCHEMA, "meta")
    )
    if new_only:
        query += sql.SQL(" AND NOT EXISTS (SELECT 1 FROM {} d WHERE d.key = s.key)").format(
            _ident(CURRENT_SCHEMA, "meta")
        )
    cur.execute(query, (_meta_like(ns), f"{ns}:{MARKER_KEY}"))
    return int(cur.fetchone()["n"])


def _copy_columns(cur: Any, table: str) -> list[str]:
    """Writable columns of the current table that the legacy table also has."""
    query = """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND is_generated = 'NEVER'
        ORDER BY ordinal_position
    """
    cur.execute(query, (LEGACY_SCHEMA, table))
    legacy = {row["column_name"] for row in cur.fetchall()}
    cur.execute(query, (CURRENT_SCHEMA, table))
    return [
        row["column_name"]
        for row in cur.fetchall()
        if row["column_name"] in legacy and row["column_name"] != "namespace"
    ]


def _unique_keys(cur: Any, table: str) -> list[list[str]]:
    """Column sets of the current table's unique constraints, namespace excluded."""
    cur.execute(
        """
        SELECT array_agg(a.attname ORDER BY k.ord) AS cols
        FROM pg_index i
        CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        WHERE i.indrelid = to_regclass(%s) AND i.indisunique AND i.indpred IS NULL
        GROUP BY i.indexrelid
        """,
        (f"{CURRENT_SCHEMA}.{table}",),
    )
    return [[c for c in row["cols"] if c != "namespace"] for row in cur.fetchall()]


def _conflict_count(cur: Any, table: str, ns: str) -> int:
    """Legacy rows that ``ON CONFLICT DO NOTHING`` would skip."""
    keys = [key for key in _unique_keys(cur, table) if key]
    if not keys:
        return 0
    clauses = [
        sql.SQL("EXISTS (SELECT 1 FROM {} d WHERE d.namespace = %s AND {})").format(
            _ident(CURRENT_SCHEMA, table),
            sql.SQL(" AND ").join(sql.SQL("d.{0} = s.{0}").format(sql.Identifier(col)) for col in key),
        )
        for key in keys
    ]
    cur.execute(
        sql.SQL("SELECT count(*) AS n FROM {} s WHERE s.namespace = %s AND ({})").format(
            _ident(LEGACY_SCHEMA, table), sql.SQL(" OR ").join(clauses)
        ),
        (ns, *([ns] * len(clauses))),
    )
    return int(cur.fetchone()["n"])


def _copy_table(cur: Any, table: str, ns: str) -> int:
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in _copy_columns(cur, table))
    cur.execute(
        sql.SQL(
            "INSERT INTO {} (namespace, {cols}) SELECT namespace, {cols} FROM {} "
            "WHERE namespace = %s ON CONFLICT DO NOTHING"
        ).format(_ident(CURRENT_SCHEMA, table), _ident(LEGACY_SCHEMA, table), cols=cols),
        (ns,),
    )
    return max(int(cur.rowcount), 0)


def _copy(cur: Any, ns: str, tables: Sequence[str], *, mode: str) -> dict[str, Any]:
    cur.execute("SET CONSTRAINTS ALL DEFERRED")
    report: dict[str, dict[str, int]] = {}
    for table in _legacy_tables(cur, tables):
        total = _row_count(cur, LEGACY_SCHEMA, table, ns)
        if not total:
            continue
        inserted = _copy_table(cur, table, ns)
        report[table] = {"insert": inserted, "skip": total - inserted}
    meta = 0
    if _table_exists(cur, LEGACY_SCHEMA, "meta"):
        cur.execute(
            sql.SQL(
                "INSERT INTO {} (key, value) SELECT key, value FROM {} "
                "WHERE key LIKE %s AND key <> %s ON CONFLICT (key) DO NOTHING"
            ).format(_ident(CURRENT_SCHEMA, "meta"), _ident(LEGACY_SCHEMA, "meta")),
            (_meta_like(ns), f"{ns}:{MARKER_KEY}"),
        )
        meta = max(int(cur.rowcount), 0)
    marker = {
        "mode": mode,
        "at": datetime.now(UTC).isoformat(),
        "rows": sum(item["insert"] for item in report.values()),
        "skipped": sum(item["skip"] for item in report.values()),
    }
    cur.execute(
        sql.SQL("INSERT INTO {} (key, value) VALUES (%s, %s)").format(_ident(CURRENT_SCHEMA, "meta")),
        (f"{ns}:{MARKER_KEY}", json.dumps(marker)),
    )
    return {"tables": report, "meta_keys": meta}


def _source_rows(cur: Any, ns: str, tables: Sequence[str]) -> dict[str, int]:
    rows = {table: _row_count(cur, LEGACY_SCHEMA, table, ns) for table in _legacy_tables(cur, tables)}
    return {table: n for table, n in rows.items() if n}


def auto_migrate(cur: Any, ns: str, tables: Sequence[str]) -> dict[str, Any] | None:
    """Copy ``ns`` from the legacy schema when the current tables hold none of it.

    Returns the copy report, or ``None`` when nothing was copied.
    """
    if not _table_exists(cur, LEGACY_SCHEMA, "raw_events") or _marker(cur, ns) is not None:
        return None
    if not _source_rows(cur, ns, tables):
        return None
    if any(_row_count(cur, CURRENT_SCHEMA, table, ns) for table in tables):
        if ns not in _warned:
            _warned.add(ns)
            logger.warning(
                "namespace %r has memory from before the 1.0 rename in schema %r, but its "
                "current tables are not empty; not merging automatically "
                "(see Memory.legacy_schema_status / merge_legacy_schema)",
                ns,
                LEGACY_SCHEMA,
            )
        return None
    report = _copy(cur, ns, tables, mode="auto")
    logger.info(
        "migrated namespace %r from schema %r: %s, %d meta keys",
        ns,
        LEGACY_SCHEMA,
        {table: item["insert"] for table, item in report["tables"].items()},
        report["meta_keys"],
    )
    return report


def status(cur: Any, ns: str, tables: Sequence[str]) -> dict[str, Any]:
    """Read-only view of legacy rows still waiting for ``ns``."""
    if not _table_exists(cur, LEGACY_SCHEMA, "raw_events"):
        return {"supported": True, "pending": False, "migrated": None, "rows": {}, "meta_keys": 0}
    raw = _marker(cur, ns)
    if raw is not None:
        return {"supported": True, "pending": False, "migrated": json.loads(raw), "rows": {}, "meta_keys": 0}
    rows = _source_rows(cur, ns, tables)
    return {
        "supported": True,
        "pending": bool(rows),
        "migrated": None,
        "rows": rows,
        "meta_keys": _legacy_meta_count(cur, ns, new_only=False) if rows else 0,
    }


def merge(cur: Any, ns: str, tables: Sequence[str], *, dry_run: bool) -> dict[str, Any]:
    """Merge legacy rows of ``ns`` into its current, non-empty tables.

    Rows whose primary key or another unique key already exists are skipped.
    Similar entities or topic branches can end up twice; later memory
    maintenance consolidates them. ``dry_run`` reports the same counts
    without writing.
    """
    if not _table_exists(cur, LEGACY_SCHEMA, "raw_events"):
        return {"status": "nothing_to_merge", "tables": {}, "meta_keys": 0}
    if _marker(cur, ns) is not None:
        return {"status": "already_migrated", "tables": {}, "meta_keys": 0}
    rows = _source_rows(cur, ns, tables)
    if not rows:
        return {"status": "nothing_to_merge", "tables": {}, "meta_keys": 0}
    if dry_run:
        report: dict[str, dict[str, int]] = {}
        for table, total in rows.items():
            skip = _conflict_count(cur, table, ns)
            report[table] = {"insert": total - skip, "skip": skip}
        return {
            "status": "dry_run",
            "tables": report,
            "meta_keys": _legacy_meta_count(cur, ns, new_only=True),
        }
    result = _copy(cur, ns, tables, mode="merge")
    logger.info("merged namespace %r from schema %r: %s", ns, LEGACY_SCHEMA, result)
    return {"status": "merged", **result}
