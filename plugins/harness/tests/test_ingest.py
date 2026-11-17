"""Ingest batching / offset / stable keys (review: no silent loss on retry)."""

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


def _event(content: str, key: str):
    return SimpleNamespace(
        event_type="user_message",
        content=content,
        payload={"src_key": key},
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _line_parser(multiblock: bool = False):
    """Mirrors Codex: keys start at line0 for every parse (local index)."""

    def parse(text: str):
        events = []
        for index, line in enumerate(text.splitlines()):
            body = json.loads(line)["line"]
            events.append(_event(body, f"line{index}:0"))
            if multiblock:
                events.append(_event(f"{body}-extra", f"line{index}:1"))
        return SimpleNamespace(events=events, session_id="sess-1")

    return parse


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"url": "http://stub/mcp/memory/", "token": "omcp_x"}))
    return path


def _transcript(tmp_path: Path, lines: int, first: int = 0) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join(json.dumps({"line": f"msg-{i}"}) for i in range(first, first + lines)) + "\n"
    )
    return path


def _run(monkeypatch, tmp_path, parser, path: Path, offset: int = 0, responses=None):
    monkeypatch.setitem(octop_ingest._PARSERS, "fake", parser)
    calls: list[dict] = []
    responses = iter(responses or [])

    def fake_post(url, headers, body, timeout=60.0):
        calls.append(body["params"]["arguments"])
        return 200, next(responses)

    monkeypatch.setattr(octop_ingest, "_post", fake_post)
    code = octop_ingest.main(
        [
            "--config",
            str(_config(tmp_path)),
            "--source",
            "fake",
            "--path",
            str(path),
            "--offset",
            str(offset),
        ]
    )
    return code, calls


def _ok(accepted: int = 200) -> dict:
    return {"result": {"isError": False, "structuredContent": {"accepted": accepted}}}


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_events_are_batched_with_stable_session_and_eof(monkeypatch, tmp_path, capsys):
    path = _transcript(tmp_path, 401)
    _code, calls = _run(monkeypatch, tmp_path, _line_parser(), path, responses=[_ok(), _ok(), _ok()])
    payload = _out(capsys)

    assert payload["ok"] is True
    assert payload["batches"] == 3
    assert [len(call["events"]) for call in calls] == [200, 200, 1]
    assert payload["offset"] == path.stat().st_size
    assert {call["session_id"] for call in calls} == {"sess-1"}
    # Absolute-position keys: unique across batches.
    keys = [event["payload"]["src_key"] for call in calls for event in call["events"]]
    assert len(keys) == len(set(keys)) == 401


def _uuid_parser():
    """Mirrors Claude uuid keys: stable ids, never ``lineN`` (mapped=False)."""

    def parse(text: str):
        events = []
        for index, line in enumerate(text.splitlines()):
            body = json.loads(line)["line"]
            events.append(_event(body, f"uuid-{body}:0"))
        return SimpleNamespace(events=events, session_id="sess-1")

    return parse


def test_exactly_one_batch_of_unmapped_events_advances_to_eof(monkeypatch, tmp_path, capsys):
    """Review: N == batch_limit must still advance offset when every batch succeeded."""
    monkeypatch.setenv("OCTOP_INGEST_BATCH_SIZE", "200")
    path = _transcript(tmp_path, 200)
    _code, calls = _run(monkeypatch, tmp_path, _uuid_parser(), path, responses=[_ok(200)])
    payload = _out(capsys)
    assert len(calls) == 1
    assert payload["ok"] is True
    assert payload["offset"] == path.stat().st_size


def test_exact_multiple_batches_advance_from_nonzero_offset(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OCTOP_INGEST_BATCH_SIZE", "200")
    path = _transcript(tmp_path, 400)
    _code, _calls = _run(
        monkeypatch, tmp_path, _uuid_parser(), path, responses=[_ok(200), _ok(200)]
    )
    payload = _out(capsys)
    assert payload["batches"] == 2
    assert payload["offset"] == path.stat().st_size

    # Continue from the saved offset with another exact batch.
    path.write_text(path.read_text() + "".join(
        json.dumps({"line": f"msg-{i}"}) + "\n" for i in range(400, 600)
    ))
    _code, _calls = _run(
        monkeypatch, tmp_path, _uuid_parser(), path,
        offset=payload["offset"], responses=[_ok(200)],
    )
    payload2 = _out(capsys)
    assert payload2["offset"] == path.stat().st_size


def test_multiblock_record_is_batched_by_event_count(monkeypatch, tmp_path, capsys):
    """A JSONL line can parse into multiple events; batches count events, not lines."""
    path = _transcript(tmp_path, 101)
    _code, calls = _run(
        monkeypatch, tmp_path, _line_parser(multiblock=True), path, responses=[_ok(), _ok()]
    )
    payload = _out(capsys)

    assert payload["ok"] is True
    assert [len(call["events"]) for call in calls] == [200, 2]
    assert payload["offset"] == path.stat().st_size


def test_codex_keys_stay_unique_across_increments(monkeypatch, tmp_path, capsys):
    """A later Stop parses only the delta; ``line0`` must still not collide."""
    parser = _line_parser()
    path = _transcript(tmp_path, 2)
    _code, first_calls = _run(monkeypatch, tmp_path, parser, path, responses=[_ok()])
    first = _out(capsys)
    assert first["ok"] is True

    # Append one more record and report from the saved offset.
    path.write_text(path.read_text() + json.dumps({"line": "msg-2"}) + "\n")
    _code, second_calls = _run(
        monkeypatch, tmp_path, parser, path, offset=first["offset"], responses=[_ok()]
    )
    second = _out(capsys)

    assert second["ok"] is True
    first_keys = {event["payload"]["src_key"] for event in first_calls[0]["events"]}
    second_keys = {event["payload"]["src_key"] for event in second_calls[0]["events"]}
    assert second_keys == {f"pos:{first['offset']}:0"}
    assert not (first_keys & second_keys)


def test_tool_error_does_not_advance_past_failed_batch(monkeypatch, tmp_path, capsys):
    path = _transcript(tmp_path, 250)
    err = {"result": {"isError": True, "content": [{"type": "text", "text": "storage exploded"}]}}
    _code, _calls = _run(
        monkeypatch, tmp_path, _line_parser(), path, responses=[_ok(3), err, _ok(3)]
    )
    payload = _out(capsys)

    assert payload["ok"] is False
    assert "storage exploded" in payload["error"]
    assert 0 < payload["offset"] < path.stat().st_size


def test_partial_trailing_line_is_not_consumed(monkeypatch, tmp_path, capsys):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"line": "a"}) + "\n" + json.dumps({"line": "b"}) + "\npartial")
    _code, _calls = _run(monkeypatch, tmp_path, _line_parser(), path, responses=[_ok(2)])
    payload = _out(capsys)

    consumed = len((json.dumps({"line": "a"}) + "\n").encode()) + len(
        (json.dumps({"line": "b"}) + "\n").encode()
    )
    assert payload["offset"] == consumed
