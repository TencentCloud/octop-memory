# TEST_MATRIX.md

Select focused validation by change scope; the commit gate is `make all`.
Record actual code-task results in [HANDOFF](HANDOFF.md), not dated test-count baselines.

## Basic checks

Run from the repository root with Python 3.12+. Install the dev group through `make install` first.

```bash
# Documentation: layout contracts, public text, local links, import boundaries.
uv run pytest tests/test_agent_docs.py tests/test_architecture_boundaries.py -q
# Code work and pre-commit full gate; format rewrites files.
make all
```

Include new documents in `test_agent_docs.py`'s PUBLIC_FILES/directory contracts and, if distributed,
the `pyproject.toml` sdist allowlist. Maintainer documents use English; user READMEs/integration guides have
English/Chinese versions. Layout/link tests do not validate behavior or heading anchors; check those against source and navigation separately.

## Validation by change scope

| Change | Focused commands and assertions |
|---|---|
| Package / host naming | `uv run pytest tests/test_cli_entry.py tests/test_cli_config.py tests/test_bridge_server.py tests/test_hermes_adapter.py tests/test_hermes_install.py -q`; `uv build`; in `plugins/openclaw/octopmemory/`, run `npm test`, `npm run typecheck`, `npm run build` |
| Documentation / import boundaries | `uv run pytest tests/test_agent_docs.py tests/test_architecture_boundaries.py -q` |
| Host-file globs / Windows paths | `uv run pytest tests/test_bridge_host_files.py tests/test_portable_sources.py -q`; PureWindowsPath cases reject rooted/drive-relative/UNC paths on any platform; portable fixtures isolate home expansion through Path.parts |
| Memory / Runtime | `uv run pytest tests/test_core.py tests/test_service.py -q` |
| Explicit-save classification | `pytest tests/test_backend_parity.py -k ManualMemoryKind -q -rs` with dedicated PG: all four kinds, legacy Fact defaults, independent entity/topic, source evidence, scheduler notification, dashboard kind/filter/stats, rejected input without partial writes, rollback and unchanged historical Fact. In the changed Harness source run `tests/test_memory_tools.py`, `tests/test_memory_service_integration.py`, and `tests/test_memory_recall_boundary.py` with the changed memory source; assert enum schema, English/Chinese policy, zero classifier calls and explicit upgrade failure for old memory libraries. |
| Recall | `uv run pytest tests/test_recall_pipeline.py tests/test_recall_multi_source.py tests/test_recall_risk_fixes.py -q` |
| Atom-only automatic injection | `uv run pytest tests/test_backend_parity.py -k AtomOnlyRecall -q -rs` with dedicated real PG; verify raw-only/page-only stores inject nothing, atoms remain available, explicit search still returns evidence, bridge atomOnly mode and cache separation. Run `tests/test_hermes_adapter.py`, OpenClaw `npm test`, `npm run typecheck`, and `npm run build` for prefetch/prompt consumers |
| Extraction / batching / parsing | `uv run pytest tests/test_extractor.py tests/test_extract_session.py tests/test_extractor_parser.py tests/test_extractor_prompts.py tests/test_bridge_extract.py -q`; verify default budgets, preservation of oversized single events, and stopping after a failed batch |
| Durable cursor / atomic commit / retry | `uv run pytest tests/test_extract_cursor_persistence.py tests/test_extract_cursor_failure_retry.py -q -rs`; the latter shares SQLite/real-PG assertions for first/second candidate write failure, cursor failure, rollback, same-process/reopen retry, and no duplicates after normal commit |
| Promotion | `uv run pytest tests/test_promotion.py tests/test_promotion_fallback.py tests/test_promotion_llm_hook.py tests/test_promotion_tree_alignment.py -q` |
| Hermes configuration / provider contract | `uv run pytest tests/test_hermes_adapter.py tests/test_hermes_contract.py tests/test_hermes_install.py -q`; forwarding/capture/prefetch/shutdown tests do not establish automatic-extraction wiring; validate the real host separately |
| Runtime-error isolation / historical governance | `uv run pytest tests/test_runtime_error_retention.py tests/test_bridge_server.py tests/test_backend_parity.py -q -rs`; real PG covers structured-only filtering, unknown-source review lists, arbitrary explicit-save wording, absence of intent exceptions, raw fallback, promotion, manual protection, idempotency, rollback |
| Manual-document governance | `uv run pytest tests/test_manual_documents.py tests/test_backend_parity.py -q -rs`; dedicated-PG `-k ManualDocument` shares SQLite assertions for conflict/replacement candidates, original evidence, raw fallback, snapshots, idempotency, rollback. Unit cases cover invalid/inconclusive evidence, stale adoption, cache invalidation, continuation, and save/extract/promote not blocking on review |
| Legacy audit metadata upgrade | `uv run pytest tests/test_backend_parity.py -k ManualDocumentAuditUpgrade -q -rs`; real PG/SQLite cover read-only pending status for old uncertain/complete records lacking uncertain, bounded re-audit, no repeat after Service reconstruction, confirmed empty unresolved state, and no remaining active atoms |
| Harness background-review contract | Install the real octop-harness revision pinned by CI, then `uv run --no-sync pytest tests/test_backend_parity.py -k HarnessManualReview -q -rs`; shared SQLite/PG cases cover save callbacks, maintenance ticks, soft-deactivated conflicts/evidence, blocked models not blocking save, 20-atom batches, continuation. Use real Service/Middleware/threads; mock model responses only |
| Host failure provenance | In octop-harness, run `pytest tests/test_model_retry_middleware.py tests/test_memory_middleware.py tests/test_teams/test_inbox.py`: real graph checkpoint errors/recovery, no error raw writes, exclusion of automatic team notifications. WebUI/IM/team error presentation is tested in Octop |
| Writes / backend | `uv run pytest tests/test_memory_atom_write.py tests/test_backend_parity.py -q -rs` |
| EntityPage concurrency / revision migration | `uv run pytest tests/test_core.py tests/test_sqlite.py tests/test_page_regen.py tests/test_entity_page_schema.py tests/test_page_user_notes.py tests/test_page_trigger.py tests/test_backend_parity.py tests/test_postgres.py -q -rs`; set dedicated `TEST_POSTGRES_DSN`. `-k PageRegenerationConcurrency` covers user edits, atom dirtying, concurrent generation/upsert, success/failure orders, equal timestamps, two-connection commits, legacy columns. `-k 'LegacyPageBackend or ConcurrentPageRevisionMigration or PageRevisionBootstrap'` covers old signatures, guarded-call non-degradation, four-connection migration, reopening a migrated PG schema with an active read transaction |
| Connections / transactions / file descriptors | `uv run pytest tests/test_sqlite.py tests/test_fd_pressure.py tests/test_postgres_pool.py tests/test_backend_parity.py -q -rs`; use dedicated `TEST_POSTGRES_DSN` and confirm PG cases ran |
| Vector interface / optional index | `uv run pytest tests/test_vector_chroma.py -q`; mock index and tmp_path database, never the default user directory |
| Checkpoints | `uv run pytest tests/test_checkpoint_compaction.py tests/test_checkpoint_gc.py tests/test_checkpoint_gc_safety.py tests/test_idle_maintenance.py -q` covers survivor restoration, message boundary snapshots, rollback, caller-protected offline snapshots and unversioned delta writes. Harness `tests/test_checkpoint_durability.py` exercises its scheduled opt-in with both packages loaded from the working copies. |
| PostgreSQL checkpoint retention | With a dedicated `TEST_POSTGRES_DSN`, run `uv run pytest tests/test_postgres_checkpoint_retention.py -q` and Harness `tests/test_checkpoint_durability.py`. Covers native message versions, concurrent cut replay, lock deferral, count/age windows, subgraph guards, and scheduled backups. |
| Migration | `uv run pytest tests/test_migration_export_import.py tests/test_portable_e2e.py tests/test_portable_sources.py -q` |
| Public API example | `uv run python examples/basic_usage.py` |
| Recall evaluation | `uv run python -m evals.recall.run` |
| Example / evaluation static checks | `uv run ruff check examples evals` and `uv run ruff format --check examples evals` |

