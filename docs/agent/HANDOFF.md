# HANDOFF.md

Clarification and continuation entry point for code work. Identify work by Issue/PR number or a short title, not a dated log.
Link an existing Issue/PR and retain only status, key conclusions, and next steps here; otherwise use the template below.
Compress or replace completed records, move durable knowledge to its owning document, and keep history in Git/Issues/PRs.

## Current state

The current code baseline is **1.0.3**, as declared in `pyproject.toml`. The following work is merged and no longer
awaiting review or release. Inclusion in the baseline does not mean the current environment has revalidated
PostgreSQL, real hosts, or release artifacts.

| Merged work | History | Durable knowledge and follow-up boundaries |
|---|---|---|
| Batched extraction, restart-persistent cursor, atomic candidate/cursor commit | [#25](https://github.com/TencentCloud/octop-memory/issues/25), [PR #29](https://github.com/TencentCloud/octop-memory/pull/29) | [Extraction and durable progress](PROJECT_MAP.md#extraction-and-durable-progress); rollback, retry, and restart checks in [TEST_MATRIX](TEST_MATRIX.md) |
| Background manual-document review, public entry point, conservative legacy-audit upgrade | [PR #31](https://github.com/TencentCloud/octop-memory/pull/31), [PR #32](https://github.com/TencentCloud/octop-memory/pull/32), [PR #33](https://github.com/TencentCloud/octop-memory/pull/33) | [Manual document governance](PROJECT_MAP.md#manual-document-governance), ADR-042; hosts still register a scheduler/provider and need a version exposing public background review |
| EntityPage conditional writeback and concurrent revision migration | [PR #24](https://github.com/TencentCloud/octop-memory/pull/24) | ADR-043, [page regeneration](PROJECT_MAP.md#page-regeneration); every writer and custom backend must maintain revision semantics |
| Windows host globs, Hermes model configuration, installation instructions | [PR #26](https://github.com/TencentCloud/octop-memory/pull/26), [PR #15](https://github.com/TencentCloud/octop-memory/pull/15), [PR #16](https://github.com/TencentCloud/octop-memory/pull/16) | [Host files](PROJECT_MAP.md#host-files-and-migration), [integration guide](../integrations.md); real-host installation/lifecycle need separate checks |

Earlier connection-capacity fixes, structured-error isolation, manual-document priority, and naming cleanup
are part of the current implementation. See [PROJECT_MAP](PROJECT_MAP.md), ADR-037 through ADR-042, and
[CHANGELOG](../../CHANGELOG.md). Historical test counts and private reproduction scenarios are not acceptance
baselines for new work; consult their original PR/Git records.

## Continuing work

### Checkpoint retention

The old lifecycle prune could leave historical checkpoints pointing to deleted replay dependencies.
A LangGraph regression reproduces message and custom-channel loss across two delta channels.
The SQLite planner keeps each retained checkpoint's dependencies and preserves its ID, payload and pending writes.
Lifecycle pruning plans and applies each thread under one saver transaction. Offline checkpoint maintenance uses
the same dependency filter after selecting caller-approved candidates.

Validation on two local database copies at `keep_last=20` deleted 896 checkpoints and checked 904 survivors.
DeepAgents message and file state replay matched for each survivor, and the copies had no new dangling links.
Existing broken links remained present in the input and output. `make all` with dedicated PostgreSQL 16
passed format, lint, strict mypy and 1,569 tests, with 12 skips. The fix is local and has not been released.

The aligned Harness uses its existing count/age policy, backup and vacuum settings, and passes
`skip_pending=True` to both preview and pruning. It requires memory 1.0.4. Octop retires the v2 archive by
migrating its history before starting maintenance. Offline migration still honors the caller's explicit
`protected_ids` list. Checkpoint upserts keep existing IDs in place.

A rehearsal on a SQLite copy matched all 764 surviving checkpoints. The dry run and actual pass
removed 745 checkpoints and 1,112 writes. Octop returned identical history and trajectory pages for 52
threads, preserved the next model input, and copied a 57-message prefix when forking from an old reply.

Harness can now opt in to replaying its messages DeltaChannel at a root-only retention boundary.
The SQLite planner prepares a snapshot only where the boundary's sole replay dependency is messages.
The snapshot, parent detachment and ancestor deletion commit together. Dry runs prepare the same plan.
Other hosts keep the dependency-preserving default.

Using the Harness timer entry point on the previously pruned main database copy reduced 764 checkpoints
to 283 and 1,050 writes to 353. After incremental vacuum the file fell from 6.99 MiB to 4.52 MiB.
All 283 retained graph states and Harness history cursors matched. Octop history and trajectories matched
across 31 threads and 69 history pages, and dashboard fork and continuation passed. Real Harness tests also
cover pending parent and subagent approvals. Subgraph retention is still pending because a Harness history
cursor can read a subgraph stream when the checkpoint list lacks a namespace filter.

PostgreSQL uses the same retention planner. The PG checkpoint tables lack agent ownership, and a live fork
can hold a checkpoint in memory before it writes. `prune_checkpoints` therefore requires an explicitly selected,
caller-verified PG thread, with execution, resume and fork paused across hosts before applying. Scheduled
retention skips PG. Opt-in PG subgraph removal retains any thread with persistent or unrecognized subgraph
streams and supports completed task-scoped streams whose final reply must be replayed from DeltaChannel.
Boundary snapshots use native versioned blobs and check surviving peers before writing a shared version.

Local branch `fix/subgraph-checkpoint-retention` includes develop at `50127ac` (PR #48).
With disposable PostgreSQL 16, `make all` passed 1,631 tests with 12 skips, including eight PG retention
cases. The companion Harness branch, loading this memory source, passed `make all` with 2,076 tests
and 17 skips. Its PG approval tests exercise both scheduled deferral and explicit offline pruning.

### Atom-only automatic recall

- **Status:** [PR #41](https://github.com/TencentCloud/octop-memory/pull/41) merged into develop as e43ca72; not in released v1.0.3. Publishing is outside this delivery. Historical validation below belongs to that PR.
- **Problem:** Raw-only matches can inject ordinary conversation; page summaries also bypass canonical-atom selection.
- **Scope and acceptance:** Automatic Service/pipeline/Hermes/OpenClaw prefetch uses canonical atoms only, including optional vector atom hits. No matching atoms returns empty. Explicit tools retain multi-source/raw access; no model calls, schema changes, extraction changes, or historical deletion.
- **Recall/write path change:** Source selection for automatic prompt injection changes; existing promoted facts and writes are unchanged. Cache separates source modes.
- **Key files:** `pipeline/recall/__init__.py`, `application/runtime.py`, OpenClaw prefetch/prompt builder; ADR-044 and PROJECT_MAP document source boundaries.
- **Validation:** `make all RUN='.venv/bin/python -m'` with dedicated PostgreSQL 16: format/lint/strict mypy pass, 1608 tests pass, 12 optional host cases skip. Focused Service/recall/retention/manual/Hermes/bridge/backend tests: 402 pass, 8 optional Harness cases skip. `pytest tests/test_backend_parity.py -k AtomOnlyRecall -q -rs` runs all 12 SQLite/real-PG cases without skips, including host-file exclusion. OpenClaw `npm test` passes 69 cases; `npm run typecheck` and `npm run build` pass.
- **Consumer verification:** Load current memory source (base d9170b5 plus this change) with actual Harness source 2fef395. Dedicated `AtomOnlyRecall or HarnessManualReview` cases: 20 pass, no skips, covering both backends; host Service/tools/recall-replay suites: 66 pass, no skips. The ordinary full gate's missing-Harness cases are thus covered separately. Four real-Hermes E2E cases remain unexecuted without a Hermes installation; real OpenClaw host hooks are not exercised by plugin tests.
- **Continuation:** PR #41 awaits a future release/adoption; relevance admission, independent durable-value screening, and negative-query evaluation remain deferred development plans. Keep the GitHub Wiki development-status pages aligned with changes to these issues and plans.

### Pre-1.0 PostgreSQL schema recovery

- **Status:** [PR #48](https://github.com/TencentCloud/octop-memory/pull/48) merged into develop as `50127ac`.
- **Problem:** 0.9.6 to 0.9.11 stored shared tables in PG schema `harness_memory`; 1.0.0 switched to `octop_memory` without moving rows, so upgraded PG users lost long-term memory (checkpoints on `public` are unaffected).
- **Scope and acceptance:** Empty namespaces copy automatically on open, in an explicit transaction, with a marker; non-empty ones are reported by `Memory.legacy_schema_status()` and merged only through `Memory.merge_legacy_schema()` (dry run by default, one transaction, key conflicts skipped; an exact preview with `blocked` nodes stops it; deferrable unique constraints make it `unsupported`). Every purge records a `purged` marker so old rows never return, even if the role could not see them. Markers are read before the legacy tables. `harness_memory` is never modified. Per-namespace advisory lock; the automatic copy bounds every lock wait to 5 s and retries on the next open, while a host-called merge keeps the caller's lock_timeout. Namespaces named `harness_memory`/`octop_memory` migrate like any other. SQLite reports unsupported and rejects merge.
- **⚠️ Recall/write path change:** Opening a PG namespace can add rows to all eleven namespace tables and its `{ns}:` meta keys, which then participate in recall and extraction cursors. Startup DDL now creates only missing indexes, so uncommitted writes elsewhere no longer stall other agents' startup.
- **Key files:** `storage/backends/postgres_legacy.py`, `postgres.py` (`_ensure_index`, `_migrate_pre_rename_schema`, `purge_namespace`, guard in `_migrate_legacy_schema`), `core.py`; `tests/test_postgres_legacy_schema.py`; integration guides (EN/CN).
- **Validation:** With dedicated PostgreSQL 16, `make all RUN='uv run --no-sync'` and two subsequent `make test` runs each passed 1621 tests with 12 optional skips. All 27 legacy-schema tests ran (26 temporary PG databases, 1 SQLite). A template copy of a real 0.9.x database migrated 217 rows. Coverage from a failing full run showed the atom timeout branch. Injecting 100 ms into the third SQLite atom query reproduced the same failed assertion in the original test. The updated test keeps real SQLite and policy assertions with a fixed elapsed clock and synchronous source calls, and passes with the same injected delay. Production budgets and deadline tests are unchanged. The cause of the intermittent delay and its association with this branch remain unconfirmed.
- **Continuation:** Hosts add a status/merge entry (Octop storage page and CLI).

### Explicit remember classification

- **Status:** Validated on `fix/manual-memory-kind` in memory and Harness; pending PR review against develop. Unreleased.
- **Problem / acceptance:** Explicit remember saves hardcoded Fact even for preferences, decisions and tasks. Add optional caller-selected kind, preserve all four types through the real tool/Service/store chain and dashboard filtering/statistics without an extra classifier call. Legacy omission remains Fact; no historical reclassification, schema migration, reminder scheduling, or changes to extraction/recall algorithms.
- **⚠️ Recall/write path change:** Manual L0 evidence → Candidate.candidate_type → canonical Atom; type is independent of entity/topic. Invalid/conflict kinds reject before writes. Preserve user protection, rollback and asynchronous review callbacks. See ADR-045.
- **Key files / consumers:** `core.py`, `service.py`, dual-backend tests; companion Harness `builtin/tools/memory_tools.py` and `memory/policy.py`. Octop already reads Candidate type for display; no UI/schema edits. Install/release the memory API before adopting typed saves in a released host; old libraries explicitly reject typed tool saves and retain legacy Fact saves.
- **Validation:** New regressions failed before implementation (8 SQLite write failures and 10 tool/policy failures). Focused real-source Harness tests: 83 passed; dedicated SQLite/PostgreSQL `-k ManualMemoryKind`: 22 passed, no skips. `TEST_POSTGRES_DSN=<dedicated-test-db> make all RUN='.venv/bin/python -m'`: memory 1640 passed, 4 environment/optional cases skipped, strict mypy/ruff clean. Harness `make all RUN='.venv/bin/python -m'`: 1989 passed, 28 optional/environment skips, mypy/ruff clean. Both environments load changed memory source based on e43ca72 and changed Harness source based on 33b6d02, not an old installed library. No classification or PostgreSQL cases skip; actual conversation-model type-choice quality is not independently evaluated by these deterministic tests.
- **Continuation:** Create linked English-title PRs into develop, synchronize bilingual GitHub Wiki development status and the workbench. Supporting dependency version is deferred until the library release exists; do not publish either package in this task.


## Task template

- **Task / status / links:** Needs clarification → Ready → In progress → Awaiting validation → Complete; name any blocker.
- **Problem and scenario:** Actual/expected bug behavior and minimal reproduction, or users, scenario, and desired result.
- **Scope and acceptance:** Included/excluded work, verifiable criteria, compatibility, and data effects.
- **Questions / assumptions:** Separate confirmed facts from unresolved issues; never record an unanswered question as confirmed.
- **Approach and key files:** Minimal implementation, affected call chain, and links to DECISIONS for important tradeoffs.
- **Validation:** Exact commands, actual results, failures/skips/uncovered cases, and sanitized evidence when needed.
- **Continuation:** Current progress, unresolved risks, next steps, and whether affected documents are aligned.
