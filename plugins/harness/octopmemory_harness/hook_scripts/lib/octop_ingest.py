#!/usr/bin/env python3
"""Parse a terminal transcript locally and upload it as ``memory_capture`` events.

The MCP server no longer parses transcripts: the client does it and sends the
already-typed events, so redaction / echo protection / role filters run once on the
server through ``MemoryRuntime.capture``.

Called by the hook transport (``octop-memory-client.mjs``) for every host; reads the
hook's ``config.json`` for endpoint and token and prints one JSON line on stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from transcript import _PARSERS, SESSION_PREFIXES  # noqa: E402

BATCH_ENV = "OCTOP_INGEST_BATCH_SIZE"
DEFAULT_BATCH_SIZE = 200


def _batch_size() -> int:
    """Events per request; keep in sync with the server's batch cap (200)."""
    try:
        value = int(os.environ.get(BATCH_ENV) or DEFAULT_BATCH_SIZE)
    except ValueError:
        return DEFAULT_BATCH_SIZE
    return max(1, min(value, 500))


def _tool_error(body: dict) -> str:
    """MCP SDK wraps tool exceptions as HTTP 200 + ``result.isError=true``."""
    result = body.get("result")
    if not isinstance(result, dict) or not result.get("isError"):
        return ""
    parts = result.get("content")
    if isinstance(parts, list):
        text = " ".join(
            str(part.get("text") or "") for part in parts if isinstance(part, dict)
        ).strip()
        if text:
            return text
    return "MCP tool returned isError=true"


def _tool_ok(body: dict) -> bool:
    result = body.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return False
    structured = result.get("structuredContent")
    return isinstance(structured, dict) and "accepted" in structured


SKIP_MARKERS = (
    "[octop memory ",
    "## Memory Recall",
    "[memory] Earlier in this workspace",
    "[octop-memory hook]",
)


def _config(path: str) -> dict:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except OSError:
        return {}


def _headers(config: dict) -> dict[str, str]:
    header = config.get("tokenHeader") or "Authorization"
    token = config.get("token") or ""
    common = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if config.get("client"):
        common["X-Octop-Client"] = str(config["client"])
    if header.lower() == "authorization":
        return {"Authorization": f"Bearer {token}", **common}
    return {header: token, **common}


