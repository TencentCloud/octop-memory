"""Memory CLI: ``entity`` subcommand group for L3 entity anchors.

Inspection only — entities are auto-created by the promotion worker
during candidate.subject resolution. Long-form summaries live in the
separate ``EntityPage`` table and are managed by the ``page`` commands.
"""

from __future__ import annotations

import json
import sys
from typing import Any, get_args

import click

from octop_memory.adapters.cli.config import get_memory
from octop_memory.types import Entity, EntityType


def _entity_to_dict(e: Entity) -> dict[str, Any]:
    return {
        "id": e.id,
        "entity_type": e.entity_type,
        "canonical_name": e.canonical_name,
        "aliases": e.aliases,
        "atom_count": e.atom_count,
        "last_promoted_at": e.last_promoted_at.isoformat() if e.last_promoted_at else None,
        "created_at": e.created_at.isoformat(),
    }


@click.group(name="entity")
def entity_group() -> None:
    """Inspect L3 entities (anchors for atoms)."""


@entity_group.command(name="list")
@click.option(
    "--type",
    "entity_type",
    type=click.Choice(list(get_args(EntityType))),
    default=None,
    help="Filter by entity_type.",
)
@click.option("--limit", default=100, show_default=True, type=int)
@click.pass_context
def list_cmd(
    ctx: click.Context,
    entity_type: str | None,
    limit: int,
) -> None:
    """List entities (alphabetical by canonical_name)."""
    memory = get_memory(ctx)
    entities = memory.list_entities(
        entity_type=entity_type,  # type: ignore[arg-type]
        limit=limit,
    )

    if ctx.obj.get("output_json"):
        click.echo(json.dumps([_entity_to_dict(e) for e in entities], ensure_ascii=False, indent=2))
        return

    if not entities:
        click.echo("No entities found.")
        return
    for e in entities:
        click.echo(f"{e.id[:8]}  {e.entity_type:10s}  atoms={e.atom_count:3d}  {e.canonical_name}")


