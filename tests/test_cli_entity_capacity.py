"""Tests for the read-only entity capacity-report CLI command."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from octop_memory.adapters.cli import main
from octop_memory.core import Memory
from octop_memory.types import Entity

_NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


def _seed(db_path: str) -> None:
    mem = Memory(namespace="test", backend_config={"db_path": db_path})
    for index, (name, count) in enumerate(
        (("wide-topic", 250), ("mid-topic", 60), ("small-topic", 5)),
    ):
        mem.add_entity(
            Entity(
                id=f"ent-{index}",
                entity_type="Project",
                canonical_name=name,
                aliases=[name],
                atom_count=count,
                created_at=_NOW,
            )
        )


class TestEntityCapacityReport:
    def test_json_reports_buckets_coverage_and_shard_hint(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        _seed(db_path)
        result = CliRunner().invoke(
            main,
            [
                "--db",
                db_path,
                "--namespace",
                "test",
                "--json",
                "entity",
                "capacity-report",
                "--threshold",
                "200",
                "--top",
                "5",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["entities_scanned"] == 3
        assert payload["total_atoms"] == 315
        assert payload["wide_entity_count"] == 1
        assert payload["wide_atom_count"] == 250
        assert payload["buckets"]["<=50"]["entities"] == 1
        assert payload["buckets"]["51-150"]["entities"] == 1
        assert payload["buckets"]["201-300"]["entities"] == 1

        wide = payload["wide_entities"][0]
        assert wide["canonical_name"] == "wide-topic"
        assert wide["atom_count"] == 250
        # 50-atom regen window over 250 atoms.
        assert wide["summary_coverage"] == 0.2
        assert wide["suggested_shards"] == 2

    def test_text_output_states_nothing_to_shard(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        _seed(db_path)
        result = CliRunner().invoke(
            main,
            ["--db", db_path, "--namespace", "test", "entity", "capacity-report", "--threshold", "400"],
        )
        assert result.exit_code == 0, result.output
        assert "capacity buckets:" in result.output
        assert "nothing to shard" in result.output
        assert "wide-topic" not in result.output

    def test_text_output_lists_wide_entity(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        _seed(db_path)
        result = CliRunner().invoke(
            main,
            ["--db", db_path, "--namespace", "test", "entity", "capacity-report", "--threshold", "200"],
        )
        assert result.exit_code == 0, result.output
        assert "wide entities (>200): 1 entities, 250 atoms" in result.output
        assert "wide-topic" in result.output
        assert "20.0%" in result.output
