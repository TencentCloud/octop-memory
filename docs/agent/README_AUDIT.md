# README_AUDIT.md

Check these claims and both READMEs when public behavior changes. Maintain current contracts, not a sequence of editing reports.

| Claim | Current contract / implementation evidence |
|---|---|
| Architecture | README describes source responsibilities, host calls, and long-term memory flow; detailed entry points are in PROJECT_MAP |
| Documentation | docs/README routes readers by task; integration setup has English/Chinese versions; CONTRIBUTING and agent engineering documents are English, each with a distinct purpose |
| Candidates / pages | Promotion can process candidates automatically; not all require manual review. Dirty pages regenerate on triggers; mutation_version guards reject stale snapshots. summary_version advances with body generation/editing; Episodes extract independently from raw |
| Naming | `octop-memory` / `octop_memory` / `octopmemory`; Python agent host is `octop-harness`. Changed default paths and PG schema require the integration migration guide |
| Quick start | Core needs no optional dependencies; MemoryService.recall returns snippets/rendered; `core.py`, `service.py` |
| Explicit-save kinds | Unreleased `Memory.store` / `MemoryService.save` accept caller-selected Fact/Preference/Decision/Task without an LLM. Default remains Fact; topic/entity type is independent, directory/conflict kinds reject, historical rows are unchanged. Harness tool/policy and consumer version adoption are separate; Task is not a reminder. |
| Facts / tree | AtomCard is canonical; leaves reference it; `core.py`, `storage/backends/` |
| Search scope | Memory.recall projects leaves; Memory.search searches archived messages; automatic prompt recall is atom-only, explicit tool search is multi-source; `core.py`, `pipeline/recall/`, Runtime |
| Recall policy | Automatic gathering uses atom plus optional vector atom hits, with no raw/page fallback. Tools pass atom_only=False; OpenClaw prefetch passes atomOnly=True. Cache separates modes; current-session raw exclusion remains |
| Backends | SQLite/PostgreSQL are relational backends. PG shares an 8+4 connection budget, 30-second waits, and 64 queued requests within a process/configuration. Chroma/Qdrant are indexes; PG skips are not acceptance |
| Capture / pages | Not every message is captured; dirty does not mean regenerated; `application/runtime.py`, `pipeline/page/` |
| Extraction progress | Default batches use 100 events/20,000 estimated characters; oversized events remain. Candidates and durable cursor share a transaction, with whole-pass retries on failure. The cursor does not certify promotion/page/Episode success; extractor and runtime |
| Runtime errors | Online paths exclude structured error provenance only. Agents call memory_save explicitly; no intent/value keyword exceptions. Maintenance deactivates only automatic records whose evidence is entirely marked errors. Legacy text only yields review lists; missing/mixed provenance keeps state; manual protection comes from records; retention modules |
| Host integration | OpenClaw/Hermes share one bilingual guide. Hermes model configuration does not imply automatic extraction; sync_turn only captures. Validate real hooks/installations separately from Python tests |
| Host files | Defaults include MEMORY/USER/DREAMS, dated diaries, topics/projects. HostFilesIndex always uses SQLite; scanning is not semantic review. Service recall/Hermes prefetch do not inherit all tool-search configuration |
| Data maintenance | Keep deduplication, retention, and vacuum distinct. Explain backups, the offline declaration, and old-reader compatibility |
| Manual documents | USER.md/MEMORY.md take priority. save stores/notifies; a host worker calls synchronous `MemoryService.review_new_memory(max_atoms=20)` with a provider, off the reply path. Preserve source/confidence checks, atomic soft deactivation, and replacement candidates. Deduplicate only against actual session snapshots; do not import full documents into atoms/indexes; Service, retention, recall |
| Distribution | Validate sdist allowlists, wheels, and standalone plugins separately. Do not present the source dashboard as a wheel feature |

Entry points and commands live in [PROJECT_MAP](PROJECT_MAP.md) and [TEST_MATRIX](TEST_MATRIX.md).
Correct the affected claim when it drifts; do not copy full README passages or record private runtime samples.
