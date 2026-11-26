# PROJECT_MAP.md

Current modules, call chains, and data contracts. Design reasons are in [DECISIONS](DECISIONS.md),
limitations in [KNOWN_RISKS](KNOWN_RISKS.md), and scoped validation in [TEST_MATRIX](TEST_MATRIX.md).

## Entry points and layers

The distribution/CLI is `octop-memory`, Python package `octop_memory`, plugin ID `octopmemory`,
and bridge `octopmemory-bridge`. Configuration uses `OCTOP_MEMORY_*`/`OCTOPMEMORY_*` variables.
Public Python imports are `from octop_memory import Memory, MemoryService`.

```text
Python host → MemoryService ─────────┐
Hermes → in-process Bridge ──────────┤
OpenClaw → stdio Bridge ─────────────┴→ MemoryRuntime → pipeline → Memory
CLI → application / operations ──────────────────────────────────┘
                                                     ├→ SQLite / PostgreSQL
                                                     └→ optional VectorIndex
```

`MemoryService.recall()` uses the recall pipeline directly; some CLI operations call Memory/operations directly.
Dependency constraints: pipeline/storage/domain do not import adapters, storage does not import pipeline,
and service does not import bridge. Backend-specific SQL stays in storage.
The source dashboard's direct SQLite access is an existing exception.
`tests/test_architecture_boundaries.py` validates these import boundaries.

### Module map

Paths in this table are relative to `src/octop_memory/`.

| Entry | Responsibility |
|---|---|
| `types.py`, `domain/` | Data structures, aliases, UTC, and provenance rules |
| `core.py` | `Memory` API, backend factory, optional LangGraph saver |
| `service.py`, `application/runtime.py` | Typed host API; shared capture/search/get/extract/promote orchestration |
| `application/config.py` | Runtime defaults and configuration conversion. OpenClaw expands profiles in TS; Python Runtime does not switch policy from a profile label |
| `application/host_files.py`, `path_projection.py` | Host Markdown index and virtual paths |
| `application/checkpoint_maintenance.py`, `storage/backends/sqlite_checkpoint*.py` | SQLite checkpoint content reuse, offline/online maintenance, reader compatibility |
| `server/admin.py`, `server/control.py`, `server/registry.py` | Standalone space and token management. A token identifies one client installation in one space. Its ID owns request counts, capture attribution and revocation. Admin moves require permission for both the original and destination spaces. The CLI and admin API resolve storage through `server/config.py`. Registration confirms existing bindings and rejects a different target for the same ID within the control-store transaction. Opening rechecks the effective mappings, including configured spaces. SQLite reserves a normalized prefix per physical file and PostgreSQL reserves a normalized namespace across the control store. |
| `server/mcp.py` | Standalone HTTP tools default to atom-only search through `MemoryRuntime`, expose explicit raw-event search through `Memory.search_raw`, and map paginated reads to the MCP response contract. |
| `storage/backends/__init__.py`, `sqlite.py`, `postgres.py`, `postgres_pool.py` | `MemoryBackend` contract, SQL, FTS, transactions, namespaces, pools |
| `storage/backends/postgres_legacy.py` | Pre-1.0 `harness_memory` schema: automatic copy on open, status, merge, purge marker (PG only) |
| `storage/driver_errors.py` | Exception tuples allowed to degrade at each boundary |
| `storage/vector/`, `ports/llm/` | Optional vector indexes, embeddings, `LLMClient` |
| `pipeline/` | extractor, promotion, page, episode, recall, lifecycle pipelines |
| `adapters/bridge/`, `adapters/cli/`, `adapters/dashboard/` | JSON-RPC, Click CLI, source dashboard |
| `operations/migration/`, `operations/migration/portable/` | JSONL export/import, namespace migration, backfill, portable packages |
| `operations/retention.py` | Historical runtime-error governance, semantic manual-document reconciliation, audit metadata |

Root `plugins/<host>/` contains external adapters: OpenClaw, Hermes, and the terminal client installer in `plugins/harness`. See the [integration guide](../integrations.md).

