"""Codex setup keeps hook trust positions and registers the tools without storing a second token."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path, PureWindowsPath

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from octopmemory_harness import installer  # noqa: E402

URL = "https://memory.example.test/mcp/"
TOKEN = "omcp_test_credential"


@pytest.fixture
def codex_install(tmp_path, monkeypatch):
    user_dir = tmp_path / "test user"
    codex = user_dir / ".codex"
    codex.mkdir(parents=True)
    default_expanduser = os.path.expanduser
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(user_dir) if path == "~" else default_expanduser(path))
    environment = {
        "OCTOP_URL": URL,
        "OCTOP_TOKEN": TOKEN,
        "OCTOP_SERVER_NAME": "octop-memory",
        "OCTOP_NODE": shutil.which("node") or "node",
        "OCTOP_PROJECT_DIR": "",
        "OCTOP_TERMINALS": "codex",
        "OCTOP_HOOK_WRAPPER": "cmd" if os.name == "nt" else "sh",
    }
    for host, suffix in (("claude-code", "CC"), ("kiro", "KIRO"), ("codex", "CODEX"), ("cursor", "CURSOR")):
        environment[f"OCTOP_{suffix}_DIR"] = str(codex / "hooks")
        environment["OCTOP_FILES_" + host.upper().replace("-", "_")] = installer._host_manifest(host)
    environment["OCTOP_DSH_DIR"] = str(user_dir / ".dsh" / "hooks")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    for key in ("OCTOP_HOOK_URL", "OCTOP_ENV_FILE", "OCTOP_CODEX_READ_ONLY", "OCTOP_CODEX_EXTRACTION"):
        monkeypatch.delenv(key, raising=False)
    return codex


def _read_toml(codex: Path):
    return tomllib.loads((codex / "config.toml").read_text(encoding="utf-8"))


def test_reinstall_keeps_hook_order_and_adopts_existing_mcp(codex_install):
    codex = codex_install
    config = codex / "config.toml"
    helper = f"local reader of {codex / 'hooks' / 'config.json'}"
    config.write_text(
        f"""
[mcp_servers."octop-memory"]
url = {json.dumps(URL)}
http_headers_helper = {json.dumps(helper)}
enabled_tools = [
    "memory_get",
]
tool_timeout_sec = 45

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = {json.dumps(str(codex / "hooks/codex/stop-capture.sh"))}

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "personal-stop-hook"

