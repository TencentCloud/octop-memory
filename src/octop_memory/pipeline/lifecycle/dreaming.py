"""Bounded background audit of entity assignments. Findings retain their evidence."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from octop_memory.domain.alias import normalize_alias
from octop_memory.types import JournalEntry

if TYPE_CHECKING:
    from octop_memory.core import Memory
    from octop_memory.types import AtomCard, Entity


_CURSOR = "entity_review/v1/cursor"
_ALIAS_DECLARATION = re.compile(
    r"(?:我们(?:内部)?把)?(.+?)(?:也叫|又叫|简称(?:为)?|叫作|叫做|叫|"
    r" is also known as | is called )\s*[「“\"]?([\w.-]+(?: [\w.-]+){0,3})[」”\"]?[。.!]?",
    re.I,
)


def _nearby(memory: Memory, entity: Entity) -> list[str]:
    name = normalize_alias(entity.canonical_name)
    if len(name) < 2:
        return []
    nearby = memory.backend.similar_name_candidates(entity, name[:1], limit=32)
    return sorted(e.id for e in nearby if SequenceMatcher(None, name, normalize_alias(e.canonical_name)).ratio() >= 0.8)


def run_entity_dreaming(memory: Memory, *, limit: int = 20) -> int:
    """Review one keyset page, wrapping for late inserts and changed ownership.

    The cursor and evidence records commit together. Stable finding IDs keep
    retries and overlapping background workers from duplicating review items.
    """
    if limit < 1:
        return 0
    with memory.backend.transaction():
        cursor = memory.get_meta(_CURSOR) or ""
        atoms = memory.backend.scan_live_atoms(cursor, limit=limit)
        neighbors: dict[str, list[str]] = {}
        for atom in atoms:
            entity = memory.get_entity(atom.entity_id)
            if entity is None:
                _record(memory, atom, "missing_entity", [])
                continue
            if entity.id not in neighbors:
                neighbors[entity.id] = _nearby(memory, entity)
            if neighbors[entity.id]:
                _record(memory, atom, "similar_name", neighbors[entity.id])
            candidate = memory.get_candidate(atom.candidate_id)
            if candidate is None:
                continue
            name = normalize_alias(candidate.subject_name)
            owners = memory.match_entity_names([name]).get(name, [])
            relevant = sorted(e.id for e in owners if e.entity_type == candidate.subject_entity_type)
            if entity.entity_type != candidate.subject_entity_type or (relevant and entity.id not in relevant):
                _record(memory, atom, "assignment_mismatch", relevant)
            elif len(relevant) > 1:
                _record(memory, atom, "ambiguous_name", relevant)
            # Only suggest aliases attested in a complete first-party statement.
            raw = memory.get_raw(atom.quote_event_id)
            if raw is None or raw.event_type not in ("user_message", "manual"):
                continue
            match = _ALIAS_DECLARATION.fullmatch(normalize_alias(raw.content))
            if match and normalize_alias(match[1]) == normalize_alias(entity.canonical_name):
                alias = normalize_alias(match[2])
                existing = memory.match_entity_names([alias]).get(alias, [])
                if not any(e.id == entity.id for e in existing):
                    _record(memory, atom, "alias_declaration", sorted(e.id for e in existing), alias)
        next_cursor = atoms[-1].id if len(atoms) == limit else ""
        if next_cursor != cursor:
            memory.set_meta(_CURSOR, next_cursor)
    return len(atoms)


def _record(memory: Memory, atom: AtomCard, reason: str, candidates: list[str], alias: str = "") -> None:
    details = {
        "reason": reason,
        "related_entity_ids": candidates,
        "alias": alias,
        "quote_event_id": atom.quote_event_id,
    }
    key = json.dumps([memory.namespace, atom.id, atom.entity_id, details], sort_keys=True)
    memory.append_journal(
        JournalEntry(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
            timestamp=datetime.now(UTC),
            action="entity_review",
            actor="rule",
            target_entity_id=atom.entity_id,
            target_candidate_id=atom.candidate_id,
            target_atom_id=atom.id,
            after=details,
            note=reason,
        )
    )