The Codex installer registers a Streamable HTTP MCP server in `config.toml`. Its header helper reads the same
local credential file as the hooks. Recall includes virtual paths for `memory_get`. Read-only setup exposes
only `memory_get` and skips transcript capture. With `OCTOP_CODEX_EXTRACTION=codex`, writable Codex captures
pass `extract=false` and request evidenced user event IDs for the active turn. Stop can continue that turn once.
The authenticated `memory_propose` tool checks the caller and matches the quote against the stored user event
before storing one atomic batch of up to three separate assertions with their evidence. Redelivery returns the original candidate IDs for that turn. Legacy single-quote clients remain supported. The captured model slug is client-reported provenance.
The review prompt asks the model to end with an empty final response for both submissions and skips.
The continued Stop sends a completion receipt through `memory_capture`. Overview reads
`last_codex_review` from eligible user events, the matching receipt and submitted quote candidates.
`last_extract_run` continues to describe server extraction.
`tests/` contains behavior tests, `evals/recall/` synthetic evaluation, and `examples/basic_usage.py` a public-API
example of temporary-database writes, trees, and prompt recall. `scripts/` contains builds, deployments,
real-host E2E, and maintenance launchers; deployments are not substitutes for unit tests.

Codex delegated review (`OCTOP_CODEX_EXTRACTION=octop`) captures user evidence through the existing MCP and queues event IDs for the configured Octop review endpoint. The host uses `memory_codex_review_evidence` to verify the credential, space, turn and eligible source before calling its selected model. `memory_propose` validates each assertion's exact quote and uses the same transactional reconciliation as native review. Native review obtains policy and matching target IDs with `memory_codex_related`. Targets come from the same credential in the same space. Matching uses scoped attributes, values and target versions. Repetition appends evidence to the existing candidate and active atom. Explicit changes supersede a confirmed atom or retire an older draft. A conflicting suggestion leaves the old value active until a reviewer decides. Each turn receipt records the resulting IDs, including merges.

### Public API responsibilities

| Scenario | Entry and result |
|---|---|
| Explicitly save a fact | `Memory.store()` / `MemoryService.save()` → `MemoryNode` leaf |
| Basic fact retrieval | `Memory.recall()` → atom-backed tree leaves |
| Search archived chat messages | `Memory.search()` → conversation-message results |
| Prompt context | `MemoryService.recall()` / `recall_for_prompt()` → `RecallResult.snippets`, `rendered` |
| Host search/read tools | `MemoryService.search()` / `get()` → hits / path excerpt; Bridge methods are `memory_search` / `memory_get` |
| Capture/extract a session | `MemoryService.capture_turn()` / `extract()`; without an LLM, capture/FTS work and extraction returns `failure_reason` |
| Manual-document review | `reconcile_manual_documents()` explicitly reconciles; `review_new_memory()` is called by a host background worker |

Service configuration feeds Runtime tool search, capture, privacy, and extraction.
`MemoryService.recall()` directly calls prompt recall without forwarding all Runtime recall configuration.
Automatic injection is atom-only; Runtime raw/host-file configuration does not widen it.
Tool search retains its multi-source policies and file sources.
`recall.min_token_coverage` (default 0.0, off) is the one relevance knob both paths share: Runtime
`memory_search` reads it from Service configuration, `MemoryService.recall()` takes it as a parameter, and
`recall_for_prompt()` drops any candidate whose matched-query-token share falls below it — so a query can
return fewer than `limit` snippets, including none. The standalone MCP registry forwards this setting,
and the thread cache key includes the effective floor.

## Repository integration map

