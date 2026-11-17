"""Ingest batching / MCP tool-error handling (review: no silent offset advance)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LIB = PLUGIN_ROOT / "octopmemory_harness" / "hook_scripts" / "lib"
sys.path.insert(0, str(LIB))

import octop_ingest  # noqa: E402


class _FakeParser:
    """One event per non-empty line, stable src_key per line."""

    def __init__(self) -> None:
        self.session_id = "sess-1"

    def __call__(self, text: str):
        events = []
        for line in text.splitlines():
            if not line.strip():
                continue
            events.append(
                SimpleNamespace(
                    event_type="user_message",
                    content=line,
                    payload={"src_key": f"line:{line}"},
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        return SimpleNamespace(events=events, session_id=self.session_id)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"url": "http://stub/mcp/memory/", "token": "omcp_x"}))
    return path


def _run(monkeypatch, tmp_path, lines: int, responses):
    parser = _FakeParser()
    monkeypatch.setitem(octop_ingest._PARSERS, "fake", parser)
    calls = []
    responses = iter(responses)

    def fake_post(url, headers, body, timeout=60.0):
        calls.append(body["params"]["arguments"])
        return 200, next(responses)

    monkeypatch.setattr(octop_ingest, "_post", fake_post)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(f"line-{i}" for i in range(lines)) + "\n")
    out = tmp_path / "out.json"
    code = octop_ingest.main(
        [
            "--config",
            str(_config(tmp_path)),
            "--source",
            "fake",
            "--path",
            str(transcript),
        ]
    )
    assert code in (0, 1)
    return json.loads(out.read_text()) if out.exists() else None, calls, transcript


def test_events_are_sent_in_batches_and_offset_is_eof(monkeypatch, tmp_path, capsys):
    ok_body = {
        "result": {
            "isError": False,
            "structuredContent": {"accepted": 200},
        }
    }
    _, calls, _ = _run(monkeypatch, tmp_path, 401, [ok_body, ok_body, ok_body])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["batches"] == 3
    assert payload["offset"] == (tmp_path / "t.jsonl").stat().st_size
    # Every batch carries the same stable session + per-event src_key so the
    # server can dedupe a retry end to end.
    sessions = {call["session_id"] for call in calls}
    assert sessions == {"sess-1"}
    for call in calls:
        assert all(event["payload"]["src_key"] for event in call["events"])


def test_tool_error_does_not_advance_offset(monkeypatch, tmp_path, capsys):
    ok_body = {"result": {"isError": False, "structuredContent": {"accepted": 3}}}
    err_body = {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": "storage exploded"}],
        }
    }
    _run(monkeypatch, tmp_path, 250, [ok_body, err_body, ok_body])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "storage exploded" in payload["error"]
    # First batch of 200 lines was accepted; the failed batch must stay unconsumed.
    consumed = payload["offset"]
    assert consumed > 0
    assert consumed < (tmp_path / "t.jsonl").stat().st_size


def test_partial_trailing_line_is_not_consumed(monkeypatch, tmp_path, capsys):
    ok_body = {"result": {"isError": False, "structuredContent": {"accepted": 1}}}
    parser = _FakeParser()
    monkeypatch.setitem(octop_ingest._PARSERS, "fake", parser)
    monkeypatch.setattr(octop_ingest, "_post", lambda *a, **k: (200, ok_body))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("line-a\nline-b\npartial")
    octop_ingest.main(
        ["--config", str(_config(tmp_path)), "--source", "fake", "--path", str(transcript)]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["offset"] == len(b"line-a\nline-b\n")
