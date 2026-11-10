"""``octop-memory-harness`` — run the terminal installer locally.

The scripts the Octop server used to stream at ``/mcp/memory/setup`` are rendered here
instead, so the client experience no longer depends on the server serving client code.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import installer


def _run_script(script: str, *, suffix: str, env: dict[str, str]) -> int:
    fd, raw_path = tempfile.mkstemp(prefix="octop-memory-harness-", suffix=suffix)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script)
        if suffix == ".ps1":
            cmd = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(path),
            ]
        else:
            cmd = ["sh", str(path)]
        return subprocess.run(cmd, env=env).returncode
    finally:
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="octop-memory-harness",
        description="把本机终端（Claude Code / Codex / Cursor / Kiro / DSH）接到 octop-memory MCP。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="安装 hooks 与 MCP 条目")
    install.add_argument(
        "--url",
        required=True,
        help="MCP 地址，例如 https://octop.example.com/mcp/memory/",
    )
    install.add_argument(
        "--token",
        default=None,
        help="用户级 MCP token（omcp_…）；也可以先设 OCTOP_MEMORY_TOKEN",
    )
    install.add_argument("--terminals", default="", help="逗号分隔：claude-code,codex,cursor,kiro,dsh")
    install.add_argument("--project", default="", help="只装到某个项目目录（默认全局）")

    uninstall = sub.add_parser("uninstall", help="卸载 hooks 与 MCP 条目")
    uninstall.add_argument("--terminals", default="")
    uninstall.add_argument("--project", default="")

    args = parser.parse_args(argv)
    env = dict(os.environ)

    if args.command == "install":
        token = args.token or env.get("OCTOP_MEMORY_TOKEN", "")
        if not token:
            parser.error("缺少 token：用 --token 或 OCTOP_MEMORY_TOKEN 提供")
        env["OCTOP_MEMORY_TOKEN"] = token
        env["OCTOP_TERMINALS"] = args.terminals
        env["OCTOP_PROJECT_DIR"] = args.project
        script = installer.render_setup_script(
            mcp_url=args.url,
            terminals=args.terminals,
            project=args.project,
        )
    else:
        env["OCTOP_TERMINALS"] = args.terminals
        env["OCTOP_PROJECT_DIR"] = args.project
        script = installer.render_uninstall_script(
            terminals=args.terminals,
            project=args.project,
        )

    return _run_script(
        script,
        suffix=".ps1" if os.name == "nt" else ".sh",
        env=env,
    )


if __name__ == "__main__":
    sys.exit(main())