## PostgreSQL and real host environments

SQLite/PostgreSQL are production backends. Public I/O, persistence orchestration, contracts, schema, transactions,
FTS, and lifecycle changes maintain both implementations and run shared assertions against real PostgreSQL:

```bash
# Dedicated test database; credentials below are placeholders.
TEST_POSTGRES_DSN='postgresql://user:pass@localhost/octop_memory_test' \
  uv run pytest tests/test_backend_parity.py tests/test_postgres.py \
    tests/test_memory_atom_write.py tests/test_checkpointer.py \
    tests/test_extract_cursor_failure_retry.py -q -rs
```

PG skips are not acceptance. CI/release starts dedicated PostgreSQL 16. An explicitly configured but unavailable
`TEST_POSTGRES_DSN` fails. Without the variable, local tests probe quickly and skip only when unavailable,
avoiding repeated 30-second pool waits across the suite.

After base tests, CI/release installs a pinned octop-harness, checks its real middleware import, and runs integration contracts.
The host is not a production dependency of memory. Missing local host cases may skip but must not be reported as passed.
See `Test real Harness background review` in the [CI workflow](../../.github/workflows/ci.yml) for installation and revision.
After installation, use `--no-sync` to retain host test dependencies and confirm cases did not skip.

`test_postgres_pool.py` verifies at most 12 total business/checkpoint connections for 69 mixed-use instances,
queue saturation/timeouts, idle zero/reconnect, server-closed idle connections, initialization retry,
shared maintenance capacity, and closing one instance without affecting others.
Idle tests shorten reclamation only in the test process; production keeps `max_idle=600`.
Closure tests assert closed leases, zero references, and a closed real pool before bounded polling for remote
`pg_stat_activity` to reach zero; closing a client socket does not imply synchronous server-process exit.
Backend-specific capabilities must explicitly reject the other backend and document limits.
Use tmp_path, never real user memory; report optional-dependency and host-E2E skips separately.
`test_fd_pressure.py` sets soft NOFILE=1024 in a separate POSIX process, exercising 69 temporary Memory instances
with 8 persistent workers and checking socket creation and return to baseline after close.
It does not alter pytest's process limit. Windows/insufficient hard limits skip; report the Linux `/proc` regression separately.
If uv cache is unavailable but the virtual environment is ready, use `make all RUN='.venv/bin/python -m'`.

