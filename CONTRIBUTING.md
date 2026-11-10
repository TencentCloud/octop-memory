# Contributing

Read the [README](README.md) for an introduction and this guide for implementation and contribution workflows.
Shared coding-agent rules live in [AGENTS.md](AGENTS.md). Host installation, configuration, and integration contracts
are maintained in the [integration guide](docs/integrations.md) ([Chinese](docs/integrations_CN.md)).

## Getting started

Use Python 3.12+, SQLite with FTS5, and uv.

```bash
git clone https://github.com/TencentCloud/octop-memory.git
cd octop-memory
make install
make install-hooks
make all
```

Create `feature/*` or `fix/*` branches from `develop`. Reproduce issues with synthetic data,
then add the implementation and relevant behavior tests. `make all` runs format, lint, strict mypy, and pytest;
format rewrites files. The pre-commit hook runs the same gate.

Supporting directories have distinct roles: `examples/basic_usage.py` is the single basic example, demonstrating
public APIs, the organization tree, and prompt recall with a temporary database. `evals/recall/` provides synthetic
regression evaluation; see TEST_MATRIX for metric limitations. `scripts/` contains plugin builds, deployments,
and real-host acceptance checks. Deployment/E2E scripts need dedicated environments and are not ordinary unit tests.
Keep personal settings in ignored `.claude/`, `.codebuddy/`, and `.cursor/` directories. The tracked
`.cursor/skills/publish` and `.codebuddy/skills/publish` are shared release workflows, not personal settings.
Maintain project-wide agent rules only in AGENTS.md.

## Documentation language

`AGENTS.md`, this guide, and `docs/agent/` are maintained in English as a single engineering source of truth.
The README and integration guide have English and Chinese versions with reciprocal links.
When changing public contracts, commands, or examples, update both user-facing versions together.
Use the [documentation index](docs/README.md) to find the relevant document.

## Engineering documentation workflow

For code bugs and requirements, describe the problem, scope, and acceptance criteria before implementing,
validating, and updating project knowledge. Use an existing Issue/PR as the clarification record and retain its
link and current status in [HANDOFF](docs/agent/HANDOFF.md); without an Issue, use its short template.
Human contributors follow the same loop as coding agents; the shared rules are in [AGENTS.md](AGENTS.md).
Routine banner, layout, link, Python/dependency, and build/CI maintenance only updates the affected files
and necessary user documentation. Do not create engineering activity logs for it; if runtime behavior or a data
contract also changes, document that code impact.

| Document | Single responsibility |
|---|---|
| [HANDOFF](docs/agent/HANDOFF.md) | Clarification, progress, validation, unresolved items, and next steps for active work |
| [PROJECT_MAP](docs/agent/PROJECT_MAP.md) | Module entry points, call chains, data flows, and boundaries |
| [DECISIONS](docs/agent/DECISIONS.md) | Architecture choices, reasons, costs, and supersession relationships |
| [TEST_MATRIX](docs/agent/TEST_MATRIX.md) | Commands and environments selected by change scope |
| [KNOWN_RISKS](docs/agent/KNOWN_RISKS.md) | Current gaps, limitations, and mitigations |
| [GLOSSARY](docs/agent/GLOSSARY.md) | Core terminology |
| [README_AUDIT](docs/agent/README_AUDIT.md) | Public claims checked against implementation |

Update affected documents rather than creating a file for every task. Keep completed-task details in Issue/PR/Git history.
Start from the [project map](docs/agent/PROJECT_MAP.md), choose checks from the
[test matrix](docs/agent/TEST_MATRIX.md), and consult [known risks](docs/agent/KNOWN_RISKS.md).

<a id="checkpoint-维护"></a>

## Checkpoint maintenance

SQLite's `CompactSqliteSaver` reuses large `skills_metadata` and `memory_contents` values by default.
Immutable content lives in `hm_checkpoint_blobs` in the same database; `harness-checkpoint-v1` checkpoints store references.
Content and checkpoint writes are atomic, legacy inline values remain readable, and existing history is not migrated in bulk automatically.
Independent readers use `CheckpointSerializer.with_connection(conn)` for their own decoder/cache and consistent transaction view.

```bash
# Read-only preview by default.
octop-memory db slim /path/to/memory.sqlite
# After stopping all database users: back up, deduplicate, and VACUUM without deleting history.
octop-memory db slim /path/to/memory.sqlite --apply --offline
# Before downgrading a reader, expand the legacy representation. The backup path must not exist.
octop-memory --db /path/to/memory.sqlite db checkpoints \
  --apply --offline --backup /path/to/new-backup.sqlite --expand
```

