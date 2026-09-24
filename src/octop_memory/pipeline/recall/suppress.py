"""Pre-injection suppression — rule-based by default (D44-A).

Drops or downranks snippets that would duplicate information the
host already has. Two scenarios:

1. **Self-duplicate within recall**: two atoms with very similar
   ``assertion`` text — keep the higher-scored one.
2. **Plugin vs Host index** (M4.8 deferred to M1 W3): if the host
   already has a USER.md fact saying the same thing, suppress the
   plugin atom. M4 ships the rule path only; the host index hookup
   is added when M1 W3 lands.

D44-A means we **default to rule-based** similarity:

- Normalised string equality (case + whitespace folded).
- Jaccard similarity over word-tokens with a default ``0.85`` threshold.

The opt-in LLM path is wired but disabled by default; the dogfood
script flips ``--llm-suppress`` to A/B test it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from octop_memory.pipeline.recall.rerank import RankedSnippet

DEFAULT_JACCARD_THRESHOLD = 0.85
"""Two snippets with Jaccard ≥ this are deemed redundant. Tuned high
so we only suppress near-duplicates; lower thresholds drop legitimate
related-but-distinct atoms."""


@dataclass(frozen=True)
class SuppressionResult:
    """Snippets kept and dropped by duplicate suppression.

    Output of :func:`suppress_duplicates`. ``kept`` is the result
    list to pass downstream; ``dropped`` is the suppressed snippets
    (paired with the snippet that beat them) for debug rendering."""

    kept: list[RankedSnippet]
    dropped: list[tuple[RankedSnippet, RankedSnippet]]


_NON_WORD_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


def _tokenize(text: str) -> set[str]:
    """Coarse tokens for Jaccard similarity.

    Coarse tokeniser for Jaccard. Split on non-word boundaries,
    lowercased, then explode multi-Han runs into single chars so
    Chinese substring overlap is captured."""
    tokens: set[str] = set()
    for piece in _NON_WORD_RE.split(text.lower()):
        if not piece:
            continue
        # Split CJK runs into individual chars; keep ASCII as whole word.
        if any("\u4e00" <= c <= "\u9fff" for c in piece):
            tokens.update(c for c in piece if "\u4e00" <= c <= "\u9fff")
        else:
            tokens.add(piece)
    return tokens


def jaccard(a: str, b: str) -> float:
    """Word-token Jaccard similarity in ``[0.0, 1.0]``.

    Empty inputs always score 0 — empty == empty makes no sense to
    return 1.0 since "no information" can't duplicate "no information".
    """
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def suppress_duplicates(
    ranked: Iterable[RankedSnippet],
    *,
    threshold: float = DEFAULT_JACCARD_THRESHOLD,
) -> SuppressionResult:
    """Greedy pass: keep highest-scored, drop later snippets too similar.

    Iterates input in order — caller is responsible for sorting by
    score DESC first (typically reranker → diversifier → suppress).
    Within-call complexity is ``O(n^2)`` on snippet text length but
    n is small (≤ recall limit, typically 5-20) so this is cheap.
    """
    kept: list[RankedSnippet] = []
    dropped: list[tuple[RankedSnippet, RankedSnippet]] = []
    for snippet in ranked:
        loser_for: RankedSnippet | None = None
        for prior in kept:
            if jaccard(snippet.candidate.text, prior.candidate.text) >= threshold:
                loser_for = prior
                break
        if loser_for is None:
            kept.append(snippet)
        else:
            dropped.append((snippet, loser_for))
    return SuppressionResult(kept=kept, dropped=dropped)


__all__ = [
    "DEFAULT_JACCARD_THRESHOLD",
    "SuppressionResult",
    "jaccard",
    "suppress_duplicates",
]
