# AGENTS.md

Shared working agreement for coding agents. Start with the [README](README.md),
[documentation index](docs/README.md), and [CONTRIBUTING](CONTRIBUTING.md).
Engineering knowledge lives in `docs/agent/`; host setup lives in the
[integration guide](docs/integrations.md). Follow the clarification, implementation,
validation, and documentation loop below.
This file governs repository contributions. Hosts own the memory instructions,
workspace templates, and file-access policies used by their running agents.

## Project overview

`octop-memory` is an independently usable and releasable memory library, called at runtime
by Octop, octop-harness, and other hosts. Maintain it as a reusable memory foundation;
users need neither an Octop deployment nor adjacent repository checkouts.
This repository owns evidence-capture rules, fact extraction, recall, storage, and migration.
Upper layers own agent-turn wiring, execution scheduling, and product interfaces.
The core uses the standard library and SQLite; PostgreSQL, vector indexes, and LLMs use optional backends/interfaces.

- Public entry points: `Memory` (`core.py`) and `MemoryService` (`service.py`); shared orchestration is in `application/`.
- Business pipelines live in `pipeline/`, persistence in `storage/`, and CLI/JSON-RPC adapters in `adapters/`.
- `plugins/<host>/` adapts external hosts, currently OpenClaw and Hermes. New adapters reuse shared business logic.
- Find implementation and validation entry points in [PROJECT_MAP](docs/agent/PROJECT_MAP.md)
  and [TEST_MATRIX](docs/agent/TEST_MATRIX.md). Package paths above are relative to `src/octop_memory/`.

## Repository ownership and collaboration

Dependencies flow **Octop → octop-harness → octop-memory**; Octop's memory-management API also calls octop-memory directly.
Production code here must not depend on the other two repositories. Identify the owner of a behavior before editing;
a problem appearing in Octop's UI does not justify copying memory algorithms or agent-turn logic into Octop.

| Change | Primary repository | Check related consumers |
|---|---|---|
| Extraction results, promotion/conflict rules, recall sources/ranking, evidence, data contracts | octop-memory | Check Harness consumers and Octop's management API when public interfaces/results change. Algorithm fixes with unchanged interfaces need no duplicate implementations upstream. |
| Capture/extraction timing, prompt injection/replay, memory-tool binding, background review/maintenance, instance lifecycle | octop-harness | Reuse memory APIs; involve Octop for product switches, persisted settings, or status display. |
| Auxiliary-model selection, retries, timeouts, and configuration forwarding for Octop agents | octop-harness | Octop owns configuration UI/storage/mapping; memory owns the generic LLM protocol and algorithm budgets. Fix host calls rather than hiding failures with prompt changes. |
| Session loading of USER.md/MEMORY.md, agent write protection, runtime memory instructions | octop-harness | Octop owns user editing, owner permissions, and file saving; memory owns semantic reconciliation, conflict candidates, and recall suppression. |
| User memory settings, HTTP/CLI, permissions, memory/review UI, proactive-reminder product flows | Octop | Apply settings through Harness configuration/public memory APIs. Put new algorithms or generic turn behavior in the owning library. |
| Schema, SQL, namespaces, checkpoint encoding, migrations, generic space maintenance | octop-memory | Check Harness resource reuse/closure and Octop statistics, backup/restore, thread cleanup, and online-pause coordination. |
| OpenClaw/Hermes hooks, tool schemas, installation configuration | This repository's `plugins/<host>/` | Reuse this repository's Runtime; external hosts need no changes to octop-harness or Octop. |

