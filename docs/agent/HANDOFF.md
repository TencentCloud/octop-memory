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

### Standalone memory space isolation

- Local follow-up to [Memory PR #59](https://github.com/TencentCloud/octop-memory/pull/59), based on `c32e45a`.
- **⚠️ Recall/write path change** — The standalone server checks token scope changes in the control-store transaction and requires a registered space before MCP access. Registration and opening reject a shared SQLite table prefix in the same physical file, including file aliases and older configurations. PostgreSQL namespaces remain unique across the control store.
- Space registration resolves the configured database for both CLI and API callers. Repeating the same binding confirms it. A different target returns HTTP 409 and keeps the original registration. The paired [Octop PR](https://github.com/TencentCloud/Octop/pull/2887) still needs to stop token issuance on conflict.
- **⚠️ Recall/write path change** — Standalone MCP search defaults to durable atoms. Explicit `raw` uses the existing event search and returns readable paths. `memory_get` supports `start` and returns the excerpt as `content` for existing clients.
- **Validation** — `make all` passed against dedicated PostgreSQL 16 with 1,712 passed and 3 platform or optional-host skips. All 33 [standalone server checks](TEST_MATRIX.md#validation-by-change-scope) ran, including token rebinding on an older control database, configured CLI and API registration, same-ID retries and concurrent conflicts, file aliases, shared-file MCP isolation, per-installation revocation and paginated recall. All 14 terminal plugin tests passed. A temporary POSIX project received distinct Codex and Cursor configurations. Windows rendering was tested without running PowerShell.
- **⚠️ Recall/write path change** — Each installation receives its own space token. Usage, capture ownership and review attribution follow the token ID. Older shared or partially disabled credentials are retired on upgrade. The server ignores earlier secondary grants. Clients with Octop-only credentials need a new space token.

### Raw image search

- **Status** — Under review in [Memory PR #54](https://github.com/TencentCloud/octop-memory/pull/54) for [Octop #2989](https://github.com/TencentCloud/Octop/issues/2989).
- **⚠️ Recall/write path change** — Index and rank captions from raw events. Skip serialized image blocks and image-only placeholders before applying the result limit. Keep original records and virtual-path reads for evidence. Fresh stores initialize empty indexes. Existing stores require `db upgrade-index` during the deployment window.
  Raw recall returns an actionable pending status while capture, evidence reads, and automatic atom recall continue.
- **Validation** — `make all` with disposable PostgreSQL 16, the SQLite namespace-copy regression, and both GIN variants. On old stores created by `c66e353`, the actual Harness tool returned the upgrade hint with and without atom hits. Capture and original reads stayed available, and a live reader found old and pending-period captions after the CLI ran. The Harness tool, service and recall-boundary suites passed 101 cases against the proposed Memory source.
### Entity identity, lexical recall, and background review

⚠️ Recall/write path change on local branch `feat/entity-recall-dreaming`, based on `c66e353`.
Typed writes and promotion retain ambiguous identity for review. The prompt pipeline combines entity-scoped and global topical evidence.
A bounded background audit records legacy assignment and alias evidence. See ADR-046 and [retrieval risks](KNOWN_RISKS.md#retrieval-quality-and-validation).

The frozen 115-atom replay contains 26 answerable and six unanswerable questions. On SQLite, hit@5 rose from 15 to 23, first hits from 13 to 22, and unwanted injections fell from five to one. Among 22 project-specific questions, five previously included a fact from an unrelated project and none do now. On dedicated PostgreSQL the same replay went from five to 23 hit@5, three to 22 first hits, four to one unwanted injections, and zero to zero questions with unrelated project facts. Both new backends retrieve both relationship facts held by a person. At revision `4bb9a69` the remaining unwanted injection supplied a signatory for a telephone question. These are retrieval measurements on synthetic atoms. Production behavior and large-database latency remain unmeasured.

The full library gate passed with real PostgreSQL (1,638 passed, 12 skipped). The three Harness service, middleware and prompt replay suites passed all 96 cases against the local memory source with the dedicated PostgreSQL database. The ten-project same-field control retained 10/10 first hits before and after on SQLite.

Reproduce per-question results with `python -m evals.recall.entity_cases` and the PostgreSQL variant in [TEST_MATRIX](TEST_MATRIX.md#validation-by-change-scope).

### Paraphrases and answer-field evidence

⚠️ Recall/write path change. The baseline is the entity-aware dual retriever at `4bb9a69`. Atom candidate collection expands recognized topic/field pairs in the existing lexical queries. A precursor to this change missed an old PostgreSQL fact among 1,171 atoms because fragments of an already recognized compound crowded the top results. Keeping that subject whole recovered the fact. Contact lookups bind a labeled value to the requested role. Extractor v2.5 can index up to three additional topic phrases without an extra model call. Old atoms participate immediately through query expansion.

On the frozen 115-atom replay, both backends moved from 23/26 to 26/26 hit@5 and from 22/26 to 25/26 first hits. Unwanted injection went from 1/6 to 0/6. Neither version injected an unrelated project in the 22 targeted questions. At 1,171 atoms both new backends also achieved 26/26 first hits. The separate 13-question general SQLite suite retained precision 0.808 and MRR 0.846. The frozen corpus has legacy index keys throughout. Generated phrases were exercised separately with a deterministic extractor response.

Each version, corpus and backend received 512 timed public recall calls with warm storage and no result cache. On 115 atoms SQLite P50/P95 in milliseconds moved from 1.98/5.59 to 2.07/4.86, and PostgreSQL from 2.10/4.80 to 2.14/3.66. The four previously failing questions had median times of 1.98→2.69 ms on SQLite and 2.46→2.54 ms on PostgreSQL. On 19 atoms SQLite P95 increased from 1.95 to 4.32 ms. These are single-client local measurements. See [TEST_MATRIX](TEST_MATRIX.md#validation-by-change-scope) for replay commands.

The library gate passed with the dedicated PG instance (1,646 passed, 12 skipped). All 14 focused entity and lexical field parity cases ran, including every PG variant. The three Harness integration suites passed all 96 cases. The separate ten-project same-field SQLite control retained 10/10 first hits.

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
