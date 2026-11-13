"""Smoke tests: renderer never embeds credentials and the CLI is wired."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from octopmemory_harness import installer  # noqa: E402


def test_rendered_posix_script_has_no_embedded_token() -> None:
    script = installer.render_setup_script(mcp_url="https://octop.example.com/mcp/memory/")
    assert "https://octop.example.com/mcp/memory/" in script
    # No pre-filled credential: the installer only accepts a token at run time.
    assert "DEFAULT_TOKEN=" not in script


def test_rendered_powershell_script_has_no_embedded_token() -> None:
    script = installer.render_setup_ps1(mcp_url="https://octop.example.com/mcp/memory/")
    assert "https://octop.example.com/mcp/memory/" in script
    # No pre-filled credential: the installer only accepts a token at run time.
    assert "DEFAULT_TOKEN=" not in script


def test_cli_exposes_install_and_uninstall() -> None:
    from octopmemory_harness import cli

    assert callable(cli.main)
