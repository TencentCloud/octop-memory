"""Windows installers must render PowerShell, not POSIX shell (review P1)."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from octopmemory_harness import cli  # noqa: E402


def _fake_run(monkeypatch):
    captured = {}

    def run(script, *, suffix, env):
        captured["script"] = script
        captured["suffix"] = suffix
        captured["env"] = env
        return 0

    monkeypatch.setattr(cli, "_run_script", run)
    return captured


def test_windows_install_uses_powershell_renderer(monkeypatch):
    captured = _fake_run(monkeypatch)
    monkeypatch.setattr(cli.os, "name", "nt")
    cli.main(["install", "--url", "https://x/mcp/memory/", "--token", "omcp_x", "--terminals", "claude-code"])
    assert captured["suffix"] == ".ps1"
    assert captured["script"].lstrip().startswith("# octop memory")
    assert "$env:OCTOP_TOKEN" in captured["script"] or "OCTOP_TOKEN" in captured["script"]


def test_windows_uninstall_uses_powershell_renderer(monkeypatch):
    captured = _fake_run(monkeypatch)
    monkeypatch.setattr(cli.os, "name", "nt")
    cli.main(["uninstall", "--terminals", "claude-code"])
    assert captured["suffix"] == ".ps1"


def test_posix_install_still_renders_shell(monkeypatch):
    captured = _fake_run(monkeypatch)
    monkeypatch.setattr(cli.os, "name", "posix")
    cli.main(["install", "--url", "https://x/mcp/memory/", "--token", "omcp_x", "--terminals", "claude-code"])
    assert captured["suffix"] == ".sh"
    assert captured["script"].startswith("#!/bin/sh")
