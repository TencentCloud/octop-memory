"""Source identities used by raw capture and by the legacy receipt backfill."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from octop_memory.types import RawEvent


def capture_event_key(payload: dict[str, Any], event: dict[str, Any]) -> str:
    """Stable per-event id inside a space/session scope (client ``src_key`` first)."""
    src_key = str(payload.get("src_key") or "").strip()
    if src_key:
        return f"src:{src_key}"
    digest = hashlib.sha256()
    digest.update(str(event.get("event_type") or "").encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(str(event.get("content") or "").encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(str(event.get("timestamp_iso") or "").encode("utf-8", "replace"))
    return f"hash:{digest.hexdigest()}"


def stored_event_key(event: RawEvent) -> str:
    stored = event.payload.get("octop_event_key")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return capture_event_key(
        event.payload,
        {"event_type": event.event_type, "content": event.content, "timestamp_iso": event.timestamp.isoformat()},
    )


def receipt_key(caller: str, session: str, *source: str) -> str:
    """The two-field digest marks an indexed session; three fields identify a delivery."""
    data = json.dumps([caller, session, *source], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def verify_capture_revision(previous: RawEvent, incoming: RawEvent, *, compare_content: bool = True) -> None:
    if previous.event_type != incoming.event_type or (compare_content and previous.content != incoming.content):
        raise ValueError("Capture source key has different evidence. Send a new source key for a new revision.")
