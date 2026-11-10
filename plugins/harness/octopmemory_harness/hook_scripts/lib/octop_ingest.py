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
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from transcript import _PARSERS, SESSION_PREFIXES  # noqa: E402

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
    text = raw[start:].decode("utf-8", "replace")

    parse = _PARSERS.get(args.source)
    if parse is None:
        print(json.dumps({"ok": False, "error": f"unknown source {args.source!r}"}))
        return 2
    parsed = parse(text)

    events: list[dict] = []
    for event in parsed.events:
        content = event.content.strip()
        if not content or any(marker in content for marker in SKIP_MARKERS):
            continue
        item: dict = {
            "event_type": event.event_type,
            "content": content,
            "payload": dict(event.payload or {}),
            "timestamp_iso": event.timestamp.isoformat(),
        }
        events.append(item)

    if not events:
        print(json.dumps({"ok": True, "status": 200, "imported": 0, "offset": len(raw)}))
        return 0

    session_id = parsed.session_id or args.session
    if not session_id:
        print(json.dumps({"ok": False, "error": "no session id"}))
        return 2
    prefix = args.prefix or SESSION_PREFIXES.get(args.source, "")

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
                    "session_id": f"{prefix}{session_id}",
                    "extract": True,
                },
            },
        },
    )
    result = body.get("result", {}).get("structuredContent") or {}
    ok = 200 <= status < 300 and "error" not in body
    print(
        json.dumps(
            {
                "ok": ok,
                "status": status,
                "imported": result.get("accepted", 0),
                "offset": len(raw) if ok else start,
                "error": body.get("error", {}).get("message") if body.get("error") else "",
            }
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