def _post(url: str, headers: dict[str, str], body: dict, timeout: float = 60.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-configured endpoint
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:  # still a valid JSON-RPC error body
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except OSError as exc:
        return 0, {"error": str(exc)}

    payload = raw
    for line in raw.splitlines():
        if line.startswith("data: "):
            payload = line[6:]
            break
    try:
        parsed = json.loads(payload[payload.index("{") : payload.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return status, {"error": raw[:300]}
    return status, parsed


def _split_complete_lines(raw: bytes, start: int) -> tuple[list[bytes], bytes]:
    """Return complete lines from ``start``; a half-written trailing line is left out."""
    tail = raw[start:]
    parts = tail.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
        return parts, (b"\n".join(parts) + b"\n" if parts else b"")
    if len(parts) > 1:
        parts = parts[:-1]
        return parts, b"\n".join(parts) + b"\n"
    return [], b""


def _record_spans(complete: bytes) -> list[tuple[int, int]]:
    """Byte spans of parsed JSONL records, in parser index order.

    Mirrors ``transcript._iter_records``: blank/malformed lines are skipped and do
    not advance the record index.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in complete.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            try:
                json.loads(stripped)
            except ValueError:
                pass
            else:
                spans.append((offset, offset + len(line)))
        offset += len(line)
    return spans


_LINE_KEY = re.compile(r"^line(\d+):(\d+)$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--session", default="")
    parser.add_argument("--path", required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--prefix", default="")
    args = parser.parse_args(argv)

    config = _config(args.config)
    url = (config.get("url") or "").rstrip("/")
    if not url or not config.get("token"):
        print(json.dumps({"ok": False, "error": "hook config has no url/token"}))
        return 2

    try:
        raw = Path(args.path).read_bytes()
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"read failed: {exc}"}))
        return 2

    start = args.offset if 0 <= args.offset <= len(raw) else 0
    if start and raw[start - 1 : start] != b"\n":
        start = 0

    _lines, complete = _split_complete_lines(raw, start)
    text = complete.decode("utf-8", "replace")

    parse = _PARSERS.get(args.source)
    if parse is None:
        print(json.dumps({"ok": False, "error": f"unknown source {args.source!r}"}))
        return 2
    full = parse(text)

    session_id = full.session_id or args.session
    if not session_id:
        print(json.dumps({"ok": False, "error": "no session id"}))
        return 2
    prefix = args.prefix or SESSION_PREFIXES.get(args.source, "")
    session = f"{prefix}{session_id}"

    # Codex/Claude fall back to ``line{index}`` inside each parse; index restarts for
    # every delta, so translate it to the record's absolute byte position. That key is
    # stable across batches and across Stop increments, so server-side dedupe cannot
    # collapse two different messages.
    spans = _record_spans(complete)
    entries: list[tuple[dict, int | None]] = []
    remaining: dict[int, int] = {}
    for event in full.events:
        content = event.content.strip()
        if not content or any(marker in content for marker in SKIP_MARKERS):
            continue
        payload = dict(event.payload or {})
        record: int | None = None
        match = _LINE_KEY.match(str(payload.get("src_key") or ""))
        if match:
            index = int(match.group(1))
            if index < len(spans):
                record = index
                payload["src_key"] = f"pos:{start + spans[index][0]}:{match.group(2)}"
        entries.append(
            (
                {
                    "event_type": event.event_type,
                    "content": content,
                    "payload": payload,
                    "timestamp_iso": event.timestamp.isoformat(),
                },
                record,
            )
        )
        if record is not None:
            remaining[record] = remaining.get(record, 0) + 1

    if not entries:
        print(json.dumps({"ok": True, "status": 200, "imported": 0, "offset": len(raw)}))
        return 0

    batch_limit = _batch_size()
    next_record = 0
    safe_end = start
    # Events whose key is not line-based (uuid / content-hash parsers) cannot be
    # mapped to a record boundary; stay conservative and only advance to EOF after
    # every batch succeeded.
    has_unmapped = any(record is None for _, record in entries)

    def _advance(record: int) -> None:
        nonlocal next_record, safe_end
        if has_unmapped:
            return
        while next_record <= record and remaining.get(next_record, 0) == 0:
            if next_record < len(spans):
                safe_end = start + spans[next_record][1]
            next_record += 1

    def _send(events: list[dict]) -> tuple[bool, str, int]:
        status, body = _post(
            f"{url}/",
            _headers(config),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "memory_capture",
                    "arguments": {
                        "events": events,
                        "source": args.source,
                        "session_id": session,
                        "extract": True,
                    },
                },
            },
        )
        top_error = body.get("error")
        tool_error = _tool_error(body)
        ok = (
            200 <= status < 300
            and not top_error
            and not tool_error
            and _tool_ok(body)
        )
        message = ""
        if not ok:
            message = (
                (top_error or {}).get("message")
                if isinstance(top_error, dict)
                else (tool_error or f"HTTP {status}: unexpected tool result")
            ) or "capture failed"
        structured = body.get("result", {}).get("structuredContent") or {}
        return ok, message, int(structured.get("accepted") or structured.get("imported") or 0)

    imported = 0
    batches = 0
    ok = True
    error = ""
    committed = start
    batch: list[dict] = []

    def _flush(final: bool) -> bool:
        nonlocal imported, batches, committed, batch, ok, error
        if not batch:
            return True
        good, message, accepted = _send(list(batch))
        batches += 1
        if not good:
            ok, error = False, message
            return False
        imported += accepted
        batch = []
        if final:
            committed = start + len(complete)
        elif not has_unmapped:
            committed = safe_end
        return True

    for item, record in entries:
        batch.append(item)
        if record is not None:
            remaining[record] -= 1
            if remaining[record] == 0:
                _advance(record)
        if len(batch) >= batch_limit and not _flush(final=False):
            break
    else:
        # An exact multiple of the limit is already flushed with final=False,
        # so commit the consumed bytes here once every batch succeeded.
        if _flush(final=True):
            committed = start + len(complete)

    print(
        json.dumps(
            {
                "ok": ok,
                "status": 200 if ok else 0,
                "imported": imported,
                "batches": batches,
                "offset": committed,
                "error": error,
            }
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