`--offline` is an operator declaration; it does not stop processes. Backups include committed WAL and need extra disk space.
`OCTOP_MEMORY_CHECKPOINT_DEDUP=0` stops new reference writes but does not expand existing records; old readers cannot
read the new format directly. Migration commits in batches and skips converted rows on reruns.
The offline migration command requires explicit completed threads and external references before it prunes.
The separate `thread prune --dry-run` command previews the count or age window and its replay dependencies.
The window counts graph checkpoints, including intermediate steps within a turn. Retention can keep older checkpoints
and writes needed by a survivor. Pruning preserves survivor IDs and detaches a parent link only at an independent boundary.
Threads with subgraphs retain their cross-stream history unless explicit subgraph removal passes the pruning guards.
SQLite hosts can call `pin_sqlite_checkpoints` from `octop_memory.application.checkpoint_maintenance` before
publishing a reference to an existing root checkpoint. Pins survive restart and are honored by lifecycle and offline
retention. Hosts can set an expiry for temporary references. Explicit checkpoint deletion removes the corresponding pin.
Hosts reconciling existing references can pass `strict=False` to receive the missing pairs while registering all
available ones in the same transaction. By default a missing reference rolls back the whole registration.
Scheduled SQLite maintenance can use `run_checkpoint_retention` from the same module. It keeps active pins
and skips threads with pending work or subgraph dependencies. Other backends bypass scheduled checkpoint retention.
The host coordinates `slim_live_checkpoints`: pause new invocations, coordinate readers, and resume.
Long-lived readers may delay WAL reclamation. These format-maintenance operations support SQLite only and explicitly
reject PostgreSQL, which uses its original saver. `checkpoint_ns` is not the memory namespace.

## Pull requests and releases

Describe the problem, resulting behavior, validation, and remaining limits. Add user-visible changes under
Unreleased in `CHANGELOG.md`. For nontrivial code work, update the existing HANDOFF and affected map, decisions,
tests, risks, glossary, and README audit. Do not append dated activity logs.
Use English PR titles by default. When `docs/` updates issues, status, risks, or plans, synchronize the affected
[GitHub Wiki](https://github.com/TencentCloud/octop-memory/wiki) pages in the same work session, including both
language versions where present. Cite the source Issue/PR/revision and distinguish proposed, merged, and released states.

### Branching and releases

| Branch | Role |
|---|---|
| `main` | Production source of truth and GitHub default; release/hotfix merges only |
| `develop` | Daily integration; **feature PRs target `develop`** |
| `release/x.y.z` | Temporary release branch; delete after release |
| `hotfix/*` | Emergency fix from `main`; merge into `main`, then back into `develop` |

```text
feature/* ──PR──► develop ──► release/x.y.z ──PR──► main ──tag v*──► publish
hotfix/* ──PR──► main (+ tag) and ──PR──► develop
```

- Never push `develop` directly onto `main` or bulk-merge it into `main`; never push directly to `main`/`develop`.
  Use the protected PR flow.
- Merge `release/x.y.z` into `main` with a **merge commit**, not squash.
- Actions creates production `v*` tags only after the release/hotfix has merged, on the main tip.
- `sync-main-to-develop.yml` keeps `main` an ancestor of `develop` after release.

Daily work starts from `develop` on `feature/*`/`fix/*` and targets `develop`.
Release through a `release/x.y.z` or `hotfix/*` PR into `main`; keep project version, CHANGELOG, and tag consistent.
The workflows create the `v*` tag after merge, publish to PyPI/GitHub Release, then sync back into `develop`.
Without release authorization, validate builds only; do not push tags or upload artifacts.
Use the PR/Actions release flow rather than local `make publish`/`twine upload`.
Before release, verify version availability, the Unreleased archive, and gates; afterward check the tag/artifacts
and clean release branches according to repository policy.

Agent-assisted release workflows are `.cursor/skills/publish` and `.codebuddy/skills/publish` (`/publish <version>`).
They synchronize CHANGELOG/README versions and related release content.

```bash
uv build --out-dir dist/package-review
uv build --wheel --out-dir dist/package-review/direct
```

The first command rebuilds a wheel from the sdist; compare extracted wheel filenames and contents with the direct build.
Python sdist packaging uses an allowlist. Build standalone plugins separately; tests and CI remain in the full checkout.
Plugin READMEs are short distribution entry points; detailed instructions live in the English/Chinese integration guide.

Never commit `.env`, databases/WAL, backups, logs, `.hmpkg`, dependency-installation directories, or personal operations records.
Personal editor settings and scripts inspecting real databases do not belong in public source; the shared `/publish` skill does.
Ignore rules do not remove tracked files, and sanitizing a worktree does not clean Git history or existing attachments.
See [SECURITY.md](SECURITY.md) for vulnerability reporting.
