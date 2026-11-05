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

No recorded task in this document currently requires further implementation. Consult the actual status of open Issues/PRs.
Current capability gaps are in [KNOWN_RISKS](KNOWN_RISKS.md); commands are in [TEST_MATRIX](TEST_MATRIX.md).
Add a record below for new code work. Do not create activity records for routine documentation layout, links, or build maintenance.


## Task template

- **Task / status / links:** Needs clarification → Ready → In progress → Awaiting validation → Complete; name any blocker.
- **Problem and scenario:** Actual/expected bug behavior and minimal reproduction, or users, scenario, and desired result.
- **Scope and acceptance:** Included/excluded work, verifiable criteria, compatibility, and data effects.
- **Questions / assumptions:** Separate confirmed facts from unresolved issues; never record an unanswered question as confirmed.
- **Approach and key files:** Minimal implementation, affected call chain, and links to DECISIONS for important tradeoffs.
- **Validation:** Exact commands, actual results, failures/skips/uncovered cases, and sanitized evidence when needed.
- **Continuation:** Current progress, unresolved risks, next steps, and whether affected documents are aligned.
