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

### Atom-only automatic recall

- **Status:** Validated; [PR #41](https://github.com/TencentCloud/octop-memory/pull/41) targets develop and awaits review/merge. Publishing is outside this delivery.
- **Problem:** Raw-only matches can inject ordinary conversation; page summaries also bypass canonical-atom selection.
- **Scope and acceptance:** Automatic Service/pipeline/Hermes/OpenClaw prefetch uses canonical atoms only, including optional vector atom hits. No matching atoms returns empty. Explicit tools retain multi-source/raw access; no model calls, schema changes, extraction changes, or historical deletion.
- **Recall/write path change:** Source selection for automatic prompt injection changes; existing promoted facts and writes are unchanged. Cache separates source modes.
- **Key files:** `pipeline/recall/__init__.py`, `application/runtime.py`, OpenClaw prefetch/prompt builder; ADR-044 and PROJECT_MAP document source boundaries.
- **Validation:** `make all RUN='.venv/bin/python -m'` with dedicated PostgreSQL 16: format/lint/strict mypy pass, 1608 tests pass, 12 optional host cases skip. Focused Service/recall/retention/manual/Hermes/bridge/backend tests: 402 pass, 8 optional Harness cases skip. `pytest tests/test_backend_parity.py -k AtomOnlyRecall -q -rs` runs all 12 SQLite/real-PG cases without skips, including host-file exclusion. OpenClaw `npm test` passes 69 cases; `npm run typecheck` and `npm run build` pass.
- **Consumer verification:** Load current memory source (base d9170b5 plus this change) with actual Harness source 2fef395. Dedicated `AtomOnlyRecall or HarnessManualReview` cases: 20 pass, no skips, covering both backends; host Service/tools/recall-replay suites: 66 pass, no skips. The ordinary full gate's missing-Harness cases are thus covered separately. Four real-Hermes E2E cases remain unexecuted without a Hermes installation; real OpenClaw host hooks are not exercised by plugin tests.
- **Continuation:** PR #41 is open against develop; relevance admission, independent durable-value screening, and negative-query evaluation remain deferred development plans. Keep the GitHub Wiki development-status pages aligned with changes to these issues and plans.


## Task template

- **Task / status / links:** Needs clarification → Ready → In progress → Awaiting validation → Complete; name any blocker.
- **Problem and scenario:** Actual/expected bug behavior and minimal reproduction, or users, scenario, and desired result.
- **Scope and acceptance:** Included/excluded work, verifiable criteria, compatibility, and data effects.
- **Questions / assumptions:** Separate confirmed facts from unresolved issues; never record an unanswered question as confirmed.
- **Approach and key files:** Minimal implementation, affected call chain, and links to DECISIONS for important tradeoffs.
- **Validation:** Exact commands, actual results, failures/skips/uncovered cases, and sanitized evidence when needed.
- **Continuation:** Current progress, unresolved risks, next steps, and whether affected documents are aligned.
