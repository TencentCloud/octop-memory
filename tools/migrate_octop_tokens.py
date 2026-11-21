#!/usr/bin/env python3
"""Migrate legacy Octop ``memory_mcp_tokens`` into space tokens (hash-preserving).

The plaintext token stays valid: only the sha256 digest is copied, so clients do
not need to re-authenticate. Expert ids become spaces ``octop-expert-<agent_id>``;
multi-expert / unrestricted tokens keep access through ``memory_space_grants``.

Usage:
    python tools/migrate_octop_tokens.py --octop-db ~/.octop/octop.db --dry-run
    python tools/migrate_octop_tokens.py --octop-db ~/.octop/octop.db --config ~/.octop-memory/config.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from octop_memory import _config_io
from octop_memory.server.config import load_server_config
from octop_memory.server.control import ControlStore


@dataclass
class LegacyToken:
    token_id: str
    user_id: int
    name: str
    expert_ids: list[str] | None
    token_hash: str
    token_hint: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


def _read_tokens(db: Path) -> list[LegacyToken]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM memory_mcp_tokens").fetchall()
    finally:
        conn.close()
    out: list[LegacyToken] = []
    for row in rows:
        raw = row["expert_ids"]
        experts = json.loads(raw) if raw else None
        out.append(
            LegacyToken(
                token_id=row["token_id"],
                user_id=int(row["user_id"]),
                name=row["name"],
                expert_ids=[str(e) for e in experts] if experts else None,
                token_hash=row["token_hash"],
                token_hint=row["token_hint"],
                created_at=row["created_at"],
                last_used_at=row["last_used_at"],
                revoked_at=row["revoked_at"],
            )
        )
    return out


def _read_agents(db: Path) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
        if not cols:
            return {}
        id_col = "agent_id" if "agent_id" in cols else "id"
        rows = conn.execute(f"SELECT {id_col} AS agent_id, name, user_id FROM agents").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {
        str(row["agent_id"]): {"name": row["name"], "user_id": row["user_id"]}
        for row in rows
    }


def _scopes(token: LegacyToken) -> list[str]:
    # Legacy tokens written by an expert owner could write and review; every
    # token could read. This mirrors the previous capability set.
    return ["read", "write", "review"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--octop-db", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.config:
        _config_io.set_config_path(args.config)
    _server, control_cfg = load_server_config()
    control = ControlStore(control_cfg.sqlite_path)
    agents = _read_agents(args.octop_db)
    tokens = _read_tokens(args.octop_db)

    report: dict[str, Any] = {"migrated": [], "skipped": [], "created_spaces": []}
    for token in tokens:
        experts = token.expert_ids
        if experts is None:
            experts = [
                agent_id
                for agent_id, info in agents.items()
                if int(info.get("user_id") or -1) == token.user_id
            ]
        if not experts:
            report["skipped"].append({"token_id": token.token_id, "reason": "no experts"})
            continue
        spaces = [f"octop-expert-{expert}" for expert in experts]
        if control.token_by_hash(token.token_hash) is not None:
            report["skipped"].append({"token_id": token.token_id, "reason": "hash already migrated"})
            continue
        if args.dry_run:
            report["migrated"].append(
                {
                    "token_id": token.token_id,
                    "name": token.name,
                    "primary_space": spaces[0],
                    "grants": spaces[1:],
                }
            )
            continue
        for space_id in spaces:
            if control.get_space(space_id) is None:
                display = agents.get(space_id.removeprefix("octop-expert-"), {}).get("name") or space_id
                control.create_space(
                    space_id,
                    str(display),
                    "sqlite",
                    {"db_path": f"~/.octop-memory/spaces/{space_id}.db"},
                    created_by="migration",
                )
                report["created_spaces"].append(space_id)
        migrated = control.import_token(
            space_id=spaces[0],
            client_name=token.name,
            scopes=_scopes(token),
            token_hash=token.token_hash,
            token_hint=token.token_hint,
            created_at=token.created_at,
            last_used_at=token.last_used_at,
            revoked_at=token.revoked_at,
            octop_token_id=token.token_id,
        )
        for space_id in spaces[1:]:
            control.add_grant(migrated.token_id, space_id, _scopes(token))
        report["migrated"].append(
            {
                "token_id": token.token_id,
                "memory_token_id": migrated.token_id,
                "primary_space": spaces[0],
                "grants": spaces[1:],
            }
        )

    control.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
