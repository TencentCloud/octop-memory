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

Copies, merges and purges of one namespace serialize on a per-namespace
advisory lock (:func:`lock_namespace`), never the database-wide DDL lock,
so they cannot hold up other agents' startup. They do not stop ordinary
writes: a new-version writer in another process can land rows during an
automatic copy, which then simply ends up merged. A 0.9.x writer still using
the old schema after the copy is detected (``legacy_grew``) but not merged;
stop old writers before upgrading.

Every function runs on the caller's cursor (``dict_row``) inside the
caller's transaction.
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

# Two-key advisory locks live apart from single-bigint ones (DDL lock, others).
_LOCK_CLASS = 0x686D  # "hm"
_LOCK_TIMEOUT = "5s"
_BLOCKED_SAMPLE = 20

_warned: set[str] = set()


def _ident(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _table_exists(cur: Any, schema: str, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS oid", (f"{schema}.{table}",))
    row = cur.fetchone()
    return row is not None and row["oid"] is not None


def legacy_present(cur: Any) -> bool:
    """Whether the database still has the pre-1.0 shared tables (with a ``namespace`` column)."""
    cur.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_schema = %s AND table_name = 'raw_events' AND column_name = 'namespace'""",
        (LEGACY_SCHEMA,),
    )
    return cur.fetchone() is not None


def lock_namespace(cur: Any, ns: str) -> None:
    """Serialize copy, merge and purge of ``ns`` until the transaction ends.

    Waits at most ``_LOCK_TIMEOUT`` for the lock, then restores the caller's
    ``lock_timeout`` so an enclosing transaction keeps its own setting.
    """
    cur.execute("SELECT current_setting('lock_timeout') AS v")
    previous = str(cur.fetchone()["v"])
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (_LOCK_TIMEOUT,))
    cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_LOCK_CLASS, ns))
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (previous,))


def _marker(cur: Any, ns: str) -> dict[str, Any] | None:
    cur.execute(
        sql.SQL("SELECT value FROM {} WHERE key = %s").format(_ident(CURRENT_SCHEMA, "meta")),
        (f"{ns}:{MARKER_KEY}",),
    )
    row = cur.fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row["value"]))
    except json.JSONDecodeError:
        value = None
    return value if isinstance(value, dict) else {"mode": "unknown"}


def _write_marker(cur: Any, ns: str, marker: dict[str, Any]) -> None:
    cur.execute(
        sql.SQL(
            "INSERT INTO {} (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        ).format(_ident(CURRENT_SCHEMA, "meta")),
        (f"{ns}:{MARKER_KEY}", json.dumps(marker)),
    )


def _legacy_tables(cur: Any, tables: Sequence[str]) -> list[str]:
    return [table for table in tables if _table_exists(cur, LEGACY_SCHEMA, table)]


def _row_count(cur: Any, schema: str, table: str, ns: str) -> int:
    cur.execute(
        sql.SQL("SELECT count(*) AS n FROM {} WHERE namespace = %s").format(_ident(schema, table)),
        (ns,),
    )
    return int(cur.fetchone()["n"])


def _source_rows(cur: Any, ns: str, tables: Sequence[str]) -> dict[str, int]:
    rows = {table: _row_count(cur, LEGACY_SCHEMA, table, ns) for table in _legacy_tables(cur, tables)}
    return {table: n for table, n in rows.items() if n}


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


def _unique_keys(cur: Any, table: str) -> tuple[list[list[str]], bool, bool]:
    """Primary/unique constraint columns (namespace excluded), whether counts are exact,
    and whether a deferrable one exists.

    Counting matches ``ON CONFLICT DO NOTHING`` only for plain constraints that
    include ``namespace``. Any other unique index (partial, expression,
    ``NULLS NOT DISTINCT``, not namespace-scoped) makes the preview an
    estimate. ``ON CONFLICT`` rejects deferrable unique constraints outright.
    """
    regclass = f"{CURRENT_SCHEMA}.{table}"
    cur.execute(
        """
        SELECT array_agg(a.attname ORDER BY k.ord) AS cols, bool_or(c.condeferrable) AS deferrable
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.conrelid = to_regclass(%s) AND c.contype IN ('p', 'u')
        GROUP BY c.oid
        """,
        (regclass,),
    )
    found = cur.fetchall()
    constraints = [list(row["cols"]) for row in found]
    deferrable = any(row["deferrable"] for row in found)
    cur.execute(
        """
        SELECT count(*) AS n FROM pg_index i
        WHERE i.indrelid = to_regclass(%s) AND i.indisunique
          AND (NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
               OR pg_get_indexdef(i.indexrelid) ILIKE '%%NULLS NOT DISTINCT%%')
        """,
        (regclass,),
    )
    exact = int(cur.fetchone()["n"]) == 0 and all("namespace" in cols for cols in constraints)
    keys = [[c for c in cols if c != "namespace"] for cols in constraints]
    return [key for key in keys if key], exact, deferrable


def _conflict(cur: Any, table: str, alias: str) -> tuple[sql.Composable, int]:
    """SQL condition: legacy row ``alias`` collides with a current row of the same namespace.

    Returns the condition and how many ``%s`` namespace parameters it takes.
    """
    keys, _exact, _deferrable = _unique_keys(cur, table)
    if not keys:
        return sql.SQL("FALSE"), 0
    clauses = [
        sql.SQL("EXISTS (SELECT 1 FROM {} d WHERE d.namespace = %s AND {})").format(
            _ident(CURRENT_SCHEMA, table),
            sql.SQL(" AND ").join(
                sql.SQL("d.{0} = {1}.{0}").format(sql.Identifier(col), sql.Identifier(alias)) for col in key
            ),
        )
        for key in keys
    ]
    return sql.SQL("({})").format(sql.SQL(" OR ").join(clauses)), len(clauses)


def _plan(cur: Any, ns: str, rows: dict[str, int]) -> dict[str, Any]:
    tables: dict[str, dict[str, int]] = {}
    exact = True
    deferrable: list[str] = []
    for table, total in rows.items():
        _keys, table_exact, table_deferrable = _unique_keys(cur, table)
        exact = exact and table_exact
        if table_deferrable:
            deferrable.append(table)
        cond, n = _conflict(cur, table, "s")
        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {} s WHERE s.namespace = %s AND {}").format(
                _ident(LEGACY_SCHEMA, table), cond
            ),
            (ns, *([ns] * n)),
        )
        skip = int(cur.fetchone()["n"])
        tables[table] = {"insert": total - skip, "skip": skip}
    return {
        "tables": tables,
        "exact": exact and not deferrable,
        "blocked": _blocked_nodes(cur, ns, rows),
        "deferrable": deferrable,
    }


def _blocked_nodes(cur: Any, ns: str, rows: dict[str, int]) -> list[str]:
    """Legacy nodes that would be copied while their parent is neither present nor copied.

    Their parent was skipped by a key conflict (e.g. the same ``atom_id`` under a
    different node id), so the copy would break the ``parent_id`` foreign key.
    """
    if "memory_nodes" not in rows:
        return []
    child_cond, n_child = _conflict(cur, "memory_nodes", "s")
    parent_cond, n_parent = _conflict(cur, "memory_nodes", "p")
    cur.execute(
        sql.SQL(
            "SELECT s.id FROM {legacy} s WHERE s.namespace = %s AND s.parent_id IS NOT NULL "
            "AND NOT {child} "
            "AND NOT EXISTS (SELECT 1 FROM {current} d WHERE d.namespace = %s AND d.id = s.parent_id) "
            "AND NOT EXISTS (SELECT 1 FROM {legacy} p WHERE p.namespace = %s "
            "AND p.id = s.parent_id AND NOT {parent}) "
            "ORDER BY s.id LIMIT %s"
        ).format(
            legacy=_ident(LEGACY_SCHEMA, "memory_nodes"),
            current=_ident(CURRENT_SCHEMA, "memory_nodes"),
            child=child_cond,
            parent=parent_cond,
        ),
        (ns, *([ns] * n_child), ns, ns, *([ns] * n_parent), _BLOCKED_SAMPLE),
    )
    return [str(row["id"]) for row in cur.fetchall()]


def _copy_table(cur: Any, table: str, ns: str) -> tuple[int, int]:
    """Copy one table; returns ``(source rows, inserted rows)`` from one statement's snapshot."""
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in _copy_columns(cur, table))
    cur.execute(
        sql.SQL(
            "WITH src AS (SELECT namespace, {cols} FROM {legacy} WHERE namespace = %s), "
            "ins AS (INSERT INTO {current} (namespace, {cols}) SELECT namespace, {cols} FROM src "
            "ON CONFLICT DO NOTHING RETURNING 1) "
            "SELECT (SELECT count(*) FROM src) AS total, (SELECT count(*) FROM ins) AS inserted"
        ).format(
            cols=cols,
            legacy=_ident(LEGACY_SCHEMA, table),
            current=_ident(CURRENT_SCHEMA, table),
        ),
        (ns,),
    )
    row = cur.fetchone()
    return int(row["total"]), int(row["inserted"])


def _copy(cur: Any, ns: str, tables: Sequence[str], *, mode: str) -> dict[str, Any]:
    """Copy every legacy table of ``ns`` and write the marker.

    ``source_rows`` in the marker is what each copy statement actually saw,
    so ``legacy_grew`` later measures net growth since that snapshot.
    """
    cur.execute("SET CONSTRAINTS ALL DEFERRED")
    report: dict[str, dict[str, int]] = {}
    for table in _legacy_tables(cur, tables):
        total, inserted = _copy_table(cur, table, ns)
        if total:
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
    _write_marker(
        cur,
        ns,
        {
            "mode": mode,
            "at": datetime.now(UTC).isoformat(),
            "rows": sum(item["insert"] for item in report.values()),
            "skipped": sum(item["skip"] for item in report.values()),
            "source_rows": {table: item["insert"] + item["skip"] for table, item in report.items()},
        },
    )
    return {"tables": report, "meta_keys": meta}


def auto_migrate(cur: Any, ns: str, tables: Sequence[str]) -> dict[str, Any] | None:
    """Copy ``ns`` from the legacy schema when the current tables hold none of it.

    Call under :func:`lock_namespace`. Returns the copy report, or ``None``
    when nothing was copied.
    """
    if _marker(cur, ns) is not None:
        return None
    rows = _source_rows(cur, ns, tables)
    if not rows:
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


def mark_purged(cur: Any, ns: str) -> None:
    """Record that ``ns`` was purged, so its legacy rows are never copied back.

    Call under :func:`lock_namespace`, in the purge's transaction, whether or
    not the legacy tables are visible: a role may only be granted them later.
    An earlier copy's marker is kept under ``previous``.
    """
    marker: dict[str, Any] = {"mode": "purged", "at": datetime.now(UTC).isoformat()}
    previous = _marker(cur, ns)
    if previous is not None:
        marker["previous"] = previous.get("previous", previous) if previous.get("mode") == "purged" else previous
    _write_marker(cur, ns, marker)


def status(cur: Any, ns: str, tables: Sequence[str]) -> dict[str, Any]:
    """Read-only view of legacy rows of ``ns``.

    ``migrated``: the marker (copied automatically, merged, or purged), read
    first so it stays visible even after the legacy schema is archived.
    ``pending``: rows exist and were never copied or discarded.
    ``legacy_grew``: per-table net growth of the legacy rows since the copy's
    snapshot, i.e. a 0.9.x writer kept writing; those rows are not merged.
    Replacing rows one for one is not detected.
    """
    result: dict[str, Any] = {
        "supported": True,
        "pending": False,
        "migrated": None,
        "rows": {},
        "meta_keys": 0,
        "legacy_grew": {},
    }
    marker = _marker(cur, ns)
    present = legacy_present(cur)
    rows = _source_rows(cur, ns, tables) if present else {}
    if marker is not None:
        result["migrated"] = marker
        recorded = marker.get("source_rows")
        if isinstance(recorded, dict):
            result["legacy_grew"] = {
                table: n - int(recorded.get(table, 0)) for table, n in rows.items() if n > int(recorded.get(table, 0))
            }
        return result
    result["pending"] = bool(rows)
    result["rows"] = rows
    result["meta_keys"] = _legacy_meta_count(cur, ns, new_only=False) if rows else 0
    return result


def merge(cur: Any, ns: str, tables: Sequence[str], *, dry_run: bool) -> dict[str, Any]:
    """Merge legacy rows of ``ns`` into its current, non-empty tables.

    Rows whose primary or unique key already exists are skipped. Similar
    entities or topic branches can end up twice; later memory maintenance
    consolidates them. ``dry_run`` reports the counts without writing.

    ``exact`` is false when custom unique indexes make the counts an
    estimate. Only an exact preview can stop a merge: when copied nodes would
    lose their parent, ``status`` is ``blocked``, ``blocked`` lists them and
    nothing is written. An inexact one is attempted and rolls back as a whole
    if a constraint fails. ``ON CONFLICT`` cannot use deferrable unique
    constraints, so their tables make the result ``unsupported``.
    Call under :func:`lock_namespace` unless ``dry_run``.
    """
    empty: dict[str, Any] = {"tables": {}, "meta_keys": 0, "exact": True, "blocked": [], "deferrable": []}
    marker = _marker(cur, ns)
    if marker is not None:
        state = "purged" if marker.get("mode") == "purged" else "already_migrated"
        return {"status": state, **empty}
    if not legacy_present(cur):
        return {"status": "nothing_to_merge", **empty}
    rows = _source_rows(cur, ns, tables)
    if not rows:
        return {"status": "nothing_to_merge", **empty}
    plan = _plan(cur, ns, rows)
    plan["meta_keys"] = _legacy_meta_count(cur, ns, new_only=True)
    if plan["deferrable"]:
        return {"status": "unsupported", **plan}
    if plan["exact"] and plan["blocked"]:
        return {"status": "blocked", **plan}
    if dry_run:
        return {"status": "dry_run", **plan}
    result = _copy(cur, ns, tables, mode="merge")
    logger.info("merged namespace %r from schema %r: %s", ns, LEGACY_SCHEMA, result)
    return {"status": "merged", **result, "exact": plan["exact"], "blocked": [], "deferrable": []}
