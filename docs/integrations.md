# External agent integrations

**English** · [中文](integrations_CN.md)

Host integration, adapters, and validation for the current OpenClaw/Hermes implementations.
See the [README](../README.md), [CONTRIBUTING](../CONTRIBUTING.md), and [documentation index](README.md)
for core APIs, development, and navigation.

Read by task: [Python hosts](#python-hosts) · [JSON-RPC bridge](#json-rpc-bridge) · [OpenClaw](#openclaw) ·
[Hermes](#hermes) · [Troubleshooting](#shared-verification-and-troubleshooting) ·
[Existing installations](#renaming-and-migrating-existing-installations).

## Integration choices and boundaries

Python hosts should use `MemoryService`; other languages or isolated processes use stdio `octopmemory-bridge`.
For a specific agent's plugin lifecycle, tool schemas, and installation configuration, adapt it under `plugins/<host>/`.
`src/octop_memory/adapters/` provides library-side CLI/protocol entry points; `plugins/` wires external hosts.

A new adapter specifies namespace/session/thread mapping, capture timing, recall/extraction timing, and shutdown cleanup.
Reuse `MemoryService`/`MemoryRuntime` rather than duplicating extraction, promotion, or ranking in plugins.
Test contracts against temporary databases, then validate installation, I/O, and lifecycle with a real host.
Python unit tests do not replace host E2E.

## Python hosts

Import `Memory`/`MemoryService` from `octop_memory`. The runnable
[public API example](../examples/basic_usage.py) writes only a temporary database.
Hosts provide identities, decide when to call, and manage background work; the library does not start an autonomous memory loop.

| Stage | Call and contract |
|---|---|
| Initialize | `Memory(namespace=..., backend_config={"db_path": ...})`, then `MemoryService(memory, llm=..., config=..., host=...)` |
| Explicit save | `service.save(content, topic=...)` returns a canonical tree leaf without an LLM. Unreleased/develop adds `kind="Fact"/"Preference"/"Decision"/"Task"`; callers choose it, independent of topic/entity type. Omission retains Fact; v1.0.3 has no kind parameter. |
| Turn end | `capture_turn(user=..., assistant=..., session_id=..., thread_id=...)` writes L0 according to policy; background execution is the host's responsibility |
| Extract | `extract(session_id, incremental=True, promote=True, regen_pages=True)` runs synchronously outside the reply path; inspect `failure_reason` and each stage result |
| Prompt injection | `recall(query, session_id=..., thread_id=..., manual_context=...)`; consume `rendered` or `snippets` |
| Tool reads | `search(query, max_results=..., corpus=...)` returns `hits`; pass an actual returned path to `get(path, start=..., lines=...)` |
| Close | Stop/wait for host-created tasks before releasing owned backend, checkpoint, and file-index resources |

`Memory.search()` searches archived chat messages; `Memory.recall()` returns fact-backed leaves.
Neither is the same as Service tool search or prompt recall. Service `recall()` calls the prompt pipeline directly;
automatic injection is atom-only, including optional vector atom hits, and returns empty when no atom matches.
Runtime `recall` configuration serves explicit tool search; its raw/host-file policies do not widen automatic injection.
OpenClaw hybrid prefetch passes `atomOnly: true` to the bridge and filters host-prefetched results to atom paths.
Direct multi-source pipeline callers must explicitly pass `atom_only=False`.

Manual USER.md/MEMORY.md governance is optional wiring: pass `manual_documents_provider` when constructing Service,
register an immediately returning enqueue callback with `set_manual_review_scheduler()`, and let a worker call synchronous
`review_new_memory(max_atoms=20)`. Never wait for review inside the scheduler callback.
Explicit reconciliation uses `reconcile_manual_documents()`; `manual_context` is the actual chat snapshot, a distinct input.
The public background-review entry point is available from 1.0.3; see
[manual document governance](agent/PROJECT_MAP.md#manual-document-governance) for statuses/version checks.

## JSON-RPC bridge

The base installation provides `octopmemory-bridge` without the CLI extra.
Run `octopmemory-bridge --probe` to check SQLite/FTS5, then start a process:

```bash
octopmemory-bridge --namespace demo --db-path /path/to/demo.sqlite
```

stdin/stdout use one JSON object per line; stderr carries logs. For example, send these requests in order:

```json
{"jsonrpc":"2.0","id":1,"method":"handshake","params":{"client_version":"my-agent"}}
{"jsonrpc":"2.0","id":2,"method":"memory_search","params":{"query":"Python","maxResults":5,"corpus":"all"}}
```

Responses contain the same id and either `result` or `error`. Protocol/parameter failures use JSON-RPC errors;
unavailable extraction can return a normal result with `failure_reason`, so checking only error envelopes is insufficient.
Common methods include `capture`, `extract`, `promote`, `memory_search`, `memory_get`, `stats`, `reindex`,
`reconcile_manual_documents`, and `manual_document_review_status`. Fields follow
[handlers/dispatch](../src/octop_memory/adapters/bridge/handlers.py) and
[Runtime](../src/octop_memory/application/runtime.py), not a guaranteed one-to-one mapping to Service method names.
Batching and durable progress share the [Runtime contract](agent/PROJECT_MAP.md#extraction-and-durable-progress).

## Available adapters

| | OpenClaw | Hermes |
|---|---|---|
| Plugin | npm `@octop-memory/openclaw` | Native provider in `plugins/hermes/octopmemory` |
| Transport | TypeScript → stdio `octopmemory-bridge` | Python provider → in-process `Bridge` |
| Python environment | Separate uv/pipx environment with a usable bridge path | Install into Hermes' actual interpreter |
| Storage | SQLite default; PostgreSQL configurable | Provider currently constructs SQLite |
| Automatic extraction | After `agent_end` capture, when enabled and `llm.endpoint` is configured | Current `sync_turn()` only captures; it does not call extract |
| Default namespace | `openclaw__default` | `hermes__default` |
| Configuration | `plugins.entries.octopmemory.config` in `openclaw.json` | `$HERMES_HOME/octopmemory.json` |

Use Python 3.12+ and SQLite with FTS5. Host requirements follow the
[OpenClaw manifest](../plugins/openclaw/octopmemory/package.json) and Hermes provider contract.
Hermes needs v0.10.0+ with `MemoryProvider`; installation instructions are not acceptance of every later host version.

## OpenClaw

### Installation and activation

```bash
uv tool install 'octop-memory[cli]'
octopmemory-bridge --probe
openclaw plugins install @octop-memory/openclaw
octop-memory openclaw setup
openclaw gateway restart
octop-memory openclaw doctor
```

Alternatively use `pipx install 'octop-memory[cli]'`. Probe should return `ok: true` and `fts5: true`.
Use the Python CLI for first setup: `openclaw octopmemory` may not exist until the plugin owns the memory slot.
Setup configures `plugins.slots.memory=octopmemory`, conversation access, namespace, and bridge path.
Running npm install in an arbitrary directory does not register a host plugin.

```bash
octop-memory openclaw setup --profile proactive --namespace my_project
octop-memory openclaw setup --db-path /path/to/memory.sqlite
octop-memory openclaw setup --bridge-python /path/to/python
octop-memory openclaw print-config --profile privacy
```

Setup defaults to `~/.openclaw/octopmemory/<namespace>/memory.sqlite`, following a custom OpenClaw home.
Only a bridge without an explicit path falls back to `~/.octopmemory/<namespace>/memory.sqlite`;
inspect generated configuration. `bridge.command` takes precedence over `bridge.python`; setup locates the console script.
For PG, install `[postgres]` in the bridge environment, set `backend: "postgres"`, and inject `OCTOP_MEMORY_DSN`
into the plugin-starting process. Do not put credential-bearing DSNs in JSON forwarded through argv.

### Configuration and profiles

Profile defaults apply before explicit overrides. Old profile tables are not the runtime source of truth;
see [config.ts](../plugins/openclaw/octopmemory/src/config.ts) for fields/defaults/merge behavior and
[openclaw.plugin.json](../plugins/openclaw/octopmemory/openclaw.plugin.json) for host validation.

| Profile | Purpose |
|---|---|
| `balanced` | Default tool hints and conversation capture, with raw fallback |
| `low_latency` | Fewer search sources and narrower capture |
| `proactive` | `hybrid` recall, requiring the host prefetch hook |
| `privacy` | Restricted sources, placeholder raw bodies, metadata may remain |
| `archive` | Broader conversation retention, including more tool content |
| `eval` | Synthetic evaluation without short-message fixture filtering |

Common overrides, merged into `plugins.entries.octopmemory.config`:

```json
{
  "profile": "balanced",
  "namespace": "my_project",
  "recall": {"raw_policy": "fallback", "default_max_results": 5},
  "capture": {"include_roles": ["user", "assistant"], "include_tool_results": false},
  "privacy": {"redact_secrets": true, "store_tool_payloads": false}
}
```

| Group | Important fields and semantics |
|---|---|
| `recall` | `mode`: `off` / `tool_hint` / `hybrid`; `default_corpus`: `all` / `memory` / `sessions` / `wiki` |
| `recall` | `raw_policy`: `never` / `fallback` / `always`; `host_files_policy`: `off` / `include` / `only` |
| `recall` | `default_max_results`, `max_prompt_chars`, `layer_order`, `citation_policy` control counts, display budget, ordering |
| `capture` | `agent_end_hook`, `include_roles`, `min_message_chars`, `include_tool_calls/results` control capture scope |
| `capture` | `skip_memory_echo` filters injected snippets; `host_files_watcher` controls Markdown indexing |
| `privacy` | `redact_secrets`, `redact_patterns`, `store_raw_content`, `store_tool_payloads` |
| `llm` | `endpoint`, `model`, `model_heavy`, `api_key_env`, timeouts/retries; prefer environment variables over argv-visible keys |
| `extraction` | `promote`, `regen_pages`, `max_candidates`, `page_regen_limit`; Python Runtime/Hermes also accepts `max_tokens`, subject to each host schema |
| `compaction` | Host context-flush thresholds and subsequent indexing, distinct from database VACUUM |
| `bridge` | `command` / `python`, `log_level`, `spawn_timeout_ms` |

`recall.mode=off` disables the prompt section, not capture or history retention.
`tool_hint` asks the model to use tools; `hybrid` renders results after the host invokes `recallPrefetcher`.
OpenClaw automatic extraction also needs `capture.extract_on_agent_end` and `llm.endpoint`; without a model, it only captures L0.
Legacy `recall.limit`/`total_chars` remain compatible; new configuration uses `default_max_results`/`max_prompt_chars`.

## Hermes

### Installation and activation

Hermes' installer accepts `owner/repo/path/to/plugin`, so the provider can be installed directly from this repository:

```bash
hermes plugins install TencentCloud/octop-memory/plugins/hermes/octopmemory \
  --ref <full-40-character-commit-SHA>
hermes memory setup
hermes gateway restart
hermes memory status
```

`--ref` must be a full 40-character commit SHA; the host rejects short SHAs.
Pin a validated revision. Source/revision metadata is stored in `~/.hermes/plugins/.install-metadata.json`.
Select `octopmemory` in `hermes memory setup`; the host installs the `octop-memory[cli]` dependency declared by plugin.yaml.
Confirm loading with `hermes memory status`, then search a word from synthetic test chat:
`hermes octopmemory search "Python" -n 5 --corpus all`.

If dependency installation fails, inspect `hermes --print-runtime-command`: its first argument identifies the actual gateway
Python. Install dependencies into that interpreter, not a guess based on the `hermes` shebang, which may be a shell wrapper.
Install `octop-memory[cli]` there and restart the gateway.

This repository does not publish a separate `octop-memory-hermes` package to PyPI; that installation route returns 404.
`plugins/hermes/octopmemory/installer.py` is a copy/activation utility for repository E2E, not the released installation path.
Maintainers can distribute a standalone plugin directory through `scripts/build-hermes-plugin.sh`.
The repository does not assume the provider is built into upstream Hermes.

### Configuration and files

| Path | Purpose |
|---|---|
| `$HERMES_HOME/config.yaml` | Select `memory.provider` |
| `$HERMES_HOME/octopmemory.json` | Plugin configuration; defaults when omitted |
| `$HERMES_HOME/octopmemory/memory.sqlite` | Default store |
| `$HERMES_HOME/memories/` | Default host Markdown root |

`hermes memory setup` calls `get_config_schema()`/`save_config()`.
Dotted keys become nested configuration and deep-merge with the existing file.
Supported blocks include profile, namespace, db_path, recall/capture/privacy, llm/extraction, and host files;
see the [provider](../plugins/hermes/octopmemory/__init__.py). Do not copy OpenClaw's TS defaults into Hermes.
In current Hermes/Python Runtime, `profile` is a configuration label, not an expansion of OpenClaw policies.
For privacy/low-latency behavior, set `recall`, `capture`, and `privacy` explicitly and inspect effective status configuration.

```json
{
  "namespace": "hermes__default",
  "recall": {"default_max_results": 5, "raw_policy": "fallback"},
  "capture": {"min_message_chars": 0, "include_roles": ["user", "assistant"]},
  "privacy": {"redact_secrets": true, "store_tool_payloads": false},
  "llm": {"base_url": "https://<openai-compatible>/v1", "model": "<model>", "model_heavy": "<model>"},
  "extraction": {"promote": true, "regen_pages": true}
}
```

`llm`/`extraction` forward to Runtime with bridge `--config-json` semantics.
A valid `endpoint` or `base_url` plus `model` enables the model client; missing/rejected configuration falls back to NoopLLMClient.
Explicit extraction then returns `failure_reason` without preventing raw capture.
Extraction controls `promote`, `regen_pages`, `max_candidates`, `max_tokens`, and `page_regen_limit`.

Current `sync_turn()` only calls capture in the background; `on_memory_write()` and `on_pre_compress()` also write raw only.
These hooks do not call extract. Model configuration alone therefore creates no Candidates/Atoms.
Additional host scheduling must call shared Runtime/Bridge `extract`; the provider has no separate automatic-extraction switch.
Hermes `prefetch()` calls atom-only prompt recall directly; raw-only capture produces no automatic context until facts are saved or extracted/promoted. Tool settings such as `recall.raw_policy` apply to explicit search.
Prefer environment credentials: without `api_key`, the client reads `api_key_env` (default `OCTOPMEMORY_LLM_API_KEY`),
keeping plaintext keys out of shared configuration files.

## Shared verification and troubleshooting

Run several synthetic conversations in a test environment, then search words actually captured:

```bash
openclaw octopmemory status
openclaw octopmemory search "Python"
hermes octopmemory status
hermes octopmemory search "Python" -n 5 --corpus all
```

Pass returned paths to `memory_get` or host `show`; do not guess atom IDs.
Check layer, content, and namespace, then verify the model prompt actually uses the results.

| Symptom | Check in order |
|---|---|
| Bridge fails to start | FTS5 probe, effective Python path, dependencies, handshake logs |
| Hermes import fails | Actual interpreter from `hermes --print-runtime-command`, not the terminal's default Python |
| Plugin not loaded | Memory slot/provider, install location, restart, host status |
| No raw | Conversation access, capture switch, roles, short-message threshold, privacy, actual DB/namespace |
| No atoms | Valid LLM, actual extract invocation, failure_reason, Candidate state; Hermes sync_turn alone does not extract |
| Tool search works but prompt is empty | OpenClaw recall.mode and real prefetch invocation; doctor success does not prove injection |
| Host file missing | host_files_root, watcher, allowlist; defaults do not scan the whole workspace |

Enabled file indexing scans root MEMORY.md/USER.md/DREAMS.md, date-named `memory/*.md` including slug variants,
and `topics/*.md`/`projects/*.md`. Custom allowlists use safe relative Markdown globs, not the whole workspace.
Hits are SQLite index snapshots, even with PG memory; file indexing is not completed semantic document review.
Migration uses core CLI `portable list-sources / pack / adopt / doctor`; exported `.hmpkg` contains memory and must not be committed.

## Modifying adapters

Put shared behavior in `application/`; host adapters only translate hook/tool contracts.
Current adapters expose `memory_search(query, maxResults?, minScore?, corpus?)` and
`memory_get(path, from?, lines?, corpus?)`. Search returns `hits[]`, reads return `path/excerpt`;
follow the actual tool schema for corpus and fields.

Hermes `initialize` accepts `hermes_home`; provider methods and manifest event hooks are separate interfaces.
`sync_turn` captures in the background, `prefetch` recalls synchronously, and `queue_prefetch` is currently a no-op.
Shutdown joins each thread for at most 5 seconds and stops the file index; it neither guarantees all slow work finished nor closes Memory.
[plugin.yaml](../plugins/hermes/octopmemory/plugin.yaml) and [contract tests](../tests/test_hermes_contract.py) define the interface.
Fallback stubs support repository unit tests only; real Hermes compatibility requires E2E.

```bash
uv run pytest tests/test_hermes_contract.py tests/test_hermes_adapter.py tests/test_hermes_install.py -q
HERMES_SOURCE=/path/to/hermes-agent bash scripts/e2e_hermes_test.sh
npm ci --prefix plugins/openclaw/octopmemory
npm test --prefix plugins/openclaw/octopmemory
npm run typecheck --prefix plugins/openclaw/octopmemory
npm run build --prefix plugins/openclaw/octopmemory
bash scripts/e2e_openclaw_test.sh
```

Read E2E scripts first and use isolated environments: they may install/start hosts.
A missing real host that causes skips is not compatibility acceptance.

<a id="更名与已有安装迁移"></a>

## Renaming and migrating existing installations

Use `octop-memory` for the distribution/CLI, `octop_memory` for Python imports, `octopmemory-bridge` for the bridge,
and `octopmemory` for OpenClaw/Hermes plugin IDs. Entry-point naming is breaking;
there are no old command/import/environment aliases. Persistent-format compatibility is described below.

For previous installations, stop hosts and all database writers, back up configuration/data, then update installation/startup settings:

| Setting | Current name |
|---|---|
| Distribution / Python import | `octop-memory` / `octop_memory` |
| Bridge / Hermes plugin | `octopmemory-bridge` / this repository's `plugins/hermes/octopmemory` |
| OpenClaw package / plugin ID | `@octop-memory/openclaw` / `octopmemory` |
| Environment prefixes | `OCTOP_MEMORY_*` / `OCTOPMEMORY_*` |
| Local data directories | `~/.octop-memory/` / `~/.octopmemory/` |
| Plugin data directories | `~/.openclaw/octopmemory/` / `$HERMES_HOME/octopmemory/` |
| Deployment-directory parameter | `OCTOP_MEMORY_DIR` / `--octop-memory-dir` |
| Hermes configuration | `$HERMES_HOME/octopmemory.json` |
| Python agent host | `octop-harness` |

- After installing the distribution, update imports, bridge paths, OpenClaw entries/slot/allow/install settings, and Hermes provider.
  Migrate plugin configuration to the new ID and rerun setup/doctor. Uninstall old packages yourself to avoid duplicate host loading.
- SQLite structure/namespaces, internal `hm_*` tables, `harness-checkpoint-v1` encoding, and `.hmpkg` remain unchanged by the rename.
  Continue with an explicit db_path or stop, back up, and move the complete data directory. Do not copy a live .sqlite alone and omit WAL.
  Move Chroma directories or configure their locations explicitly as well.
- The shared PG schema is `octop_memory`. Keep the existing DSN; after backup, shutdown, and confirmation the new schema is absent,
  have the database owner verify the old schema's actual name, then run
  `ALTER SCHEMA "<actual_previous_schema>" RENAME TO octop_memory;` after replacing the placeholder.
  If both schemas exist, reconcile/merge explicitly rather than overwriting one. The default DSN database name is also octop_memory,
  but renaming the database is unnecessary: set `OCTOP_MEMORY_DSN` to use the old database.
- Portable agent scans default to `~/.octop/agents/*/memory.sqlite` and `~/.octop-harness/*/memory.sqlite`.
  Old directories are no longer discovered automatically. Move them or pass
  `list_sources(extra_paths=[("agent", "/path/to/previous/*/memory.sqlite")])` explicitly from Python.
- New defaults do not discover old paths/schemas and installing the package does not move data automatically.
  Finish migration before starting the new installation.