Do not confuse the two `MemoryRuntime` classes: this package's `application.runtime.MemoryRuntime`
is host-neutral orchestration; `octop_harness.memory.runtime.MemoryRuntime` adapts assembly and lifecycle to agents.
See the [repository integration map](docs/agent/PROJECT_MAP.md#repository-integration-map) for files and call chains.

For work spanning repositories:

1. State the primary repository, affected consumers, API/configuration/data compatibility, and acceptance criteria
   in the Issue/PR. Read the other repository's AGENTS before editing it.
2. Trace real consumers when public fields, RPC results, configuration, or persistent formats change;
   library unit tests alone are insufficient. Audit Octop's existing direct table readers, but do not use
   those management paths as a template for new business logic that bypasses library APIs.
3. Use [validation across repositories](docs/agent/TEST_MATRIX.md#validation-across-repositories).
   Record the versions/source revisions actually loaded. A green library suite, a green old installation,
   or skipped PostgreSQL/host cases cannot establish cross-repository acceptance.
4. Link related PRs and state dependency order. To consume new interfaces, release the lower-level library first,
   then update Harness/Octop dependency declarations and lockfiles and verify the consumer loads the new version.
   Local source integration does not mean released users have the fix. UI-only work needs no unrelated library edits.

## Working practices

- Check `git status --short` and relevant diffs first; preserve existing user changes.
- For nontrivial work, state the goal, files, expected behavior, minimal validation, and risks. Stage large work and keep scope focused.
- Update affected code, tests, configuration, and documentation together. Avoid unrelated refactors and per-task document proliferation.
- Communicate in the requester's language; preserve code identifiers, CLI flags, environment variables, and error messages verbatim.
- Use generic placeholders and temporary databases. Never commit personal paths, runtime records, credentials, memory exports, or real databases.

## Documentation language

Maintain `AGENTS.md`, `CONTRIBUTING.md`, and `docs/agent/` in English as the engineering source of truth.
Keep the README and integration guide available in English and Chinese, with reciprocal language links;
keep their technical contracts, commands, and examples aligned. The documentation index is in English and links both user-facing languages.

## Engineering documentation workflow

**Scope:** `docs/agent/` records business logic, architecture, public APIs, data contracts, recall/write behavior,
and related bugs, requirements, design tradeoffs, and unresolved risks.
Routine banner, layout, wording, link, Python/dependency/lockfile, build/CI, and personal-tool maintenance belongs
in the affected files and necessary README/CONTRIBUTING/CHANGELOG updates; do not create HANDOFF entries or ADRs for it.
If such maintenance changes runtime behavior or data contracts, document that code impact rather than a maintenance diary.
The following requirements apply according to actual impact, not file count or whether source files changed.

1. **Check in:** inspect relevant code, diffs, and recent related commits against
   [HANDOFF](docs/agent/HANDOFF.md) and [PROJECT_MAP](docs/agent/PROJECT_MAP.md).
   Check recall sources/order, public APIs, configuration, prompts, and validation commands.
   Judge freshness from implementation, not dates or commit counts. Correct drift; record out-of-scope gaps in
   [KNOWN_RISKS](docs/agent/KNOWN_RISKS.md).
2. **Clarify in text:** use the existing Issue/PR or HANDOFF to describe actual/expected behavior and a minimal
   reproduction, or the user scenario, scope, exclusions, and verifiable acceptance criteria.
   List assumptions and unresolved questions. Resolve questions affecting scope, compatibility, or data safety;
   continue already-authorized work without asking for the same permission again.
3. **Implement and validate:** complete the smallest verifiable loop and run relevant tests and the full gate.
   Apply the mandatory recall/write and backend rules below when affected.
4. **Update knowledge:** for nontrivial code work, update HANDOFF with current status, key files, actual validation,
   risks, and next steps. Update PROJECT_MAP for modules/call chains/runtime configuration;
   [DECISIONS](docs/agent/DECISIONS.md) for important tradeoffs;
   [TEST_MATRIX](docs/agent/TEST_MATRIX.md) for validation changes; KNOWN_RISKS for code-related gaps;
   [GLOSSARY](docs/agent/GLOSSARY.md) for terms; and check [README_AUDIT](docs/agent/README_AUDIT.md)
   when public behavior changes.

Each document owns its facts. HANDOFF keeps active work and unresolved items; move merged tasks out of active work,
retaining only useful history links and follow-up boundaries. Put durable knowledge in the map, decisions, tests, or risks.
Merged code is not evidence of fresh validation in the current environment.
Keep stable ADR numbers and supersession relationships, check statuses against implementation, and avoid dated activity logs.
Correct documentation drift in the relevant knowledge files without recording documentation cleanup as a new code task.
Updating only HANDOFF while omitting affected maps or test guidance is incomplete.

## Architecture constraints

- `Memory` is the public storage API. Shared host operations in `MemoryService`/`Bridge` reuse `application.MemoryRuntime`.
  `MemoryService.recall()` calls the prompt pipeline directly; verify tool-search and prompt-recall configuration/sources separately.
- pipeline/storage/domain must not depend on adapters; storage must not depend on pipeline; service must not depend on bridge.
- New hosts use shared application logic. The source dashboard's direct SQLite access is an existing exception, not a new-adapter template.
- `AtomCard` is the canonical fact; tree leaves reference atoms. Chroma/Qdrant are optional indexes, not relational backends.
- Runtime incremental extraction commits candidates and the durable cursor in one transaction, then advances in-memory progress.
  Never mark success before candidates persist, or equate that commit with successful promotion/page/Episode stages.
  Check lower-level extractor failure semantics separately.
- Automatic page regeneration passes and atomically checks the snapshot's `mutation_version`; failed guards must not fall back
  to unconditional writes. Preserve legacy public low-level page calls without version conditions; all page writers maintain revision semantics.
- `MemoryService.save()` stores the fact and notifies a nonblocking scheduler. Hosts run synchronous `review_new_memory()` in the background.
  Current manual files govern reconciliation; the actual chat snapshot governs recall deduplication. Keep those inputs distinct.
- Verify host configuration forwarding, hook scheduling, and actual execution separately. A configured model does not prove extraction ran;
  do not assume profiles have identical semantics across hosts.
- Do not change public APIs, data formats, configuration defaults, or production dependencies without task authorization.

Detailed contracts are in [PROJECT_MAP](docs/agent/PROJECT_MAP.md); host capabilities and limits are in the
[integration guide](docs/integrations.md) and [KNOWN_RISKS](docs/agent/KNOWN_RISKS.md).

## SQLite and PostgreSQL gate

Both are supported production memory backends. Changes to public reads/writes, persistence orchestration,
backend contracts, schema, SQL, transactions, FTS, or lifecycle must:

1. Maintain both implementations and validate the same behavior assertions, preferably in `tests/test_backend_parity.py`.
2. Use a real dedicated PostgreSQL database through `TEST_POSTGRES_DSN`; confirm relevant PG cases did not skip.
3. Explicitly reject the other backend for deliberately backend-specific capabilities, documenting the reason in KNOWN_RISKS.
4. Keep backend-specific SQL in `storage/backends/`. Existing capability gaps do not justify new silent divergence.

## Sensitive recall and write paths

| Area | Explain the effect on |
|---|---|
| `core.py`, `storage/backends/` | Public writes, transactions, schema, indexes, namespaces |
| `pipeline/extractor/`, `pipeline/promotion/`, `pipeline/episode/` | What becomes a Candidate, AtomCard, or Episode |
| `pipeline/recall/`, `pipeline/page/` | Sources, ranking, raw policy, deduplication, budgets, page content |
| `application/runtime.py`, `application/path_projection.py`, `application/host_files.py` | Tool search, readable paths, host files |
| `adapters/bridge/handlers.py`, host hooks | Host capture/extraction and memory_search/memory_get behavior |

Mark behavior changes with `⚠️ Recall/write path change`, naming the affected sources/layers and validation commands.
For documentation/build-list edits, state that behavior is unchanged; do not describe documentation cleanup as a runtime change.

## Validation and delivery

```bash
make install-hooks    # Once per clone; never bypass a failed gate.
make all              # Format + lint + strict mypy + pytest.
```

Run focused tests before the full gate for code changes. Documentation-only work needs relevant documentation checks;
the full gate still runs before a commit.
Use `tests/test_agent_docs.py` and `tests/test_architecture_boundaries.py` for documentation checks.
When adding/moving files or changing headings, update navigation, local links, and heading anchors;
check public-document coverage, directory contracts, and the sdist allowlist.
Find the first failure's cause, make the smallest in-scope fix, and rerun.
If the environment or an existing issue prevents validation, report failures/skips and reproduction commands without claiming success.
Before delivery, confirm acceptance and alignment with code. For work within the engineering-documentation scope,
record actual validation and unresolved items in HANDOFF. Never label an unvalidated state as accepted.
Summarize changes, validation, and remaining limits; no fixed multi-section handoff response is required.

Without explicit user authorization, do not push, rewrite Git history, delete large directories, deploy production, or change secrets.
See [TEST_MATRIX](docs/agent/TEST_MATRIX.md) for backend/environment commands and
[CONTRIBUTING](CONTRIBUTING.md) for branching and release checks (`develop` integration and the `/publish` skill).
