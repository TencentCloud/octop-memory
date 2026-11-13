# KNOWN_RISKS.md

Keep risks that still affect maintenance: triggers, effects, mitigations, and validation.
Remove obsolete descriptions after a fix, or retain a short Resolved note with its PR.

## Writes, extraction, and review

- The 20,000-character extraction budget estimates content plus overhead; it is not a strict prompt/token limit.
  An oversized event still forms its own batch and can exceed model limits. Runtime reads at most 10,000 session
  raw events per call, not necessarily all history. Any model-batch failure prevents all candidate/cursor commits
  for that pass, so retries can repeat model cost. Promotion, pages, and Episodes run after that commit;
  the cursor is not an all-stages-success marker. See extraction checks in TEST_MATRIX.
- Low-level `extract_session(..., persist=True)` persists returned partial candidates without checking the overall
  `failure_reason` and does not maintain the Runtime cursor. Hosts needing incremental rollback semantics use
  Service/Bridge extract; direct extractor callers inspect the result before persisting it.
- EntityPage revision protection depends on every writer maintaining mutation_version. Upgrade all writing processes;
  old processes or direct SQL bypassing revisions/dirty invalidation do not have the same guarantee.
  Legacy page APIs without a version condition retain their old backend call form. Custom backends used for automatic
  regeneration must implement conditional parameters and revision maintenance; never ignore a guard or retry unconditionally.
  Rejected stale results are not immediately retried. Hosts later trigger dirty pages; newer clean user/generated pages stay clean.
- Background manual-document review requires a nonblocking host scheduler/provider and a worker/maintenance call to
  `MemoryService.review_new_memory`. The entry point is available from 1.0.3; a host upgrade retaining memory 1.0.2
  lacks it and skips review. Raise the host's minimum dependency. The method synchronously calls the model and must
  not return to the user-facing save path.
- Semantic reconciliation needs an auxiliary model. Only exact source quotations, explicit subject/scope, and high-confidence
  conclusions can deactivate facts; failures/uncertainty do not. Calls inspect at most 100 unchecked atoms and 100,000 document
  characters. Hosts must expose partial/uncertain/unavailable states and continuation. Without a provider, external hosts only
  reconcile explicitly. Concurrent file updates require a host freshness callback. Adoption should supply current documents;
  the lower-level worker at least checks the last reconciliation version. Old-version candidates remain auditable but cannot be
  adopted directly: reject and reconcile the new version. Conflicting raw events are excluded wholesale from automatic fallback;
  other mixed facts remain reachable through valid atoms/direct evidence reads. Historical Episodes are not bulk-deleted. See ADR-042.
- Legacy audit metadata without an `uncertain` dictionary needs one conservative re-audit, including records previously reported
  complete. Old progress cannot distinguish comparison from verification, so additional model cost is expected.
  Status reads only calculate pending work; reconciliation remains bounded and resumes normally after writing the new format.
- Unmarked synthetic errors cannot reliably be distinguished from ordinary chat, so online paths retain them; not all historical
  errors are isolated. Main octop-harness/Octop paths raise exceptions; custom hosts synthesizing replies must mark
  `payload.octop_runtime_error=true`. Legacy fixed error text only produces review hints. Missing/mixed evidence yields read-only
  review_*_ids, not new UI tasks or automatic deactivation. Manual saves/approval/edits are protected by provenance, not wording. See ADR-041.

## Storage and maintenance

- Shared PG pool budgets apply only within one process and connection configuration. Reserve additional server capacity for
  other processes, configurations, and host control-plane connections. Long transactions occupy the business pool;
  saturation waits for a bounded time, then errors rather than growing indefinitely. CI/release uses dedicated PG;
  a local environment skipping unavailable services is not PG acceptance.
- `run_gc()` retention currently supports SQLite only; PG returns empty statistics. This is an existing capability gap.
  Row deletion, checkpoint retention, and physical reclamation are different operations; none proves a host schedules them automatically.
