# DECISIONS.md

Preserve historical ADR numbers and concise, public design rationale; omit private samples, deployment records, and dates.
Accepted identifies the current design choice, not validation in every environment. See [KNOWN_RISKS](KNOWN_RISKS.md) for gaps.
New decisions describe context, choice/reason, alternatives/cost, and review conditions. Mark supersession explicitly;
never silently overwrite an existing constraint.

## ADR-001: Memory / MemoryBackend boundary (Accepted)

Hosts need replaceable storage. Keep the public storage API in `Memory`, constrain backends with a Protocol, and lazy-load them.
This avoids upper-layer SQL and optional-dependency coupling. Cost: both backends must maintain the same contract;
review parity when extending an API.

## ADR-002: RawEvent is evidence for derived memory (Accepted)

L0 is append-only; Candidates/AtomCards retain raw references and verbatim quotations for tracing and re-extraction.
Model rewrites do not replace valuable originals. Cost: evidence storage and privacy retention require separate management;
review them when changing retention.

## ADR-003: Extractor has no persistence side effects (Accepted)

`CandidateExtractor.extract()` returns results; its caller decides persistence. LLM/parse failures return `failure_reason`.
This supports tests, inspection, and retries, but callers must distinguish failure from a successful empty result.
The extractor currently calls the model in batches and stops at the first failed batch. Only after the entire extraction
pass succeeds does Runtime atomically commit candidates and the `extracted_ids/v1/<session>` cursor, then advance memory state.
Runtime does not persist partial successes batch by batch; candidate/cursor write failures roll back together.
See [extraction and durable progress](PROJECT_MAP.md#extraction-and-durable-progress) for budgets and downstream-stage boundaries.

## ADR-004: Rule-first promotion (Accepted)

Run value → evidence → entity → duplicate → conflict checks; the first terminal outcome wins. Escalate ambiguity to an LLM.
This avoids a model call per candidate. Cost: jointly validate rules and state mapping; review the demotion-only boundary
when adding automatic promotion conditions.

## ADR-005: Append and deprecate facts (Accepted)

Create a new atom and invalidate the old one through `superseded_by`/`deprecated_at`; default queries exclude invalid facts.
Do not overwrite assertions in place, preserving relationships and auditability. Cost: historical storage and concurrent
replacement control. Manual correction is covered by ADR-034.

## ADR-006: Store EntityPage separately (Accepted)

Separate entity and page tables. Pages track dirty/version/regeneration state, retain old content on failure, and preserve `## My Notes`.
This keeps large bodies out of hot indexes and protects user notes from generation. Cost: asynchronous updates may be stale;
review that behavior when changing regeneration.

## ADR-007: Independent notes tree (Superseded by ADR-010)

Previously, tree facts and pipeline facts were separate and tree leaves were an independent recall source.
The single-fact-source design replaces this arrangement; it is not a current implementation rule.

## ADR-008: Host shells reuse the Python runtime (Accepted)

OpenClaw uses TypeScript plus a stdio JSON-RPC bridge; Hermes reuses Bridge handlers in-process.
Avoid a second memory algorithm implementation or an extra network daemon. Cost: validate interpreter, process,
and protocol lifecycles; see the [integration guide](../integrations.md).

## ADR-009: Versioned JSONL migration (Accepted)

A first-line header, record envelopes, and dependency order enable streaming import/export instead of copying backend-private SQL data.
Cost: new fields and format upgrades need explicit compatibility rules. Revisit when changing export/import.

## ADR-010: AtomCard is the canonical fact (Accepted; supersedes ADR-007)

Leaves store only `atom_id`; reads JOIN the atom assertion. Manual store, promotion, and direct atom writes share one fact chain.
Entities map to organization branches; tree is no longer a recall source. This avoids duplicate fact writes, at the cost of
leaf lifecycle depending on atoms. See current source conditions in [PROJECT_MAP](PROJECT_MAP.md);
do not reuse the historical absolute claim that recall has only atom/raw sources.

## ADR-011: Host supplies hybrid context (Accepted; real-host validation pending)

Before prompt construction, OpenClaw invokes the optional `recallPrefetcher`; the synchronous promptBuilder consumes its result.
Do not try to supply this turn's context from `agent_end`. Cost: actual host hook invocation is required;
review it when upgrading the SDK/host.

## ADR-012: Capture supports short CJK text and strips think blocks (Accepted)

Shared capture policy uses CJK-weighted length and strips `<think>` before length checks and storage.
Explicit intent keywords no longer admit short messages; agents call memory_save for deliberate saves.
This avoids duplicated host filters and re-ingestion of reasoning. Cost: boundary-length messages can be filtered;
validate multilingual samples when changing thresholds.

## ADR-021: Layered directories and shared MemoryRuntime (Accepted)

Use adapters → application → pipeline/core/storage/ports/domain. Service must not depend on bridge, nor storage on pipeline.
Do not retain shims for old internal paths, avoiding two long-lived layouts. Cost: internal import users must migrate.
Define any future stable extension API separately; this decision does not authorize breaking existing public APIs.

## ADR-022: Checkpoint pruning is separate from memory GC (Accepted; safety extended by ADR-029/036)

Separate execution-state cleanup from fact lifecycle. Do not add a journal row per deletion and grow one table while clearing another.
Cost: independent statistics and restore-dependency checks. Row deletion, history pruning, and physical reclamation are distinct.

## ADR-023: Merge extract_run heartbeats (Superseded by ADR-028)

The historical approach merged quiet journal entries by session. Current code overwrites meta;
heartbeat configuration no longer controls journal writes.

## ADR-024: Partial indexes for SQLite journal targets (Accepted)

Targets are usually null. Use `WHERE col IS NOT NULL` and idempotent schema-introspection migration, without adding columns or tables.
Query semantics stay unchanged. Cost: rebuilding old indexes takes time; revisit coverage when changing journal queries.

## ADR-025: Intent-oriented space maintenance APIs (Accepted)

`check_storage` diagnoses read-only, `nudge_vacuum` reclaims lightly, and `compact_vacuum` performs heavy maintenance,
with backend-specific implementations. Callers need not depend on engine terminology.
Cost: locking, space, and shared-table scope differ; ordinary PG VACUUM also cannot promise no lock waits.

## ADR-026: Raw is fallback by default (Accepted)

Before reranking, prompt recall applies `raw_policy=fallback`: durable results prevent raw top-ups,
and current-session/thread raw is excluded. This avoids echoing the active conversation into prompts,
at the cost of possibly fewer results. Tools can explicitly select raw policy; review filter order when changing it.

## ADR-027: Hosts schedule light maintenance while idle (Accepted)

New SQLite files use INCREMENTAL. `run_idle_maintenance()` runs prune → gc → nudge.
Heavy compaction and WAL truncation need an appropriate idle window and space checks.
The library does not promise every host schedules them automatically; validate host wiring separately.

## ADR-028: Separate runtime summaries from decision journals (Accepted; supersedes ADR-023)

`extract_run` overwrites a meta summary; journal retention depends on action, and GC returns counts rather than per-row entries.
This prevents idle activity from inflating the audit table. Cost: summaries cannot replay every empty pass;
see the risk list for the PG retention gap.

## ADR-029: Seal parent graphs before pruning (Accepted)

Keep stepwise checkpoints within a turn. Pruning defaults to `keep_last=1`: seal parent-graph messages first,
then delete ancestors/subgraphs under safety checks. In-progress, HITL, or failed-sealing cases are not ordinary completed turns.
Cost: sealing and dependency-validation work; review it when changing restore semantics.

## ADR-030: Short transactions for ordinary PostgreSQL operations (Accepted)

End ordinary read/write transactions through `_cursor()`; explicit transactions preserve atomicity across operations.
Bootstrap uses lock timeouts and an FTS catalog fast path. This avoids idle transactions blocking DDL.
Cost: genuinely long migrations need separate planning rather than globally relaxed timeouts.

## ADR-031: Manual adoption is a user decision (Accepted)

Pending candidates run automatic checks. Approval handles conflict/needs_review; duplicates merge,
and a successor replaces contradictory active atoms. Fact writes, candidate state, supersession, and user journal share one transaction.
Cost: this explicitly overrides automatic judgment and must not be treated as an ordinary retry.

## ADR-032: Recover PostgreSQL connections at cursor boundaries (Superseded by ADR-040)

Reset/reconnect and restore search_path only outside transactions; a failed explicit transaction stays failed.
Avoid silently committing partial work. Cost: callers retry the whole transaction.
The existing SQLite-only GC gap does not authorize silent backend capability divergence.

## ADR-033: Namespace-first GIN (Accepted)

In the shared PG schema, try `btree_gin` plus `GIN(namespace, tsv)`. Attempt extension installation inside a savepoint;
failure retains ordinary GIN. Queries remain correct without permission to install the extension.
Cost: weaker namespace performance isolation; use real PG validation.

## ADR-034: Manual correction appends an atom (Accepted)

The successor retains historical provenance and `user_edit` records the correction; do not fabricate a RawEvent/Candidate.
Both backends require active-only supersession and complete rollback on failure.
Cost: the original quote is provenance, not evidence proving the corrected assertion.

## ADR-035: Aware UTC for absolute Episode times (Accepted)

Normalize write/read/query boundaries to UTC; interpret legacy naive values as UTC and defensively normalize sorting.
This avoids mixed datetime types without requiring upfront migration.
Cost: unknown historical local time zones cannot be recovered; future local-time support needs a separate field and versioned migration.

## ADR-036: Reuse checkpoint content with explicit migration (Accepted)

SQLite uses same-database typed blobs and a format envelope to reuse large fields. Reads restore historical values,
not the latest workspace file. Separate new writes from historical migration; independent readers have their own decoder/cache.
Cost: old readers are incompatible, so expand before downgrade.
See [checkpoint maintenance](../../CONTRIBUTING.md#checkpoint-maintenance) for offline migration, backups, and rollback.

`db slim` previews by default; explicit apply/offline creates a backup, deduplicates, and reclaims space without implicit history pruning.
Advanced options remain under checkpoints. For online slimming, the host pauses new invocations, coordinates readers,
and resumes. Batches are transactional and do not chase an unlimited stream of new rows.
Long readers can postpone WAL reclamation. Keep backups and report partial completion;
never overwrite subsequent legitimate writes automatically with a backup.

## ADR-037: Unified Octop naming with stable disk protocols (Accepted)

The distribution/CLI is `octop-memory`, Python package `octop_memory`, and plugin ID `octopmemory`.
Rename environment variables, default paths, bridge probe fields, and the PG schema together, without old-entry aliases.
SQLite checkpoint encoding, `hm_*` tables, and `.hmpkg` are persistent protocols and remain stable to protect historical reads.
Cost: old installations explicitly migrate configuration, directories, or PG schema; maintain those steps only in the
[integration guide](../integrations.md#renaming-and-migrating-existing-installations).

## ADR-038: Degrade only on expected driver and I/O failures (Accepted)

Queries, files, and timeouts use `storage/driver_errors.py`'s `DRIVER_ERRORS`
(`sqlite3.Error`, installed `psycopg.Error`, `OSError`, `TimeoutError`, `RuntimeError`), log warnings, and degrade.
Vector indexes use `VECTOR_ERRORS`. Doctor, extraction boundaries, bridge, idle maintenance, backfill, and host polling use `REPORTABLE_ERRORS`.
Transaction rollback and checkpoint-backup deletion use `try`/`finally`; failures and cancellation roll back.
Exceptions outside the relevant tuples propagate. `AssertionError` is not a degradation exception.

## ADR-039: One SQLite connection per backend, serialized operations and transactions (Accepted)

Remove thread-local connection registries so multiple agents and permanent workers do not multiply connections.
Each backend has one `check_same_thread=False` connection and a reentrant lock covering execute/fetch/commit,
explicit transactions, and close. Failed writes outside explicit transactions roll back and re-raise the original error.
Internal vacuum, WAL truncation, and export SQL use the same protection, preventing maintenance from interleaving in normal transactions.

Choose one connection rather than a SQLite pool to reduce idle file descriptors and lifecycle branches.
PG pooling is covered by ADR-040. Cost: slow queries and long transactions block other SQL on the same backend.
LLM calls stay outside those operation locks; different backends and the separate checkpoint connection can still run concurrently.
SQLite/WAL, tables, atomicity, and recall contracts are unchanged, with no data migration.
Future same-backend SQL concurrency needs bounded leases and renewed transaction/closure validation.

## ADR-040: Bounded PostgreSQL pools shared by connection configuration (Accepted)

One business connection plus a checkpoint pool per agent still compounds capacity: 69 active agents need at least
138 sessions; ten-minute idle release cannot cover active peaks. Share 8 business and 4 checkpoint/maintenance connections
per process, normalized DSN, and driver options. Namespace stays in SQL predicates, not pool identity.
Distinct session settings use separate pools to avoid mixing identity/configuration.

A business lease covers the complete cursor/result/transaction. Explicit transactions retain one connection and the backend's
reentrant lock across calls. Checkout detects bad connections; return commits or rolls back. Do not reconnect later writes
inside a broken transaction. Pool configuration restores search_path. psycopg_pool reclamation, liveness checks, and reconnect
backoff replace the private persistent `_conn`, per-instance idle Timer, and reconnect cooldown.
The `postgres` extra explicitly includes psycopg-pool, already used by checkpoints, rather than implementing another pool.

LangGraph requires ConnectionPool. An internal lease adapter subclasses that type without opening its own pool,
delegates `connection()` to the shared pool, and exposes the same idempotent lease through saver.conn and
Memory._checkpointer_pool to tolerate two host close calls. Each lease releases one reference, waits for its in-flight work,
and closes the real pool only after the last reference. Checkpoint setup is serialized once per pool;
failed setup publishes neither saver nor lease and releases resources. Autovacuum settings and maintenance also use pooled connections.

Each pool allows at most 64 waiters and a 30-second checkout timeout, with `min_size=0` and `max_idle=600`.
Capacity exhaustion errors rather than opening unlimited connections or replaying potentially committed writes.
This is a per-process boundary; other processes/configurations and the host control plane need separate capacity.
Schema, encoding, recall order, and namespace contracts are unchanged. Real PG regression covers 69 agents, transactions/isolation,
saturation/recovery, idle/bad connections, initialization failure, and closure references; SQLite retains shared public-behavior assertions.

## ADR-041: Isolate runtime diagnostics by provenance, protect user knowledge (Accepted)

A runtime failure is not automatically durable knowledge. octop-harness/Octop use `on_failure="error"` to retain model failures
as checkpoint errors, displayed separately by the host; automatic team-failure notifications are excluded from capture.
Hosts still synthesizing error replies mark `payload.octop_runtime_error=true`. Capture, Candidate/Episode extractors,
promotion, and raw prompt recall exclude only structured error provenance. Remove intent/value keyword lists;
agents decide when to call memory_save. Manual writes, approvals, and edits are protected by provenance;
the extraction model judges ordinary conversation's long-term value.
**⚠️ Recall/write path change:** background capture no longer admits short messages through intent exceptions.
Even apply-mode historical governance only soft-deactivates automatic records whose evidence is entirely marked errors.
Legacy fixed retry-failure text only produces operations review hints. Missing/mixed provenance keeps state and returns review_*_ids.
Do not infer provenance from wording, delete raw evidence, or add a schema for this.

## ADR-042: Manual-document priority, conflict candidates, snapshot-bound deduplication (Accepted)

Host users maintain USER.md/MEMORY.md; the existing unified chat loader reads their full text only when a session first loads.
Memory receives current files for governance, not file writing or prompt refresh. Hosts may explicitly reconcile after saving documents.
Ordinary `MemoryService.save()` only notifies a nonblocking scheduler and does not call the comparison model synchronously.
A host worker/maintenance tick calls `review_new_memory(max_atoms=20)` and reads the provider's current documents.
Extraction/promotion does not start synchronous reconciliation.
**⚠️ Recall/write path change:** only high-confidence, mutually exclusive facts in the same subject/field/time scope,
with genuine complete source quotations, can atomically soft-deactivate an old atom, create a replacement Candidate, and journal it.
Keep original evidence; unreviewed replacements do not promote. Old conflicting raw is excluded from automatic fallback;
dirty pages wait for regeneration. Do not force changes for historical events, normal troubleshooting knowledge,
different contexts, or contradictions between the two manual documents themselves.

Keep duplicate atoms; suppress recall only when the actual session snapshot contains the same information.
Do not inject a new file version into an old session. Metadata stores reconciliation versions and audit snapshots;
a separate small manifest participates in recall cache keys so old cache entries cannot revive suppressed content.
Report model failure, uncertainty, fabricated evidence, version changes, and limits explicitly; each call can resume up to 100 new atoms.
Store compared hashes separately from unresolved `uncertain` hashes. Without the legacy unresolved dictionary, checked progress
cannot establish a conclusion: perform one bounded re-audit instead of guessing complete. Reads calculate pending work without writes;
after the first reconciliation writes the new format, retain progress. Cost: even old complete reports may need extra model comparisons.
An explicit empty unresolved dictionary is valid new-format state and must not be invalidated merely because it is falsy.
Adoption checks the last reconciliation version; supplying current documents also catches changes not yet reconciled.
Reuse Service/Bridge/retention/promotion/recall and the existing backend contract, without a new schema, full-text document index,
or separate loader. Shared SQLite/real-PG tests cover transactions, idempotency, evidence retention, and prompt filtering.

## ADR-043: Conditional page writeback using a separate mutation revision (Accepted)

`summary_version` changes with successful generation/manual edits and cannot detect new facts on an already dirty page;
timestamps may also match. Storage manages `mutation_version`, atomically incremented on body writes and dirty invalidation,
without changing summary_version semantics. Regeneration ensures a stub exists, then reads revision, notes, and atoms;
model calls run outside database transactions.
Success and failure writebacks compare the snapshot revision in the same UPDATE. A stale result must not overwrite newer content,
clear new dirty state, or increase a newer page's failure count. Failure bookkeeping does not advance the revision:
its retry count/dirty state does not change generation input, so an otherwise valid concurrent result can still commit.
Once a success advances the revision, a late failure is rejected by its original condition.
Log stale generated results as `page_regen_failed: stale_snapshot`; retry in a later explicit/dirty-batch call, not an unbounded inner loop.
A failed conditional update must not fall back to unconditional upsert. Manual edits, repeated dirty invalidation,
other successful generations, and low-level upserts advance the revision. Upsert does not assign the revision supplied in EntityPage;
imports establish revisions in the destination store.
Both SQLite/PG schema 2 add a default-zero column and migrate existing pages. SQLite checks/adds the column inside a short
BEGIN IMMEDIATE transaction, serializing first migrations across connections. PG checks inside an initialization transaction-level
advisory lock and ALTERs only if missing, avoiding an exclusive lock for no-op DDL. No production dependency is added.
When no version condition is supplied, Memory keeps the legacy backend call signature. Conditional calls require custom backends
to implement the new parameter, maintain revisions, and compare atomically. Never catch TypeError and retry without a condition.
The scope is writers honoring the existing dirty contract; this does not add governance to low-level atom SQL bypassing dirty
invalidation, or promise that page journals and body commits share a transaction.
