"""Candidate Extractor — turns a batch of L0 raw events into L1 candidates.

Orchestration:
1. Render the prompt v2 against the input raw events
2. Call ``LLMClient.call_llm(tier="light", response_format="json")``
3. Parse the JSON response (with one retry if parse fails)
4. Run post-hoc validation (referential integrity + anti-dilution warnings)
5. Return a ``ExtractionResult``

The Extractor never persists anything itself — that's the caller's job
(via ``Memory.add_candidate`` / promotion worker). This keeps the extractor
side-effect-free, easy to test with ``MockLLMClient``, and allows the
caller to inspect / drop candidates before they hit storage.

When the LLM is unavailable (``LLMClientError``, ``OSError``, or
``TimeoutError``) or the parse retries are exhausted, ``extract`` returns
an ``ExtractionResult`` with an empty ``candidates`` list and
``failure_reason`` set. Other exceptions propagate. The promotion worker /
CLI surface this state as ``extractor_failed`` if the caller tracks it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from octop_memory.domain.retention import is_runtime_error
from octop_memory.pipeline.extractor.parser import (
    ExtractorParseError,
    ParseResult,
    parse_extractor_output,
)
from octop_memory.pipeline.extractor.prompts import (
    EXTRACTOR_VERSION,
    render_prompt,
    render_retry_prompt,
)
from octop_memory.ports.llm import (
    DEFAULT_EXTRACT_MAX_TOKENS,
    LLMClient,
    LLMClientError,
)
from octop_memory.types import Candidate, RawEvent

logger = logging.getLogger(__name__)

DEFAULT_MAX_CANDIDATES = 20
# Extraction is bounded per LLM call: a whole session's backlog (thousands of
# events) used to go into one prompt, which overflows the context and fails as
# "length limit reached" (#25). Mirror ``operations/retention.py``: cap the
# events per call and split further on a character budget.
DEFAULT_MAX_EVENTS_PER_BATCH = 100
DEFAULT_MAX_CHARS_PER_BATCH = 20_000
# Rough per-event overhead (event_id, event_type, timestamp, session_id, JSON
# syntax/indent) added to ``len(content)`` when estimating a batch's size.
_EVENT_OVERHEAD_CHARS = 200


@dataclass
class ExtractionResult:
    """Outcome of running the extractor on one batch.

    - ``candidates``: zero or more candidates ready to be persisted. Empty
      means "no extraction-worthy content" (which is a normal outcome —
      most session_end batches contain chit-chat that should not promote).
    - ``warnings``: anti-dilution / shape concerns from the parser, plus
      retry / cap-warning messages added by the orchestrator.
    - ``cap_warning``: set when the LLM signalled it truncated the output
      (caller should consider re-batching).
    - ``failure_reason``: set when extraction failed entirely (LLM call
      raised, or both attempts produced unparseable JSON). Caller decides
      whether to log, drop, or persist a placeholder ``extractor_failed``
      record.
    - ``llm_calls``: how many ``LLMClient.call_llm`` invocations were made
      (1 = first attempt parsed, 2 = retry was needed).
    """

    candidates: list[Candidate]
    warnings: list[str] = field(default_factory=list)
    cap_warning: str | None = None
    failure_reason: str | None = None
    llm_calls: int = 0


def _serialize_events_for_prompt(events: list[RawEvent]) -> str:
    """Render raw events as compact JSON for prompt embedding.

    We deliberately pick the small subset of fields the prompt cares about
    (event_id / event_type / content / timestamp / session_id). Including
    the full payload would burn tokens on data the LLM doesn't need.
    """
    payload = [
        {
            "event_id": e.id,
            "event_type": e.event_type,
            "content": e.content,
            "timestamp": e.timestamp.isoformat(),
            "session_id": e.session_id,
        }
        for e in events
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _chunk_events(
    events: list[RawEvent],
    *,
    max_events: int,
    max_chars: int,
) -> list[list[RawEvent]]:
    """Split events into batches bounded by count and estimated prompt size.

    A single oversized batch is what overflowed the extractor's LLM context
    (#25). Greedy chunking keeps each batch under both limits; a single event
    larger than the budget still gets its own batch so it is never dropped.
    """
    batches: list[list[RawEvent]] = []
    current: list[RawEvent] = []
    current_chars = 0
    for event in events:
        cost = len(event.content) + _EVENT_OVERHEAD_CHARS
        if current and (len(current) >= max_events or current_chars + cost > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(event)
        current_chars += cost
    if current:
        batches.append(current)
    return batches


def _merge_results(results: list[ExtractionResult]) -> ExtractionResult:
    """Combine per-batch results into one, preserving partial candidates."""
    candidates: list[Candidate] = []
    warnings: list[str] = []
    cap_warning: str | None = None
    failure_reason: str | None = None
    llm_calls = 0
    for result in results:
        candidates.extend(result.candidates)
        warnings.extend(result.warnings)
        llm_calls += result.llm_calls
        if cap_warning is None:
            cap_warning = result.cap_warning
        if failure_reason is None:
            failure_reason = result.failure_reason
    return ExtractionResult(
        candidates=candidates,
        warnings=warnings,
        cap_warning=cap_warning,
        failure_reason=failure_reason,
        llm_calls=llm_calls,
    )


class CandidateExtractor:
    """Stateless orchestrator that turns raw events into candidates.

    The extractor is constructed once with an ``LLMClient`` and reused for
    every session. It does not own any persistence — call ``extract`` and
    pass the resulting ``Candidate`` objects to ``Memory.add_candidate``.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        extractor_version: str = EXTRACTOR_VERSION,
        max_retries: int = 1,
        temperature: float = 0.0,
        max_tokens: int | None = DEFAULT_EXTRACT_MAX_TOKENS,
        max_events_per_batch: int = DEFAULT_MAX_EVENTS_PER_BATCH,
        max_chars_per_batch: int = DEFAULT_MAX_CHARS_PER_BATCH,
    ) -> None:
        self._llm = llm
        self._max_candidates = max_candidates
        self._extractor_version = extractor_version
        self._max_retries = max_retries
        self._temperature = temperature
        # ``None`` = caller left it unset (e.g. host config key absent) — fall
        # back to the built-in budget so the default cap cannot be erased by
        # an explicit ``None`` threaded down from a config layer.
        self._max_tokens = DEFAULT_EXTRACT_MAX_TOKENS if max_tokens is None else max_tokens
        if max_events_per_batch < 1:
            raise ValueError("max_events_per_batch must be >= 1")
        if max_chars_per_batch < 1:
            raise ValueError("max_chars_per_batch must be >= 1")
        self._max_events_per_batch = max_events_per_batch
        self._max_chars_per_batch = max_chars_per_batch

    @property
    def extractor_version(self) -> str:
        return self._extractor_version

    def extract(
        self,
        events: list[RawEvent],
        *,
        session_id: str | None = None,
    ) -> ExtractionResult:
        """Run extraction on a batch of raw events.

        ``session_id`` defaults to the session of the first event when
        homogeneous (best-effort tagging). Pass an explicit value when
        the batch crosses sessions (heavy-cadence extraction in M4) or
        when you want to override.
        """
        events = [
            event
            for event in events
            if not is_runtime_error(event) and not event.payload.get("manual_document_evidence")
        ]
        if not events:
            return ExtractionResult(candidates=[])

        if session_id is None and events:
            # Best-effort: if all events share a session, use it.
            sids = {e.session_id for e in events if e.session_id}
            if len(sids) == 1:
                session_id = sids.pop()

        batches = _chunk_events(
            events,
            max_events=self._max_events_per_batch,
            max_chars=self._max_chars_per_batch,
        )
        results: list[ExtractionResult] = []
        for batch in batches:
            events_json = _serialize_events_for_prompt(batch)
            prompt = render_prompt(events_json, max_candidates=self._max_candidates)
            raw_event_ids = {e.id for e in batch}
            result = self._call_with_retry(
                prompt=prompt,
                session_id=session_id,
                raw_event_ids=raw_event_ids,
            )
            results.append(result)
            if result.failure_reason is not None:
                # Fail fast: a failed batch leaves its events unmarked so the
                # caller retries them. Processing the remaining batches now
                # would only burn calls the retry discards anyway.
                break
        return _merge_results(results)

    def _call_with_retry(
        self,
        *,
        prompt: str,
        session_id: str | None,
        raw_event_ids: set[str],
    ) -> ExtractionResult:
        attempts = 0
        last_output: str | None = None
        last_parse_error: str | None = None
        warnings: list[str] = []
        max_attempts = 1 + self._max_retries

        while attempts < max_attempts:
            attempts += 1
            current_prompt = (
                prompt if attempts == 1 or last_output is None else render_retry_prompt(prompt, last_output)
            )
            try:
                raw_output = self._llm.call_llm(
                    current_prompt,
                    tier="light",
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    response_format="json",
                )
            except (LLMClientError, OSError, TimeoutError) as e:
                # Contract failures and leaked socket/timeout errors degrade.
                # Other exceptions are bugs and propagate.
                logger.warning("candidate extraction LLM call failed", exc_info=True)
                return ExtractionResult(
                    candidates=[],
                    warnings=warnings,
                    failure_reason=f"LLM call failed: {e}",
                    llm_calls=attempts,
                )

            last_output = raw_output

            try:
                parsed: ParseResult = parse_extractor_output(
                    raw_output,
                    session_id=session_id,
                    raw_event_ids=raw_event_ids,
                    extractor_version=self._extractor_version,
                )
            except ExtractorParseError as e:
                last_parse_error = str(e)
                warnings.append(f"attempt {attempts}: parse failed: {last_parse_error}")
                continue

            return ExtractionResult(
                candidates=parsed.candidates,
                warnings=warnings + parsed.warnings,
                cap_warning=parsed.cap_warning,
                llm_calls=attempts,
            )

        return ExtractionResult(
            candidates=[],
            warnings=warnings,
            failure_reason=f"parse failed after {max_attempts} attempts; last error: {last_parse_error}",
            llm_calls=attempts,
        )