## Validation across repositories

Run these representative consumer checks from their own repository roots, selected by actual impact;
not every task needs all repositories. Read each repository's AGENTS and prepare its development environment first.
Code commits also run that repository's full gate. Documentation-only maintenance verifies entry points and documents,
without operating running agents or real user data.

| Change | Repository and representative commands |
|---|---|
| Service, management protocol, manual-document semantics | memory: `uv run pytest tests/test_service.py tests/test_bridge_extract.py tests/test_bridge_dashboard.py tests/test_manual_documents.py -q -rs`; persistence changes also use the dual-backend gate |
| Turn capture, tool binding, scheduling, real Service wiring | Harness: `uv run pytest tests/test_memory_service_integration.py tests/test_memory_middleware.py tests/test_memory_tools.py tests/test_memory_integration.py -q` |
| Memory instructions, file protection, session recall replay | Harness: `uv run pytest tests/test_strong_memory.py tests/test_memory_recall_replay.py tests/test_memory_tools.py tests/test_filesystem_guard.py -q` |
| Auxiliary-model calls, retries, configuration serialization | Harness: `uv run pytest tests/test_memory_core.py tests/test_memory_call_policy.py tests/test_config -q` |
| Runtime reuse, closure, maintenance pause | Harness: `uv run pytest tests/test_memory_store.py tests/test_memory_restore_pause.py -q` |
| Product configuration/API and backend selection mapping | Octop: `uv run pytest tests/unit/agents/test_agent_manager.py tests/unit/agents/test_memory_backend.py tests/integration/test_memory_api.py -q` |
| Manual-edit permissions, stopped-agent access, current files for adoption | Octop: `uv run pytest tests/unit/api/test_strong_memory.py -q` |
| Storage, backup/restore, online slimming, PG statistics | Octop: `uv run pytest tests/unit/agents/test_memory_storage.py tests/unit/agents/test_memory_slim.py tests/integration/test_memory_storage_api.py tests/integration/test_postgres_memory_storage.py -q -rs`; configure dedicated real PG according to that repository's tests |

Octop memory components have adjacent `*.test.tsx` files. In `dashboard/`, run `npm test -- src/pages/Agent/Memory`
or the affected individual file, plus `npx tsc -b` and `make build-frontend` according to that repository's rules.
Display-only work needs no new business implementation in memory/Harness.

Integration cases must load the changed versions: record each source revision/resolved dependency and check import origins.
Passing with an old installed host dependency does not validate new source. `--no-sync` preserves an already prepared environment;
it does not install the intended sources or verify versions. This repository's fixed CI host covers its listed contracts,
not all future host versions. Model responses may be mocked; do not replace entire Service/Middleware/configuration-forwarding
or acceptance-critical backend paths with stubs. Schema/persistence integration uses real dedicated PG and covers legacy upgrades,
namespace isolation, and consumer reopening/restoration.

## Validation boundaries

Portable discovery tests isolate home expansion to tmp_path, covering current agent paths and explicit extra_paths.
Dry runs use temporary destination databases and compare before/after data. Recall degradation/page-headline tests also use temporary stores,
not the default user database.

Documentation tests check local links, public text, and layout contracts, not the absence of every possible secret.
Host installation, optional dependencies, real PG, and recall quality need separate validation.
See [modifying adapters](../integrations.md#modifying-adapters) for OpenClaw/Hermes plugin checks and real-host E2E scripts.
Without an installed host/executed script, do not claim E2E acceptance. Vector cases currently use fake/mock clients, not real services.
The synthetic evaluator's historical `P@5` denominator is the number of returned top-five items, not a fixed five;
MRR uses the complete returned list. The small corpus favors entity queries; dates/IDs vary per run.
Defaults compare raw_fts and full_pipeline, and exit status reflects only the configured delta gate.
Evaluation is outside `make all`/CI and does not establish production recall quality.
Comment-only maintenance must not alter metric definitions or seed data.