@entity_group.command(name="show")
@click.argument("entity_id")
@click.pass_context
def show_cmd(ctx: click.Context, entity_id: str) -> None:
    """Show entity details + its non-deprecated atoms (full UUID required)."""
    memory = get_memory(ctx)
    e = memory.get_entity(entity_id)
    if e is None:
        click.echo(f"Entity {entity_id} not found.", err=True)
        sys.exit(1)

    atoms = memory.list_atoms(entity_id=entity_id, limit=200)
    aliases = memory.list_aliases(entity_id=entity_id, limit=50)

    if ctx.obj.get("output_json"):
        click.echo(
            json.dumps(
                {
                    "entity": _entity_to_dict(e),
                    "atoms": [
                        {
                            "id": a.id,
                            "importance": a.importance,
                            "confidence": a.confidence,
                            "assertion": a.assertion,
                            "deprecated": a.deprecated_at is not None,
                        }
                        for a in atoms
                    ],
                    "aliases": [{"alias": al.alias, "created_by": al.created_by} for al in aliases],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    click.echo(f"id             : {e.id}")
    click.echo(f"type           : {e.entity_type}")
    click.echo(f"canonical_name : {e.canonical_name}")
    click.echo(f"atom_count     : {e.atom_count}")
    click.echo(f"created_at     : {e.created_at.isoformat()}")
    click.echo(f"last_promoted  : {e.last_promoted_at.isoformat() if e.last_promoted_at else '-'}")
    click.echo("")
    click.echo(f"aliases ({len(aliases)}):")
    for al in aliases:
        click.echo(f"  - {al.alias!r} (by {al.created_by})")
    click.echo("")
    click.echo(f"atoms ({len(atoms)}):")
    for a in atoms:
        flag = " DEPR" if a.deprecated_at else "     "
        click.echo(f"  - {a.id[:8]}{flag}  [{a.importance:6s}]  {a.assertion[:80]}")


@entity_group.command("dream")
@click.option("--limit", default=20, type=click.IntRange(min=1))
@click.pass_context
def dream_cmd(ctx: click.Context, limit: int) -> None:
    """Audit a batch of live atoms. Inspect findings with journal list --action entity_review."""
    from octop_memory.pipeline.lifecycle.dreaming import run_entity_dreaming

    scanned = run_entity_dreaming(get_memory(ctx), limit=limit)
    click.echo(json.dumps({"atoms_scanned": scanned}) if ctx.obj.get("output_json") else f"Reviewed {scanned} atoms.")


_CAPACITY_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("<=50", 0, 50),
    ("51-150", 51, 150),
    ("151-200", 151, 200),
    ("201-300", 201, 300),
    (">300", 301, None),
)


def _capacity_bucket(atom_count: int) -> str:
    """Label one entity by its live atom count."""
    for label, low, high in _CAPACITY_BUCKETS:
        if atom_count >= low and (high is None or atom_count <= high):
            return label
    return "<=50"


def _summary_coverage(atom_count: int, regen_window: int) -> float:
    """Share of an entity's atoms that one page regeneration can read.

    Page regeneration sends only the newest regen_window atoms, so
    everything older never reaches the summary. 0.0 means nothing is
    coverable in principle (empty entity).
    """
    if atom_count <= 0 or regen_window <= 0:
        return 0.0
    return min(atom_count, regen_window) / atom_count


@entity_group.command("capacity-report")
@click.option(
    "--threshold",
    default=200,
    show_default=True,
    type=click.IntRange(min=1),
    help="Per-entity atom count that triggers sharding.",
)
@click.option(
    "--top",
    "top_n",
    default=20,
    show_default=True,
    type=click.IntRange(min=1),
    help="How many of the widest entities to list.",
)
@click.option(
    "--regen-window",
    default=None,
    type=click.IntRange(min=1),
    help="Atoms one page regeneration reads; defaults to DEFAULT_MAX_ATOMS_PER_REGEN (50).",
)
@click.option(
    "--scan-limit",
    default=10_000,
    show_default=True,
    type=click.IntRange(min=1),
    help="Upper bound on entities scanned in one pass.",
)
@click.pass_context
def capacity_report_cmd(
    ctx: click.Context,
    threshold: int,
    top_n: int,
    regen_window: int | None,
    scan_limit: int,
) -> None:
    """Report per-entity atom capacity and summary coverage (read-only).

    Answers "which topics are too wide to summarize or deduplicate".
    Nothing is written: no atoms, pages, journal entries or LLM calls.
    Wide entities (> --threshold) are the ones the standing-stock
    consolidator skips (DEFAULT_ATOM_SCAN_LIMIT = 200) and whose page
    summary only ever sees the newest --regen-window atoms.
    """
    from octop_memory.pipeline.page.regenerator import DEFAULT_MAX_ATOMS_PER_REGEN

    memory = get_memory(ctx)
    window = regen_window or DEFAULT_MAX_ATOMS_PER_REGEN
    entities = memory.list_entities(limit=scan_limit)
    entities.sort(key=lambda e: (-e.atom_count, e.canonical_name.lower()))

    total_atoms = sum(e.atom_count for e in entities)
    buckets: dict[str, dict[str, int]] = {label: {"entities": 0, "atoms": 0} for label, _, _ in _CAPACITY_BUCKETS}
    for entity in entities:
        slot = buckets[_capacity_bucket(entity.atom_count)]
        slot["entities"] += 1
        slot["atoms"] += entity.atom_count

    wide = [e for e in entities if e.atom_count > threshold]
    wide_atoms = sum(e.atom_count for e in wide)

    rows: list[dict[str, Any]] = []
    for entity in wide[:top_n]:
        page = memory.get_entity_page(entity.id)
        rows.append(
            {
                "id": entity.id,
                "canonical_name": entity.canonical_name,
                "entity_type": entity.entity_type,
                "atom_count": entity.atom_count,
                "summary_coverage": round(_summary_coverage(entity.atom_count, window), 4),
                "suggested_shards": max(1, -(-entity.atom_count // threshold)),
                "page_dirty": bool(page.dirty) if page is not None else None,
                "page_topics": list(page.topics) if page is not None else [],
            }
        )

    wide_share = round(wide_atoms / total_atoms, 4) if total_atoms else 0.0
    payload: dict[str, Any] = {
        "entities_scanned": len(entities),
        "total_atoms": total_atoms,
        "threshold": threshold,
        "regen_window": window,
        "buckets": buckets,
        "wide_entity_count": len(wide),
        "wide_atom_count": wide_atoms,
        "wide_atom_share": wide_share,
        "wide_entities": rows,
    }

    if ctx.obj.get("output_json"):
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    click.echo(f"entities scanned : {len(entities)} (limit {scan_limit})")
    click.echo(f"total atoms      : {total_atoms}")
    click.echo(f"regen window     : {window} atoms per page")
    click.echo(f"shard threshold  : {threshold} atoms per entity")
    click.echo("")
    click.echo("capacity buckets:")
    for label, _, _ in _CAPACITY_BUCKETS:
        slot = buckets[label]
        click.echo(f"  {label:<9} entities={slot['entities']:>5}  atoms={slot['atoms']:>7}")
    click.echo("")
    share = wide_share * 100
    click.echo(f"wide entities (>{threshold}): {len(wide)} entities, {wide_atoms} atoms ({share:.1f}% of all atoms)")
    if not rows:
        click.echo("no entity exceeds the threshold - nothing to shard.")
        return
    click.echo("")
    click.echo(f"{'entity':<32}{'type':<10}{'atoms':>7}{'cover':>8}{'shards':>8}  {'dirty':<6}topics")
    for row in rows:
        topics = ", ".join(row["page_topics"][:3]) or "-"
        dirty = "yes" if row["page_dirty"] else ("-" if row["page_dirty"] is None else "no")
        click.echo(
            f"{row['canonical_name'][:32]:<32}"
            f"{row['entity_type']:<10}"
            f"{row['atom_count']:>7}"
            f"{row['summary_coverage'] * 100:>7.1f}%"
            f"{row['suggested_shards']:>8}  "
            f"{dirty:<6}{topics}"
        )