[hooks.state."test-stop:1:0"]
enabled = false
""",
        encoding="utf-8",
    )
    before = _read_toml(codex)
    exec(installer._MERGE_PY, {})
    after = _read_toml(codex)

    assert after["hooks"]["Stop"] == before["hooks"]["Stop"]
    assert after["hooks"]["state"] == before["hooks"]["state"]
    assert list(after["mcp_servers"]) == ["octop-memory"]
    server = after["mcp_servers"]["octop-memory"]
    assert server["enabled_tools"] == ["memory_get", "memory_save"]
    assert server["tool_timeout_sec"] == 45
    assert "mcp-headers." in server["http_headers_helper"]
    assert TOKEN not in config.read_text(encoding="utf-8")
    first_install = config.read_bytes()
    exec(installer._MERGE_PY, {})
    assert config.read_bytes() == first_install


def test_existing_hooks_json_read_only_install_and_uninstall(codex_install, monkeypatch):
    codex = codex_install
    settings = codex / "hooks.json"
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "personal-stop-hook"}]}]}}
    settings.write_text(json.dumps(existing), encoding="utf-8")
    monkeypatch.setenv("OCTOP_CODEX_READ_ONLY", "1")
    exec(installer._MERGE_PY, {})
    exec(installer._MERGE_PY, {})

    config = _read_toml(codex)
    assert "hooks" not in config
    assert config["mcp_servers"]["octop-memory"]["enabled_tools"] == ["memory_get"]
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    assert hooks["Stop"][:1] == existing["hooks"]["Stop"]
    assert len(hooks["Stop"]) == 2 and len(hooks["UserPromptSubmit"]) == 1
    credentials = json.loads((codex / "hooks" / "config.json").read_text(encoding="utf-8"))
    assert credentials["readOnly"] is True
    assert credentials["token"] == TOKEN

    if node := shutil.which("node"):
        helper = codex / "hooks" / "codex" / "mcp-headers.mjs"
        header = subprocess.check_output([node, str(helper)], text=True)
        assert json.loads(header) == {"Authorization": f"Bearer {TOKEN}"}
        capture = codex / "hooks" / "codex" / "stop-capture.mjs"
        subprocess.run(
            [node, str(capture)],
            input=json.dumps({"transcript_path": str(codex / "missing.jsonl")}),
            text=True,
            env={**os.environ, "OCTOP_CAPTURE_SYNC": "1"},
            check=True,
        )
        assert not (codex / "hooks" / "octop-hook.log").exists()

    exec(installer._UNINSTALL_PY, {})
    assert _read_toml(codex) == {}
    assert json.loads(settings.read_text(encoding="utf-8")) == existing
    assert not (codex / "hooks" / "config.json").exists()


def test_escaped_windows_path_remains_installer_owned(codex_install):
    scope = {}
    exec(installer._MERGE_PY, scope)
    scripts = PureWindowsPath(r"C:\Users\me\.codex\hooks")
    helper = scripts / "codex" / "mcp-headers.cmd"
    lines = ["[mcp_servers.octop-memory]\n", f"http_headers_helper = {json.dumps(str(helper))}\n"]
    assert scope["_codex_mcp_managed"](lines, scripts)


def test_client_extraction_only_exposes_proposals_on_the_selected_writable_server(codex_install, monkeypatch):
    codex = codex_install
    (codex / "config.toml").write_text('[mcp_servers.octop-memory]\nurl="https://other.test/mcp/"\n')
    monkeypatch.setenv("OCTOP_CODEX_EXTRACTION", "codex")
    exec(installer._MERGE_PY, {})
    monkeypatch.delenv("OCTOP_CODEX_EXTRACTION")
    exec(installer._MERGE_PY, {})
    config = json.loads((codex / "hooks/config.json").read_text())
    assert config["mcpServer"] == "octop-memory-2" and config["codexExtraction"] == "codex"
    assert "memory_propose" in _read_toml(codex)["mcp_servers"][config["mcpServer"]]["enabled_tools"]
    monkeypatch.setenv("OCTOP_CODEX_READ_ONLY", "1")
    exec(installer._MERGE_PY, {})
    assert _read_toml(codex)["mcp_servers"][config["mcpServer"]]["enabled_tools"] == ["memory_get"]
    assert "codexExtraction" not in json.loads((codex / "hooks/config.json").read_text())


@pytest.mark.skipif(os.name == "nt" or not shutil.which("node"), reason="requires a POSIX command hook")
def test_stop_requests_one_review_only_with_evidence(codex_install, tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOP_CODEX_EXTRACTION", "codex")
    exec(installer._MERGE_PY, {})
    fake_ingest = tmp_path / "ingest"
    fake_ingest.write_text("#!/bin/sh\nprintf '%s\\n' \"$TEST_CAPTURE_RESPONSE\"\n")
    fake_ingest.chmod(0o700)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    hook = codex_install / "hooks/codex/stop-capture.mjs"
    receipts = tmp_path / "requests.jsonl"
    mock_fetch = tmp_path / "fetch.cjs"
    mock_fetch.write_text(
        "globalThis.fetch = async (_url, init) => {"
        "require('node:fs').appendFileSync(process.env.TEST_RECEIPTS, init.body + '\\n');"
        "return new Response(JSON.stringify({result:{structuredContent:{accepted:0}}})); };"
    )

    def stop(active, evidence):
        result = {"ok": True, "offset": 0, "user_event_ids": evidence}
        env = {**os.environ, "OCTOP_CAPTURE_SYNC": "1", "OCTOP_PYTHON": str(fake_ingest),
               "TEST_CAPTURE_RESPONSE": json.dumps(result), "TEST_RECEIPTS": str(receipts),
               "NODE_OPTIONS": f'--require "{mock_fetch}"'}
        payload = {"session_id": "test", "turn_id": "one", "hook_event_name": "Stop", "stop_hook_active": active,
                   "transcript_path": str(transcript)}
        return subprocess.check_output(["node", str(hook)], input=json.dumps(payload), text=True, env=env)

    event = "evt_abcdef123456"
    review = json.loads(stop(False, [event]))
    assert review["decision"] == "block" and event in review["reason"]
    assert "In every case, end this continuation with an empty final response." in review["reason"]
    assert "Do not send chat text before or after a tool call" in review["reason"]
    assert stop(True, [event]) == "" and stop(False, []) == ""
    [request] = [json.loads(line) for line in receipts.read_text().splitlines()]
    assert request["params"] == {"name": "memory_capture", "arguments": {
        "source": "codex", "session_id": "codex:test", "extract": False, "completed_review_turn": "one",
    }}


def test_same_url_with_independent_credentials_keeps_both_spaces(codex_install):
    codex = codex_install
    config = codex / "config.toml"
    config.write_text(
        f'[mcp_servers.octop-memory]\nurl = {json.dumps(URL)}\nbearer_token_env_var = "OTHER_SPACE_TOKEN"\n',
        encoding="utf-8",
    )
    previous = _read_toml(codex)["mcp_servers"]["octop-memory"]
    exec(installer._MERGE_PY, {})
    servers = _read_toml(codex)["mcp_servers"]
    assert servers["octop-memory"] == previous
    assert servers["octop-memory-2"]["enabled_tools"] == ["memory_get", "memory_save"]


def test_project_mcp_keeps_credentials_in_the_user_directory(codex_install, tmp_path, monkeypatch):
    codex = codex_install
    project = tmp_path / "project"
    monkeypatch.setenv("OCTOP_PROJECT_DIR", str(project))
    exec(installer._MERGE_PY, {})
    project_config = project / ".codex" / "config.toml"
    text = project_config.read_text(encoding="utf-8")
    helper = tomllib.loads(text)["mcp_servers"]["octop-memory"]["http_headers_helper"]
    assert (codex / "octop-memory-projects").as_posix() in helper.replace("\\", "/")
    assert TOKEN not in text
    assert len(list((codex / "octop-memory-projects").glob("*/config.json"))) == 1


def test_delegate_review_uses_explicit_octop_endpoint_and_resets_on_switch(codex_install, monkeypatch):
    config = codex_install / "hooks/config.json"
    monkeypatch.setenv("OCTOP_CODEX_EXTRACTION", "octop")
    monkeypatch.setenv("OCTOP_CODEX_REVIEW_URL", "http://example.test/api/memory-mcp/codex-review")
    with pytest.raises(ValueError, match="HTTPS"):
        exec(installer._MERGE_PY, {})
    url = "https://octop.example/api/memory-mcp/codex-review"
    monkeypatch.setenv("OCTOP_CODEX_REVIEW_URL", url)
    exec(installer._MERGE_PY, {})
    data = json.loads(config.read_text())
    assert data["codexExtraction"] == "octop" and data["codexReviewUrl"] == url
    monkeypatch.setenv("OCTOP_CODEX_EXTRACTION", "codex")
    exec(installer._MERGE_PY, {})
    data = json.loads(config.read_text())
    assert data["codexExtraction"] == "codex" and "codexReviewUrl" not in data