- Checkpoint pruning and content deduplication/expansion support SQLite only. PG explicitly rejects these paths and uses its original
  saver. Do not equate PG vacuum with checkpoint retention; see [checkpoint maintenance](../../CONTRIBUTING.md#checkpoint-maintenance).
- Memory written by 0.9.6 to 0.9.11 sits in PG schema `harness_memory`. A namespace still empty in `octop_memory` is copied on open;
  one that already has new rows is only merged through `Memory.merge_legacy_schema()`, which skips rows whose keys exist and can leave
  similar entities or branches twice. The old schema is never changed or dropped. SQLite never used it and rejects the merge.
  The per-namespace lock does not stop ordinary writes: a new-version writer in another process during the automatic copy ends up
  merged, and 0.9.x writers after the copy are only reported (`legacy_grew`, net growth per table; one-for-one replacement is not
  detected). Dropping the whole `octop_memory` schema loses the migration and purge markers, so old rows would be copied again.
  A merge into a table with a deferrable unique constraint is refused (`unsupported`): `ON CONFLICT` cannot use it.
- The older per-namespace-schema migration (`_migrate_legacy_schema`) relies on the connection's implicit transaction; with
  `autocommit=True` a failure part-way can leave a partial copy. The pre-1.0 shared-schema copy uses an explicit transaction.
- Without PostgreSQL `btree_gin`, indexes fall back to plain GIN. Namespace filtering stays correct but performance isolation weakens.
  Shared checkpoint maintenance needs database-wide scope checks; even ordinary VACUUM cannot promise no lock waits.
- Some SQLite runtimes report `attempt to write a readonly database` during read-only FTS checks.
  Verify engine compatibility rather than skipping integrity checks.

## Host integration and distribution

- Hermes `llm`/`extraction` configuration only forwards capabilities to Runtime. Current `sync_turn()`, `on_memory_write()`, and
  `on_pre_compress()` only capture; they do not extract automatically. Additional host scheduling must call extract explicitly.
  Configuring a model alone does not generate atoms. Shutdown joins each thread for at most 5 seconds and does not close the
  Memory backend; inspect slow tasks and resource release separately. See the [provider](../../plugins/hermes/octopmemory/__init__.py)
  and [integration guide](../integrations.md#hermes).
- `HostFilesIndex` remains a SQLite sidecar with PG memory. Scanning files is not semantic document review.
  `MemoryService.recall()` and Hermes prefetch call prompt recall directly, without automatically applying all Runtime
  tool-search configuration or host-file sources.
- Hermes/Python Runtime does not expand profile policies. Setting `profile="privacy"` alone does not tighten capture/redaction.
  Set policies explicitly and inspect stats. OpenClaw's TS profile table applies only to that plugin.
- The dashboard accesses SQLite directly and is absent from the wheel. Optional-dependency versions, large migrations,
  and real host hooks require separate validation.
- Validate Hermes installation interpreters, `.pth` locations, and standalone versus installer distributions separately.
  OpenClaw hybrid recall depends on a real host invoking prefetch.
- `scripts/memory-slim --online` depends on an adjacent Octop checkout and prefers its virtual environment.
  Standalone users use `octop-memory db slim`; online maintenance still requires host coordination.
- `deploy-openclaw.sh` installs dependencies, updates the checkout, and replaces plugin directories.
  `--setup-dry-run` previews configuration steps only. Deployment and real-host E2E require dedicated environments.

## Retrieval quality and validation

- Atom-only injection removes raw/page fallback, not low-value or off-topic atoms already promoted. Relevance admission,
  independent durable-value checks, and negative-query evaluation remain follow-up work. No additional model call is introduced.
- Hermes captures raw without automatic extraction; its prefetch stays empty until canonical atoms exist through save or promotion.
- Machine load affects recall wall-clock budgets. Distinguish scheduling jitter from actual retrieval regressions when investigating timeouts.
- Chroma/Qdrant adapters currently have fake/mock coverage, not real vector-backend acceptance.
  Installing an extra does not initialize/inject an index or enable vector recall.
- Synthetic recall evaluation is not a production quality gate; see its labels, time sensitivity, and sample limitations in
  [TEST_MATRIX](TEST_MATRIX.md#validation-boundaries).
- Driver, I/O, and payload errors degrade according to each entry point's exception tuples; other exceptions propagate.
  Vector-client exceptions absent from `VECTOR_ERRORS` are not swallowed. See ADR-038.

## Existing installation migration

Source uses current distribution/plugin names, environment variables, default directories, and PG schema.
Existing installations do not migrate automatically. Follow the
[integration guide](../integrations.md#renaming-and-migrating-existing-installations); persistent-format choices are in ADR-037.
