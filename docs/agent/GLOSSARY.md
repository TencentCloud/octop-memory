# GLOSSARY.md

Core terminology only; see [PROJECT_MAP](PROJECT_MAP.md) for modules and data flows. Preserve code identifiers verbatim.

| Term | Meaning |
|---|---|
| octop-memory / octop_memory / octopmemory | Project/distribution name / Python package / host plugin ID |
| Engineering maintenance loop | Clarify in text → implement minimally → validate → update project knowledge; not a new task diary |
| Memory / MemoryBackend | Public storage API / shared SQLite and PostgreSQL contract |
| octop_memory MemoryRuntime | `application.runtime.MemoryRuntime`: host-neutral orchestration shared by MemoryService and Bridge |
| octop_harness MemoryRuntime | `memory.runtime.MemoryRuntime`: adapts Memory/Service, models, tools, middleware, and resource lifecycle to agents; distinct from this library's class |
| Bridge | JSON-RPC adapter; OpenClaw uses stdio and Hermes reuses it in-process |
| RawEvent / L0 | Immutable source evidence, subject to capture-policy filtering |
| Candidate / L1 | Extraction result awaiting review/promotion, with evidence references |
| AtomCard / L2 | Canonical fact; corrections append a successor and invalidate the old fact |
| Episode / L2.5 | Event memory; absolute times use aware UTC |
| Entity / EntityPage / L3 | Entity anchor / versioned, regenerable consolidated page |
| journal / L4 | Business decisions and selected lifecycle records; not a log of every SQL operation |
| root / branch / leaf | Organization-tree levels; leaves reference facts through atom_id |
| source | Recall candidate source: atom/raw by default, with conditional page_headline/vector sources |
| host file / virtual path | Indexed snapshot of host Markdown / readable path projected from a database record; tool access does not imply prompt injection |
| raw_policy | Controls raw participation in prompt recall; fallback is the default |
| extraction cursor | Meta key `extracted_ids/v1/<session>`; indicates candidate extraction committed, not that promotion/page/Episode stages all completed |
| summary_version / mutation_version | Page generation/edit version / storage-managed mutation revision; the latter rejects stale generation writebacks |
| manual_documents / manual_context | Current files for semantic governance / actual session snapshot for recall deduplication; governance does not refresh chat context |
| uncertain | Compared but inconclusive active assertions, tracked by assertion hash; neither complete nor unexamined |
| scheduler / review worker | Immediately returning post-save enqueue callback / host background task that calls synchronous review |
| namespace | Memory isolation identity: SQLite table prefix or namespace key in PostgreSQL's shared schema |
| checkpoint_ns | LangGraph execution-flow namespace, distinct from the memory namespace |
| retention / vacuum | Deleting history according to retention rules / maintaining physical storage space; not interchangeable |
| VectorIndex | Optional retrieval augmentation; does not replace the relational backend |
| ADR | Design decision with a stable number, rationale, costs, and supersession relationships |
