"""Bounded alternate wording used only by an atom's lexical index."""

from __future__ import annotations

from octop_memory.domain.alias import normalize_alias


def search_phrases(value: object, *, subject: str) -> list[str]:
    """Accept up to three short topic phrases. Never turn them into entity aliases."""
    if not isinstance(value, list):
        return []
    seen = {normalize_alias(subject)}
    phrases = []
    for item in value:
        if not isinstance(item, str) or len(item) > 48:
            continue
        phrase = normalize_alias(item)
        if len(phrase) < 2 or phrase in seen or not all(c.isalpha() or c in " '-" for c in phrase):
            continue
        if not any(c.isalpha() for c in phrase):
            continue
        seen.add(phrase)
        phrases.append(phrase)
        if len(phrases) == 3:
            break
    return phrases
