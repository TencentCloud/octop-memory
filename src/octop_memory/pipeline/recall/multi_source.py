"""Multi-source recall — atom + raw fanout per route.

Pure I/O orchestration: takes a parsed query + memory handle + budgets
and returns a flat ``list[RerankCandidate]`` ready for the reranker.
Per D39-A this is **synchronous sequential** in the router-provided
source order.

Each source call is wrapped in :func:`octop_memory.pipeline.recall.timeout.with_deadline`
so a stuck SQLite call can't drag the whole pipeline past the global
hard deadline. Per-source timeouts are taken from D43-A defaults but
clamped to the remaining global budget so we don't spend 50ms on
atom when only 30ms is left overall.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from octop_memory.domain.retention import is_runtime_error
from octop_memory.pipeline.recall.fields import FieldLookup, field_lookup
from octop_memory.pipeline.recall.parser import ParsedQuery, topic_terms
from octop_memory.pipeline.recall.rerank import (
    RerankCandidate,
    from_atom,
    from_episode,
    from_page_headline,
    from_raw,
)
from octop_memory.pipeline.recall.suppress import duplicates_manual_context, manual_recall_policy
from octop_memory.pipeline.recall.timeout import (
    DEFAULT_ATOM_BUDGET_MS,
    DEFAULT_RAW_BUDGET_MS,
    Stopwatch,
    TimeoutExceededError,
    with_deadline,
)
from octop_memory.storage.driver_errors import DRIVER_ERRORS, VECTOR_ERRORS
from octop_memory.types import AtomCard, Episode, RawEvent

if TYPE_CHECKING:
    from octop_memory.core import Memory
    from octop_memory.types import EntityPage

_log = logging.getLogger(__name__)


def gather_candidates(
    memory: Memory,
    parsed: ParsedQuery,
    *,
    sources: Sequence[str] = ("atom", "raw"),
    per_source_limit: int = 10,
    sw: Stopwatch | None = None,
    entity_anchor_ids: Sequence[str] = (),
    manual_context: dict[str, str] | None = None,
) -> list[RerankCandidate]:
    """Run each enabled source, collect ``RerankCandidate``s.

    ``entity_anchor_ids`` is the set of entity IDs that the router
    resolved (either from explicit entity-name mention or from
    co-reference against the active-entity stack). When non-empty,
    the atom source ALSO fetches recent atoms for those entities
    directly, regardless of FTS — this is critical for queries like
    "那个项目最近怎么样" where the surface text doesn't contain any
    searchable token for the resolved entity.

    Order in ``sources`` controls execution priority. The default is
    ``("atom", "raw")``.

    Sources are advisory; missing data simply yields zero candidates
    for that source rather than raising.
    """
    if not parsed.text:
        return []

    stopwatch = sw or Stopwatch()
    out: list[RerankCandidate] = []
    manual_policy = manual_recall_policy(memory)
    raw_seen_ids: set[str] = set(manual_policy.get("demoted_raw_ids", []))
    seen_atom_ids: set[str] = set()

    for source in sources:
        if stopwatch.expired:
            break

        if source == "atom":
            atom_budget = min(DEFAULT_ATOM_BUDGET_MS, max(1, stopwatch.remaining_ms))
            try:
                atoms = with_deadline(
                    lambda: _gather_atoms(memory, parsed, limit=per_source_limit, anchor_ids=entity_anchor_ids),
                    stage="atom",
                    budget_ms=atom_budget,
                )
            except TimeoutExceededError:
                stopwatch.split("atom_timeout")
                continue
            except DRIVER_ERRORS:
                _log.warning("recall atom gather failed", exc_info=True)
                continue
            for idx, atom in enumerate(atoms):
                if duplicates_manual_context(atom, manual_policy, manual_context):
                    raw_seen_ids.update(atom.raw_event_ids)
                    raw_seen_ids.add(atom.quote_event_id)
                    continue
                if atom.id in seen_atom_ids:
                    continue
                seen_atom_ids.add(atom.id)
                out.append(from_atom(atom, rank_index=idx))
                raw_seen_ids.add(atom.quote_event_id)
                raw_seen_ids.update(atom.raw_event_ids)
            stopwatch.split("atom")
            continue

        if source == "raw":
            raw_budget = min(DEFAULT_RAW_BUDGET_MS, max(1, stopwatch.remaining_ms))
            try:
                events = with_deadline(
                    lambda: _per_token_raw_search(memory, parsed, limit=per_source_limit * 2),
                    stage="raw",
                    budget_ms=raw_budget,
                )
            except TimeoutExceededError:
                stopwatch.split("raw_timeout")
                continue
            except DRIVER_ERRORS:
                _log.warning("recall raw gather failed", exc_info=True)
                continue
            kept = 0
            for event in events:
                if event.payload.get("manual_document_evidence"):
                    continue
                if kept >= per_source_limit:
                    break
                if event.id in raw_seen_ids:
                    continue  # already covered by an accepted atom
                candidate = from_raw(event, rank_index=kept)
                if not candidate.text:
                    continue
                out.append(candidate)
                kept += 1
            stopwatch.split("raw")
            continue

        if source == "page_headline":
            # EntityPage headline gather — one headline per entity.
            # Only fires when the router includes this source (e.g. when
            # entity_anchor_ids are resolved and the entity has a page).
            ph_budget = min(DEFAULT_ATOM_BUDGET_MS, max(1, stopwatch.remaining_ms))
            try:
                pages = with_deadline(
                    lambda: _gather_page_headlines(memory, entity_anchor_ids, limit=per_source_limit),
                    stage="page_headline",
                    budget_ms=ph_budget,
                )
            except TimeoutExceededError:
                stopwatch.split("page_headline_timeout")
                continue
            except DRIVER_ERRORS:
                _log.warning("recall page_headline gather failed", exc_info=True)
                continue
            for idx, page in enumerate(pages):
                if page.dirty and page.entity_id in manual_policy.get("demoted_entity_ids", []):
                    continue
                if page.entity_id in {c.entity_id for c in out}:
                    continue  # entity already represented by an atom hit
                out.append(from_page_headline(page, rank_index=idx))
            stopwatch.split("page_headline")
            continue

        if source == "vector":
            # Vector semantic search (optional enhancement layer).
            # Requires Memory to be configured with vector_index and
            # embedding_provider.
            vector_budget = min(DEFAULT_ATOM_BUDGET_MS, max(1, stopwatch.remaining_ms))
            try:
                vector_atoms = with_deadline(
                    lambda: _gather_vector_atoms(memory, parsed, limit=per_source_limit, anchor_ids=entity_anchor_ids),
                    stage="vector",
                    budget_ms=vector_budget,
                )
            except TimeoutExceededError:
                stopwatch.split("vector_timeout")
                continue
            except DRIVER_ERRORS:
                _log.warning("recall vector gather failed", exc_info=True)
                continue
            for idx, atom in enumerate(vector_atoms):
                if duplicates_manual_context(atom, manual_policy, manual_context):
                    raw_seen_ids.update(atom.raw_event_ids)
                    raw_seen_ids.add(atom.quote_event_id)
                    continue
                if atom.id in seen_atom_ids:
                    continue  # already matched by FTS, skip duplicate
                seen_atom_ids.add(atom.id)
                # Vector hits get rank_index starting at per_source_limit,
                # to avoid colliding with FTS atoms' rank_index (FTS has
                # higher priority).
                out.append(from_atom(atom, rank_index=per_source_limit + idx))
                raw_seen_ids.add(atom.quote_event_id)
                raw_seen_ids.update(atom.raw_event_ids)
            stopwatch.split("vector")
            continue

        if source == "episode":
            # M5 — user-diary recall. Returns ``Episode`` rows from FTS
            # over summary + verbatim + people + topics. Independent of
            # the atom layer; useful for lived-experience queries without
            # entity anchors.
            ep_budget = min(DEFAULT_ATOM_BUDGET_MS, max(1, stopwatch.remaining_ms))
            try:
                episodes = with_deadline(
                    lambda: _gather_episodes(memory, parsed, limit=per_source_limit),
                    stage="episode",
                    budget_ms=ep_budget,
                )
            except TimeoutExceededError:
                stopwatch.split("episode_timeout")
                continue
            seen_ep_ids: set[str] = set()
            for idx, ep in enumerate(episodes):
                if ep.id in seen_ep_ids:
                    continue
                seen_ep_ids.add(ep.id)
                out.append(from_episode(ep, rank_index=idx))
                # Mark covered raw events so the raw source doesn't
                # double-up on the same quote.
                raw_seen_ids.add(ep.quote_event_id)
                raw_seen_ids.update(ep.raw_event_ids)
            stopwatch.split("episode")
            continue

        # Unknown source: be loud rather than silent.
        raise ValueError(f"gather_candidates: unknown source {source!r}")

    return out


# ---------------------------------------------------------------------------
# Per-source helpers
# ---------------------------------------------------------------------------


def _gather_atoms(
    memory: Memory,
    parsed: ParsedQuery,
    *,
    limit: int,
    anchor_ids: Sequence[str] = (),
) -> list[AtomCard]:
    """Score topical matches before truncating the entity and global pools."""
    topic = parsed.topic if parsed.topic is not None else parsed.text
    lookup = field_lookup(topic)
    terms = topic_terms(parsed)
    if lookup:
        terms = lookup.expand(topic, terms)
    start = parsed.time_window.start if parsed.time_window else None
    end = parsed.time_window.end if parsed.time_window else None
    if not terms:
        if not anchor_ids and not parsed.time_window:
            return []
        return memory.search_atom_terms((), entity_ids=anchor_ids, start=start, end=end, limit=limit)
    global_hits = memory.search_atom_terms(terms, start=start, end=end, limit=limit)
    if not anchor_ids:
        return [atom for atom in global_hits if not lookup or lookup.evidence(atom.assertion)]
    local_hits = memory.search_atom_terms(terms, entity_ids=anchor_ids, start=start, end=end, limit=limit)
    allowed = _scoped_atom_ids(memory, [*global_hits, *local_hits], anchor_ids, lookup)
    eligible = [atom for atom in global_hits if atom.id in allowed]
    local_hits = [atom for atom in local_hits if atom.id in allowed]
    # Equal source weights. A candidate retrieved through both paths gets
    # both contributions; either path can contribute an older fact.
    scores: dict[str, float] = {}
    atoms: dict[str, AtomCard] = {}
    for hits in (eligible, local_hits):
        for rank, atom in enumerate(hits, 1):
            atoms[atom.id] = atom
            scores[atom.id] = scores.get(atom.id, 0.0) + 1.0 / (60 + rank)
    return [atoms[aid] for aid in sorted(scores, key=lambda aid: -scores[aid])[:limit]]


def _scoped_atom_ids(
    memory: Memory,
    hits: Sequence[AtomCard],
    anchor_ids: Sequence[str],
    lookup: FieldLookup | None,
) -> set[str]:
    from octop_memory.domain.alias import normalize_alias
    from octop_memory.pipeline.recall.names import mention_owners, mentions

    allowed: set[str] = set()
    inspect: dict[str, tuple[bool, list[str]]] = {}
    for aid, atom in {a.id: a for a in hits}.items():
        local = atom.entity_id in anchor_ids
        if local and not lookup:
            allowed.add(aid)
            continue
        evidence = lookup.evidence(atom.assertion) if lookup else (atom.assertion + " / " + atom.verbatim_quote,)
        if evidence:
            inspect[aid] = (local, [normalize_alias(clause) for clause in evidence])

    if not inspect:
        return allowed
    owners = mention_owners(memory, [c for _, clauses in inspect.values() for c in clauses])
    anchor_types = set()
    if lookup and any(e.id not in anchor_ids for matches in owners.values() for e in matches):
        anchor_types = {entity.entity_type for eid in anchor_ids if (entity := memory.get_entity(eid)) is not None}

    for aid, (local, clauses) in inspect.items():
        names = [name for clause in clauses for _, _, name in mentions(clause, owners)]
        if anchor_types and any(
            e.entity_type in anchor_types and e.id not in anchor_ids for name in names for e in owners[name]
        ):
            continue
        if local or any(len(owners[name]) == 1 and owners[name][0].id in anchor_ids for name in names):
            allowed.add(aid)
    return allowed


def _per_token_raw_search(
    memory: Memory,
    parsed: ParsedQuery,
    *,
    limit: int,
) -> list[RawEvent]:
    """Merge per-token raw-event hits in query order and deduplicate by event ID."""
    seen: dict[str, tuple[int, int, RawEvent]] = {}
    tokens = parsed.raw_tokens or (parsed.text,)
    for token_idx, token in enumerate(tokens):
        if not token.strip():
            continue
        try:
            events = memory.search_raw(token, limit=limit * 2)
        except DRIVER_ERRORS as exc:
            _log.warning("recall raw search failed for token %r: %s", token, exc, exc_info=True)
            continue
        for evt_idx, event in enumerate(events):
            if is_runtime_error(event):
                continue
            if event.id not in seen:
                seen[event.id] = (token_idx, evt_idx, event)
    ranked = sorted(seen.values(), key=lambda kv: (kv[0], kv[1]))
    return [item[2] for item in ranked[:limit]]


def _gather_page_headlines(
    memory: Memory,
    anchor_ids: Sequence[str],
    *,
    limit: int,
) -> list[EntityPage]:
    """Fetch EntityPage headlines for the given entity anchor IDs.

    Only returns pages that have a non-empty headline and are not dirty
    (dirty pages have stale content — better to skip than show outdated
    summaries). Pages are returned in the order of ``anchor_ids``.

    If ``anchor_ids`` is empty, returns an empty list — page headlines
    are only useful when we know which entity the query is about.
    """

    if not anchor_ids:
        return []

    pages: list[EntityPage] = []
    seen: set[str] = set()
    for entity_id in anchor_ids:
        if entity_id in seen:
            continue
        seen.add(entity_id)
        try:
            page = memory.get_entity_page(entity_id)
        except DRIVER_ERRORS as exc:
            _log.warning(
                "recall page_headline fetch failed for entity %r: %s",
                entity_id,
                exc,
                exc_info=True,
            )
            continue
        if page is None:
            continue
        if not page.headline.strip():
            continue  # no headline yet (page not yet regenerated)
        if page.dirty:
            continue  # stale — skip to avoid showing outdated summary
        pages.append(page)
        if len(pages) >= limit:
            break
    return pages


def _gather_vector_atoms(
    memory: Memory,
    parsed: ParsedQuery,
    *,
    limit: int,
    anchor_ids: Sequence[str] = (),
) -> list[AtomCard]:
    """Recall AtomCards via vector semantic search.

    Requires Memory to have both vector_index and embedding_provider
    configured. If either is missing, returns an empty list immediately
    (silent degradation, doesn't affect FTS recall).

    Flow:
    1. embedding_provider.embed(query) -> query vector
    2. vector_index.search(vec, limit=limit) -> [atom_id, ...]
    3. backend.get_atom(atom_id) x N -> [AtomCard, ...]
    """
    vector_index = getattr(memory, "_vector_index", None)
    embedding_provider = getattr(memory, "_embedding_provider", None)

    if vector_index is None or embedding_provider is None:
        return []

    try:
        query_vec = embedding_provider.embed(parsed.text)
    except VECTOR_ERRORS:
        _log.warning("recall vector embed failed", exc_info=True)
        return []

    try:
        atom_ids = vector_index.search(query_vec, limit=limit)
    except VECTOR_ERRORS:
        _log.warning("recall vector search failed", exc_info=True)
        return []

    atoms: list[AtomCard] = []
    for atom_id in atom_ids:
        try:
            atom = memory.get_atom(atom_id)
            if atom is not None:
                atoms.append(atom)
        except DRIVER_ERRORS:
            _log.warning("recall vector atom fetch failed for %r", atom_id, exc_info=True)
            continue
    lookup = field_lookup(parsed.topic if parsed.topic is not None else parsed.text)
    if lookup and atoms:
        if not anchor_ids:
            return [atom for atom in atoms if lookup.evidence(atom.assertion)]
        allowed = _scoped_atom_ids(memory, atoms, anchor_ids, lookup)
        return [atom for atom in atoms if atom.id in allowed]
    return atoms


def _gather_episodes(
    memory: Memory,
    parsed: ParsedQuery,
    *,
    limit: int,
) -> list[Episode]:
    """Per-token FTS search over Episodes.

    Same per-token strategy as :func:`_per_token_atom_search` to avoid
    SQLite FTS5's phrase-match trap for natural language queries.
    Time-window queries narrow by ``occurred_at``; otherwise use FTS.
    """
    if parsed.time_window is not None:
        # Time-bounded list: episodes are diary entries, so time scoping
        # is the most natural filter for "how was last week" style queries.
        try:
            return memory.list_episodes(
                after=parsed.time_window.start,
                before=parsed.time_window.end,
                limit=limit,
            )
        except DRIVER_ERRORS as exc:
            _log.warning("recall episode time-range failed: %s", exc, exc_info=True)
            return []

    seen: dict[str, tuple[int, int, Episode]] = {}
    tokens = parsed.raw_tokens or (parsed.text,)
    for token_idx, token in enumerate(tokens):
        if not token.strip():
            continue
        try:
            episodes = memory.search_episodes(token, limit=limit * 2)
        except DRIVER_ERRORS as exc:
            _log.warning("recall episode search failed for token %r: %s", token, exc, exc_info=True)
            continue
        for ep_idx, ep in enumerate(episodes):
            if ep.id not in seen:
                seen[ep.id] = (token_idx, ep_idx, ep)
    ranked = sorted(seen.values(), key=lambda kv: (kv[0], kv[1]))
    return [item[2] for item in ranked[:limit]]


__all__ = ["gather_candidates"]