The repositories are [octop-memory](https://github.com/TencentCloud/octop-memory),
[octop-harness](https://github.com/TencentCloud/octop-harness), and [Octop](https://github.com/TencentCloud/Octop).
Checkout directory names are not an interface; paths below belong to their named repository,
without requiring fixed adjacent clone locations. This library offers runtime APIs, CLI, and bridge;
Harness is the agent runtime library and Octop the application host.
They release independently: changing a local library does not update installed applications automatically.

### Chat and management paths

```text
Chat execution:
Octop AgentManager._build_harness_config()
  → HarnessAgentConfig → HarnessAgent / octop_harness.memory.MemoryRuntime
  → MemoryMiddleware / memory tools → MemoryService
  → octop_memory MemoryRuntime or prompt pipeline → Memory → backend

Memory management:
Octop dashboard/src/pages/Agent/Memory/ → dashboard/src/api/modules/memoryDashboard.ts
  → api/routers/memory.py, agent_files.py → api/common/memory_client.py / AgentManager
  → Bridge / MemoryService → library application/dashboard_data.py, Runtime, core/pipeline
```

`octop_memory.application.runtime.MemoryRuntime` orchestrates host-neutral memory behavior.
`octop_harness.memory.runtime.MemoryRuntime` constructs/reuses library instances, injects models/document providers,
binds tools/middleware, and manages resources. The classes are not interchangeable.
Octop management APIs can open memory or temporarily assemble Harness Runtime while the agent is stopped;
validating only a live chatting agent misses that path.

### Code ownership by responsibility

Memory/Harness Python paths in this table are relative to `src/octop_memory/` and `src/octop_harness/`.
Octop Python paths are relative to `src/octop/`; `dashboard/` starts at the Octop repository root.

| Responsibility | Repository | Entry points |
|---|---|---|
| Generic capture filtering, extraction orchestration/progress | memory | `application/runtime.py`, `domain/retention.py`, `pipeline/extractor/` |
| Facts, conflict candidates, ranking, pages, event semantics | memory | `core.py`, `pipeline/promotion/`, `pipeline/recall/`, `pipeline/page/`, `pipeline/episode/`, `operations/retention.py` |
| Schema, transactions, FTS, pools, vector indexes, checkpoint encoding/migration | memory | `storage/`, `application/checkpoint_maintenance.py`, `operations/migration/` |
| Generic LLM protocol and standalone bridge model client | memory | `ports/llm/`; not provider/credential selection for Octop agents |
| Management protocol/results and virtual paths | memory | `application/dashboard_data.py`, `application/path_projection.py`, `adapters/bridge/handlers.py`; Octop owns HTTP/frontend consumption |
| Agent switches, field serialization, Runtime assembly/reuse | Harness | `config/__init__.py`, `memory/runtime.py`, `memory/store.py` |
| Turn capture, recall injection/replay, extraction timers, background maintenance | Harness | `middleware/memory.py`, `middleware/memory_recall.py`; graph/session-end/checkpointer assembly in `agent.py` |
| Model-visible memory_search/get/save tools and result adaptation | Harness | `builtin/tools/memory_tools.py`; delegates to Service, not another memory algorithm |
| Octop auxiliary models, provider calls, retries, timeouts | Harness | `memory/llm_client.py`; generic protocol and standalone bridge client belong to memory |
| USER.md/MEMORY.md session snapshots, runtime instructions, agent write protection | Harness | `agent.py`, `memory/policy.py`, `middleware/filesystem_guard.py`, `backends/workspace.py` |
| Product settings, capability detection, namespace/DB paths, Harness configuration mapping | Octop | `_memory_extract_settings` / `_build_harness_config` in `infra/agents/manager.py`, plus `infra/agents/memory/backend.py` |
| HTTP, owner permissions, manual editing, supplying current files for adoption | Octop | `api/routers/memory.py`, `api/routers/agent_files.py`, `api/common/memory_client.py`, `infra/agents/manager.py` |
| Memory/settings/conflict/profile/storage/migration interfaces | Octop | `dashboard/src/pages/Agent/Memory/`, `dashboard/src/api/modules/memoryDashboard.ts`, `dashboard/src/api/modules/memoryPortable.ts` |
| Agent stop/restore, backup/restore, online pause, thread/agent deletion, storage statistics | Octop | `infra/agents/memory/`, `infra/agents/manager.py`, `api/routers/memory_storage.py`, `api/routers/memory_portable.py` |
| Episode-based reminders, proactive-care and cron product scheduling | Octop | `infra/proactive/`, `infra/cron/`; distinct from generic prompt-recall ranking |

### Common coordination cases

- **Inaccurate search:** inspect memory sources/filtering/ranking first. Correct results missing from the next prompt indicate
  Harness injection/snapshot replay; correct API results rendered incorrectly indicate Octop. Do not treat all three as ranking fixes.
- **Extraction models/budgets:** memory defines algorithm protocols/budgets, Harness chooses/calls models and forwards configuration,
  and Octop accepts/persists/maps user settings. Trace a new setting through every affected layer; a successful UI save does not prove execution uses it.
- **Manual files conflicting with facts:** memory handles quotations, soft deactivation, candidates, and audit;
  Harness handles the provider, nonblocking scheduler, snapshots, and agent file policy;
  Octop handles owner-authorized file saving, explicit reconciliation, and candidate review.
  File editing, semantic governance, and the active session snapshot are distinct stages.
- **Database/checkpoint formats:** algorithms/migration belong to memory; saver selection/reuse to Harness;
  pause, restore, and user backup flows to Octop. Also inspect Octop's `infra/history/` reads/restoration.

Octop's existing `infra/agents/memory/sqlite_storage.py`, `postgres_storage.py`, and `thread_cleanup.py`
access memory tables directly. Audit those consumers when schema changes, even though algorithms belong to memory.
Prefer library APIs for new generic SQL/data semantics; keep product ownership, runtime locks, and stop coordination in the host.
This repository's `adapters/dashboard/` is a source diagnostic tool; edit Octop user memory pages in Octop's `dashboard/`.

## Data and writes

| Data | Purpose |
|---|---|
| L0 `RawEvent` | Original evidence, filtered by capture/privacy policy |
| L1 `Candidate` | Extracted/manually constructed candidate, evidence, review state |
| L2 `AtomCard` | Canonical fact, provenance, confidence, supersession |
| L2.5 `Episode` / digest | Event memory and periodic summaries |
| L3 `Entity` / `EntityPage` | Entity anchor, consolidated page, headline |
| L4 journal | Business decisions and selected lifecycle records, not every SQL mutation |

```text
capture → RawEvent → extraction → Candidate → promotion → AtomCard → dirty page → regeneration
              └──event extraction──→ Episode                 └────atom_id────→ tree leaf
```

Candidates may promote, merge, drop, or enter conflict/needs_review automatically; not every candidate needs a human.
`Memory.store()` needs no LLM and creates manual RawEvent/Candidate/AtomCard plus a leaf reference.
`Memory.store(kind=...)` and `MemoryService.save(kind=...)` preserve the caller's Fact/Preference/Decision/Task
as Candidate.candidate_type; omission remains Fact. The manual path does not infer a kind or call an LLM.
Topic/entity type remains independent. Non-Fact kinds on directory nodes and ConflictCandidate manual promotion
are rejected before writes; conflict candidates still require the existing review path. Evidence, transaction,
user provenance, and asynchronous review scheduling are preserved. Dashboard kind/filter/statistics already
read the linked candidate. This additive API is unreleased; Harness must load a supporting memory version
before sending typed saves. Existing stored facts are unchanged; Task storage is not reminder scheduling.
`root → branch → leaf` is an organization tree; leaf `atom_id` projects the canonical assertion without a second fact copy.
Manual correction creates a successor, supersedes an active old atom only, and journals `user_edit`, without fabricating conversation evidence.
Absolute event times use aware UTC.

### Extraction and durable progress

Hosts can check `MemoryService.supports_extraction_rules` before forwarding `extraction.keep_rules` and `extraction.skip_rules`. Candidate v2.6 and Episode v2 apply these preferences to new extraction calls. Empty rules preserve the original prompt text.

`CandidateExtractor` in `pipeline/extractor/__init__.py` has no persistence side effects.
Defaults split at **100 events** and **20,000 estimated characters**: body length plus 200 characters per event,
not a strict serialized-prompt character/token limit. A single oversized event gets its own batch and is not truncated/dropped.
Constructor arguments can override the limits; they are not currently host JSON settings.
Merge candidates, warnings, cap_warning, and llm_calls across batches; the first failed batch stops the pass with `failure_reason`.

Runtime reads at most 10,000 session raw events per call, then excludes extracted IDs.
Backend meta persists `extracted_ids/v1/<session>`; the in-memory LRU holds at most 16 sessions.
Eviction or Runtime reconstruction reloads meta, so progress is not process-local only.
After all model batches succeed, candidates and cursor commit in one backend transaction, then update the in-memory set.
Candidate/strict-cursor write failures roll back together. Model failures do not advance the cursor either;
a later retry reruns this pass's input rather than persisting successful batches individually.

These cursor/transaction guarantees belong to Runtime/Service/Bridge `extract`.
Low-level `extract_session(..., persist=True)` stores returned candidates without an incremental cursor
or rejecting partial candidates carrying `failure_reason`; its failure semantics differ.

Promotion, page regeneration, and Episode extraction follow the candidate/cursor commit, outside that transaction.
The cursor certifies candidate extraction, not every downstream stage. Pending candidates can resume through promote.
Episodes use the same raw input and an independent prompt; their failures are nonfatal enrichment failures.
`extract_run` overwrites `last_extract_run` meta only; it does not append journal rows, and the old heartbeat setting no longer controls writes.

### Manual document governance

MemoryService/Bridge → MemoryRuntime → operations.retention shares one implementation.
Hosts supply current USER.md/MEMORY.md and an optional freshness callback; hosts still own file I/O and session-snapshot loading.
`MemoryService.save()` stores and notifies the nonblocking callback registered with `set_manual_review_scheduler()`;
it does not reconcile synchronously. A host worker/maintenance tick calls synchronous `review_new_memory(max_atoms=20)`
and reads current files through the provider. Missing provider/empty files return `None`. Run model review outside the chat turn.
Explicit `reconcile_manual_documents()` defaults to 100 atoms; total document length is capped at 100,000 characters.

Only high-confidence mutually exclusive facts with explicit subject/scope and valid quotations can atomically
soft-deactivate old atoms and create replacement Candidates/journal entries. Adoption follows review.
Uncertainty/model failure does not force deactivation; partial work can continue.
`manual_documents/v1` stores versions, compared assertion hashes, unresolved work, and audit snapshots;
`manual_documents/recall/v1` stores a small suppression manifest. Full files are not bulk-imported into atoms/FTS.

The audit `uncertain` dictionary tracks inconclusive active assertion hashes; compared is not verified.
Without that dictionary in legacy records, checked progress is untrusted: status reads calculate pending work without mutation;
next reconciliation performs a bounded re-audit and writes the resumable new format.
An explicit empty dictionary is valid resolved state and does not restart review.
Recall deduplicates against the actual session's `manual_context` and suppresses conflicting old-atom raw fallback and stale pages awaiting regeneration.
Adoption checks the last reconciliation version; hosts should supply current `manual_documents` to catch changes not yet reconciled.

### Runtime error isolation

Online capture, Candidate/Episode extraction, promotion, and raw prompt recall exclude errors by structured provenance only,
without intent/troubleshooting-value keyword lists. Agents choose when to call `memory_save`.
Background capture retains length and other policy filters; explicit saving does not inherit the short-message length limit.
Custom hosts synthesizing error replies mark `payload.octop_runtime_error=true`.
Harness owns `on_failure="error"`, checkpoint task errors, and team-turn exclusion;
this is not a promise that memory recognizes every error message by its wording.

`audit_runtime_errors()` previews by default. Apply mode deactivates automatic atoms/rejects unresolved candidates only when
all evidence is marked errors; provenance protects manual saves/approval/edits.
Legacy fixed error text only creates review hints. Missing/mixed provenance returns `review_atom_ids`/`review_candidate_ids`
without changing those records or raw evidence.

### Page regeneration

Dirty state triggers generation; not every write refreshes a page immediately. Regeneration preserves `## My Notes`.
`summary_version` advances on body generation/editing. Storage-managed `mutation_version` advances on body upsert,
dirty invalidation, manual editing, and successful generation. Regeneration ensures a dirty stub, then reads revision/atoms;
model calls hold no database transaction.

Success/failure writebacks compare the snapshot revision in the backend's conditional UPDATE.
A stale result returns `stale_snapshot` or its original failure reason and logs a failure journal,
without changing newer content, dirty state, or failure counts. A current-revision failure updates retry state only,
not mutation_version. Failed conditional writes never fall back to unconditional upsert.
Fact changes use the existing dirty chain; lower-level bypasses receive no additional governance.
SQLite/PG schema 2 adds default-zero revision columns automatically. SQLite checks/adds under `BEGIN IMMEDIATE`;
PG checks under the initialization advisory lock and ALTERs only if missing, avoiding no-op DDL exclusive locks.
Ordinary page apply retains legacy backend signatures; explicit version conditions require a custom backend's conditional parameters and atomic checks.

## Recall and reads

`pipeline/recall/names.py` resolves the longest known name or alias at each mention using one batched lookup. An ambiguous mention suppresses automatic atom injection. With a unique anchor, `multi_source._gather_atoms` searches both the owner's atoms and the global topic index. A global hit owned elsewhere must explicitly mention the requested entity in its assertion or quotation. Reciprocal rank fusion combines both lists before existing reranking and budgeting. A topic without an anchor searches globally. Date restrictions apply before the backend candidate limit.

`pipeline/recall/fields.py` identifies supported combinations of subject and requested value from the residual query. It expands the existing lexical searches and requires a matching value in the assertion. Scoped cross-owner field hits require a reference to the target in the evidence for that value. Extractor v2.5 accepts optional `search_phrases` in the candidate payload. Promotion copies bounded phrases into the atom index. Replacing an assertion discards its generated phrases.

`pipeline/lifecycle/dreaming.py` audits a cursor-bounded page of active atoms. The idle worker, background `review_new_memory`, and CLI `entity dream` reuse it. Journal entries under `entity_review` identify the source atom, quote, owner and suggested matches. Inspect them with `journal list --action entity_review`. Name-only evidence cannot change ownership. Candidate approval accepts an explicit entity ID for reviewed ambiguities.

Main pipeline: cache → parse → route → atom-only source restriction → gather → raw policy → rerank → diversify → suppress → budget → render.
`recall_for_prompt(atom_only=True)` is the automatic-injection default, gathering atom and optional vector atom hits only.
No atom match returns empty; raw, page_headline, and episode cannot fill the prompt. Service, Hermes prefetch, and CLI use this default.
Runtime `memory_search` explicitly passes `atom_only=False` for ordinary tools and preserves configurable raw fallback and host Markdown.
OpenClaw hybrid prefetch sends `atomOnly: true`; Runtime then skips non-atom sources and host files. The plugin also filters
host-prefetched/returned results and prompt rendering to canonical atom paths. Explicit tools keep existing sources.
Cache scope includes atom-only mode, raw policy, current session, and manual-document snapshots to prevent cross-mode reuse.
With multi-source retrieval, raw fallback excludes raw when durable results exist rather than topping up the count;
current session/thread scope excludes that conversation's raw.
Both recall pipelines use the raw text projection for candidates and snippets. SQLite FTS5 and PostgreSQL GIN index
that projection before matching, ranking, and limiting. Image-only events contribute no search text, and SQLite excludes
them from BM25 statistics. Fresh stores install empty current indexes. `db upgrade-index` calls `Memory.upgrade_search_index()` for historical
rows. Explicit tool search checks SQLite version markers or the versioned PostgreSQL index's validity, skips
pending raw search and reports an upgrade hint alongside other hits. Low-level raw search raises
`SearchIndexUpgradeRequired` until maintenance commits. `Memory.search_raw()` returns
original records for matching captions. `memory_get` reads the original evidence through a virtual path.

## Storage and connections

SQLite isolates namespaces with table prefixes; PostgreSQL uses shared `octop_memory` schema and namespace-first keys.
Chroma/Qdrant are optional indexes, not replacements for relational data/transactions; real vector-backend validation is still missing.
LangGraph checkpoints optionally persist execution state; `checkpoint_ns` differs from the memory namespace.
SQLite and PostgreSQL retention select checkpoints by count or age, then keep the ancestry and writes needed by each survivor.
The saver serializes planning and deletion in a single transaction per thread. Only a survivor that can restore
without its deleted parent loses that link. IDs, payloads and pending writes stay intact.
Offline migration honors its caller's explicit `protected_ids` before evaluating replay dependencies.
Harness uses scheduled SQLite cleanup with `skip_pending=True`, leaving checkpoints with pending head work intact.
For root-only threads Harness also supplies its messages reducer. A retained boundary with only a message
dependency can store a full DeltaChannel snapshot in the pruning transaction. The boundary keeps its ID,
metadata and writes. The planner keeps dependencies for other channels and unrecognized checkpoint formats.
PostgreSQL stores the message seed in the native versioned blob table and holds a transaction-level writer
reservation while applying a thread's plan. Consistent PG replay handles a reader that fetched the retained
boundary before it was detached. PostgreSQL releases blob versions only when every surviving checkpoint
in that namespace has stopped referencing them. Retained branches that can reuse an integer message
version keep their replay ancestry.
The PostgreSQL dispatcher requires an explicit caller-verified thread ID and offline confirmation for writes.
Scheduled maintenance has neither an ownership source nor a cross-host pause and skips PG pruning.
Opt-in subgraph removal preserves threads with stable or unrecognized namespaces and checks the reconstructed
root reply when the caller supplies a messages DeltaChannel.

Each SQLite backend has one connection and a reentrant lock across complete SQL/results, transactions, maintenance, and close.
Failed standalone writes roll back; close waits for operations and never reopens. Different backends have independent locks;
checkpoints use a separate connection and transaction. See ADR-039.

PG shares business (8) and checkpoint/maintenance (4) pools by process, normalized DSN, and driver options, not namespace.
Ordinary cursors lease short transactions; explicit transactions hold one connection and backend lock, without reconnecting later writes
in a broken transaction. Pools use `min_size=0`, `max_idle=600`, liveness checks, at most 64 waiters, and a 30-second timeout.
Lease close is idempotent; only the last release closes the pool. Checkpoint DDL initializes once per pool under serialization;
failure releases the lease and permits retry. Maintenance shares the autocommit pool. See ADR-040.

## Host files and migration

`HostFilesIndex` is a separate SQLite file index even with PG memory.
When enabled, it scans MEMORY.md, USER.md, DREAMS.md, date-named `memory/*.md`, and default `topics/*.md`/`projects/*.md`
or an explicit allowlist, not the whole workspace. Only safe relative Markdown globs are accepted;
anchors, hidden paths, and `..` are rejected. Reads use indexed snapshots rather than rereading live files.
File indexing and semantic document governance are separate; a hit does not prove reconciliation occurred.

Portable agent discovery defaults to `~/.octop/agents/*/memory.sqlite` and `~/.octop-harness/*/memory.sqlite`.
Use `list_sources(extra_paths=...)` elsewhere; no data moves automatically.
JSONL export/import and `.hmpkg` handle cross-database/host migration. Checkpoint format maintenance supports SQLite only;
see [CONTRIBUTING](../../CONTRIBUTING.md#checkpoint-maintenance).
For old-name configuration/path/schema migration, see the
[integration guide](../integrations.md#renaming-and-migrating-existing-installations).
