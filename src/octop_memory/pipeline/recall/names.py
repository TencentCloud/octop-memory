"""Exact query mentions with token boundaries and longest-name precedence."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from octop_memory.domain.alias import normalize_alias

if TYPE_CHECKING:
    from octop_memory.core import Memory
    from octop_memory.types import Entity

# Chinese has no whitespace boundary; a Latin identifier, including a hyphenated
# project name, is indivisible. Multiword names can span whitespace.
_UNIT = re.compile(r"[㐀-鿿]|(?:(?![㐀-鿿])\w)+(?:[-.](?:(?![㐀-鿿])\w)+)*")


def name_spans(text: str) -> Iterable[tuple[int, int, str]]:
    units = list(_UNIT.finditer(text))
    for i, unit in enumerate(units):
        end = unit.start()
        for other in units[i : i + 24]:
            if text[end : other.start()].strip():
                break
            end = other.end()
            yield unit.start(), end, text[unit.start() : end]


def mention_owners(memory: Memory, texts: list[str]) -> dict[str, list[Entity]]:
    keys = {name for text in texts for _, _, name in name_spans(text)}
    # Whole names (also punctuation-bearing or unusually long identifiers).
    keys.update(text for text in texts if text)
    return memory.match_entity_names(tuple(keys))


def mentions(text: str, owners: dict[str, list[Entity]]) -> list[tuple[int, int, str]]:
    if text in owners:
        return [(0, len(text), text)]
    hits = sorted((span for span in name_spans(text) if span[2] in owners), key=lambda span: (span[0], -span[1]))
    accepted: list[tuple[int, int, str]] = []
    for span in hits:
        if not accepted or span[0] >= accepted[-1][1]:
            accepted.append(span)
    return accepted


def resolve_mentions(memory: Memory, query: str) -> tuple[list[str], str, bool]:
    text = normalize_alias(query)
    owners = mention_owners(memory, [text])
    ids: dict[str, None] = {}
    residual = list(text)
    ambiguous = False
    for start, end, name in mentions(text, owners):
        matches = owners[name]
        if len(matches) == 1:
            ids[matches[0].id] = None
        else:
            ambiguous = True
        residual[start:end] = " " * (end - start)
    return list(ids), "".join(residual), ambiguous