__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MAX_CHARS_PER_BATCH",
    "DEFAULT_MAX_EVENTS_PER_BATCH",
    "CandidateExtractor",
    "ExtractionResult",
    "extract_session",
]


# ---------------------------------------------------------------------------
# Convenience: glue extractor + Memory storage
# ---------------------------------------------------------------------------


def extract_session(
    *,
    memory: object,
    extractor: CandidateExtractor,
    session_id: str,
    persist: bool = True,
) -> ExtractionResult:
    """Run the extractor against all raw events of a single session, optionally persist.

    This is the host-agnostic entry point referenced by D26: callers (CLI
    today; ``api.on('session_end')`` once the OpenClaw adapter is wired)
    pass in a ``Memory`` instance and a session id, and we:

    1. Pull raw events for that session from ``memory.list_raw``
    2. Run ``extractor.extract`` on them
    3. If ``persist`` is True, save the resulting candidates via
       ``memory.add_candidates``

    The function intentionally takes ``memory: object`` (not the concrete
    ``Memory`` class) to avoid a circular import; we duck-type the two
    methods we need (``list_raw`` and ``add_candidates``).

    Promotion is **not** triggered here — that lives in M2 task 2.5
    (``promotion`` module). Until 2.5 lands, candidates sit in
    ``status="pending"`` and the user can inspect them via
    ``memory candidate list``.
    """
    raw_events: list[RawEvent] = memory.list_raw(session_id=session_id, limit=10_000)  # type: ignore[attr-defined]
    if not raw_events:
        return ExtractionResult(candidates=[])

    result = extractor.extract(raw_events, session_id=session_id)

    if persist and result.candidates:
        memory.add_candidates(result.candidates)  # type: ignore[attr-defined]

    return result
