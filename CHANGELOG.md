# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### 变更

- PostgreSQL checkpoint 裁剪与 SQLite 共用数量、日期、待办和恢复依赖规则，支持预演与根图消息快照。裁剪在同一事务中删除旧链及其写入，只回收裁剪后已无引用的候选 blob 版本。
- SQLite checkpoint 裁剪支持由宿主提供 `messages` 的 DeltaChannel。在仅依赖消息增量的根图保留边界写入完整快照后，裁掉旧父链及写入，并保留窗口内的 checkpoint ID 和恢复状态。

## [1.0.4] - 2026-11-10

### 修复

- SQLite checkpoint 裁剪保留每个恢复点依赖的快照、写入和原有 ID。离线维护仍可通过调用方提供的名单保护指定快照。
- `Memory.store()` / `MemoryService.save()` 接受显式 `kind`，保存 Fact、Preference、Decision、Task，修复手动“记住”路径固定写成 Fact 的限制；调用方选择类型，不新增分类模型调用。省略时保留 Fact 默认值，拒绝非法类别与直接晋升 ConflictCandidate；保留原始证据、事务、用户来源与后台审核调度，不重新分类历史内容或增加 schema。

### 变更

- 每轮自动召回只检索 Atom（含可选向量 Atom），没有匹配时返回空，不再回退注入原始对话或页面摘要；主动工具搜索继续保留多源及 raw 策略。OpenClaw hybrid 预取与 Hermes 同步采用该规则，缓存隔离自动注入和多源搜索，不增加模型调用或删除历史记录。
- 文档与当前实现对齐：README 链接中英文 Wiki，维护者文档统一为英文，集成指南提供中英文两版。

## [1.0.3] - 2026-11-04

### 修复

- 候选提炼按单批事件数（默认 100）与字符预算（默认 20,000）分批调用模型，不再把整个 session 的事件塞进一次请求；此前长会话积压会让 prompt 超出上下文、以「长度截断 / 超时」失败（#25）。
- 「已提炼」游标写入 `{ns}_meta`（`extracted_ids/v1/<session>`），重启后从 meta 恢复，不再整段重跑（#25）。增量提取路径以 `strict=True` 持久化游标：游标写失败会让整个事务回滚、候选写入一并撤销，事件留在未提炼状态等待重试，而非被默默跳过；仅非严格路径的持久化失败才只告警。
- 旧手动文档审计记录缺少 `uncertain` 字段时，不再信任其已比较进度：只读状态将活跃事实列为待审，后续按现有批次上限重审一次；新格式的空未决集合保留有效进度，避免重复调用模型（#33）。
- EntityPage 再生成写回校验页面修订号，拒绝覆盖模型调用期间的用户编辑、事实 dirty 标记或更新的生成结果；迟到的生成失败不会重新污染已成功的新页。SQLite/PostgreSQL schema 2 自动补充修订列，SQLite 并发首次迁移使用短写事务；原有 summary_version 含义及不带条件的旧页接口调用保持兼容。
- Windows 宿主文件扫描忽略带根或驱动器的 allowlist glob，避免 rooted pattern 抛错；portable source 测试正确隔离 Windows 用户目录。
- **Hermes 适配器不再丢弃 `llm` / `extraction` 配置**：`_bridge_config()` 现在按桥解析器的同一语义把两个配置块转发进 `MemoryRuntimeConfig`，`$HERMES_HOME/octopmemory.json` 里的模型端点与抽取开关能真正抵达运行时；非对象值按缺省处理，坏配置不会挡住启动，`stats` 输出依旧不回显 `api_key`。此前 Hermes 宿主永远落到 `NoopLLMClient`，模型辅助抽取静默不可用（无 candidate/atom，也不报错）（#9）。
- 补齐 `MemoryService.review_new_memory(max_atoms=20)` 公共入口，修复宿主保存后和定时维护静默跳过手动文档审核的问题；保存仍不等待模型。新增真实 octop-harness 中间件与 SQLite/PostgreSQL 的跨包测试，并纳入 CI 和发布检查（TencentCloud/Octop#2358）。
- 手动文档审计中，无结论（`uncertain`）的比较仍推进游标，但不再被报告为 `complete`：未验证的 atom 按断言哈希记录，后续无待比对项的巡检仍返回 `uncertain`；新文档 revision 重新比对后可恢复 `complete`（TencentCloud/octop-memory#33）。
- 手动文档审计的未决集合改按当前活跃 atom 计算：已停用、已删除或断言已变的记录不再滞留，`remaining` 与状态读取同样只统计活跃 atom，因此停用一个 atom 后无需修改文档即可清除 `uncertain`，SQLite/PostgreSQL 同组回归（TencentCloud/octop-memory#33）。

### 变更

- **修正 Hermes 安装文档**：`docs/integrations.md` 与 provider README 收敛为一条可用路径——用 `hermes plugins install TencentCloud/octop-memory/plugins/hermes/octopmemory --ref <完整 40 位 SHA>` 从本仓子目录直接安装，无需独立插件仓库；并补充用 `hermes --print-runtime-command` 获取网关实际解释器。同时写明仓库未向 PyPI 发布独立的 `octop-memory-hermes` 插件包（按旧命令执行会 404），`installer.py` 仅为仓库内 E2E 工具（#10）。

## [1.0.2] - 2026-10-16

### 新增

- 手动 USER.md／MEMORY.md 与普通记忆核对：手动文档优先，明确冲突将旧 Atom 软停用、替代事实转候选并保留来源审计；重复仅在匹配的会话上下文抑制召回。`MemoryService` / Bridge 共用对账、状态接口；宿主可注册文档读取器在保存/晋升后检查。语义判断须引用原文，不确定、模型失败、过期文档和未完成批次显式报告。SQLite/PostgreSQL 同组验证，不增加 schema。

### 修复

- SQLite 每个 backend 复用一条连接，完整 SQL 操作与显式事务串行执行，避免多 agent 与常驻 worker 的连接数相乘；关闭等待正在进行的操作，独立失败写入回滚后再抛出异常（TencentCloud/Octop#2060）。
- PostgreSQL 按进程与连接配置共享有界池：业务最多 8 条，checkpoint/维护最多 4 条；操作归还连接、显式事务持有同一连接、最后引用关闭才销毁池。checkpoint 初始化串行复用，失败立即释放租约；修复 69 个活跃 agent 超出服务端连接上限的问题（TencentCloud/Octop#2060）。
- PostgreSQL 容量、排队超限、空闲回收、断连恢复、初始化失败及引用关闭加入真实服务回归；CI 与 release 测试均启动专用 PostgreSQL，显式配置测试 DSN 不可用时失败而非跳过。
- 系统运行报错仅按结构化来源标记隔离，不再自动采集、提炼或兜底召回。移除记忆意图、排障价值关键词及短消息意图例外，Agent 自行调用 memory_save；手工保存、人工批准按来源保护。存量预览/应用仅停用全部证据均带错误标记的自动记录，旧版报错文本、缺失或混合来源仅列入审核清单并保持原状态，保留原始证据与审计，SQLite/PostgreSQL 行为一致。

## [1.0.1] - 2026-10-09

### 修复

- 线程 ident 复用时关闭被丢弃的 SQLite 连接，短命线程不再累积 fd 并最终进入不可恢复的 disk I/O error（TencentCloud/Octop#1339）。
- PostgreSQL 空闲连接不再长期占用：连接建立时申请 `idle_session_timeout`（PostgreSQL 14+，旧版本自动降级）；本地按同一阈值主动关闭并重拨，不必等下一条语句。checkpointer 连接池 `min_size` 改为 0，空闲超过 `max_idle` 后关闭。常驻 agent 不再永久持有 1–2 个连接（TencentCloud/Octop#1795）。小时 checkpoint 裁剪在打开连接池之前会拒绝已空闲的 Postgres backend。
- checkpointer 连接池在取出连接时做存活性探测，丢弃服务器已关闭的连接并重建（TencentCloud/Octop#1172）。
- 提炼补全默认带 `max_tokens` 预算（2048）；调用方传入 `None` 视为未设置并回落到该默认值，避免推理模型占满上下文并超过读取超时（TencentCloud/Octop#1360）。
- 提高删除后与空闲维护的 SQLite 增量 vacuum 页数，避免 300 页的小时预算在大批删除后追不上。compact 统计计入 SQLite 主库、WAL 与 shm，Postgres 则报告数据库大小与回收字节。

### 变更

- 发版时同步检查多语言 README 版本。

## [1.0.0] - 2026-09-24

### 新增

- 首个 1.0.0 正式版本发布到 PyPI。

### 变更

- 对齐 Octop：引入 `develop` 集成分支策略；禁止直推 `main`/`develop`；发版后由 `sync-main-to-develop.yml` 同步；新增 `/publish` skill（发版同步 CHANGELOG / README）。



### Changed

- Rename the project and distribution to `octop-memory`, the Python package to `octop_memory`, and host plugin IDs to `octopmemory`.
- Rename CLI entry points, environment variables, default paths, and the PostgreSQL shared schema. Existing installations require explicit migration; see [the migration guide](docs/integrations.md#更名与已有安装迁移).
- Update host adapters, build metadata, examples, documentation, and the README banner. SQLite checkpoint and portable package formats remain unchanged.
- 最低 Python 版本提升到 3.12，与 Octop 基线对齐；核心、Hermes 包声明、开发环境与 CI 同步调整，Python 3.11 不再受支持。

### Documentation

- README banner 改用仓库相对路径，补充代码目录与记忆数据流；明确 Candidate 可自动处理、EntityPage 按触发再生成、Episode 为独立提炼流程。
- 清理个人工具配置、重复发布 skill 和私人数据库诊断脚本；合并基础/树示例为公共 API 临时库演示，保留合成 recall eval 并明确指标与覆盖限制。
- 合并 OpenClaw/Hermes 安装、配置和排错到 `docs/integrations.md`；贡献入口集中到 CONTRIBUTING，AI 规则集中到 AGENTS；保留精简、脱敏的 `docs/agent/` Harness 澄清、决策与交接流程。

### Added

- 新增宿主协调的 SQLite/WAL 在线 checkpoint 瘦身：一致备份、并发安全的批次去重和显式空间回收结果，不删历史或共享正文。`scripts/memory-slim --online AGENT` 调用运行中的新版 Octop 显示维护提示并协调暂停/恢复，不能用于旧 reader。

- 新增 `db slim FILE` 简单瘦身入口：默认只读预览；`--apply --offline` 自动备份、复用 checkpoint 重复字段并回收文件空间，不删除历史。提供本地源码启动器 `scripts/memory-slim`。

- SQLite checkpoint 默认复用重复的 `skills_metadata` / `workspace_file_contents`（工作区文件内容；底层字段仍为 DeepAgents 的 `memory_contents`）内容版本，兼容读取旧 inline 快照；`OCTOP_MEMORY_CHECKPOINT_DEDUP=0` 可关闭新增引用写入。新增 `db checkpoints` 只读预览、离线批量迁移、保守历史裁剪、压缩及 `--expand` 格式回退。新格式须使用兼容读端，不能直接回退旧二进制；详见 [Checkpoint 维护](CONTRIBUTING.md#checkpoint-维护)。

### Fixed

- SQLite checkpoint 解码器支持为独立只读连接创建专用实例与缓存，供宿主安全回填历史；离线裁剪逐条释放正文，避免内存按重复正文总量增长。补充 SQLite/真实 PostgreSQL 重开后审批恢复且不重复已完成步骤的对照测试。

- **Episode 时间统一为 UTC**：LLM 返回不带时区的 `occurred_at` 时不再把 naive `datetime` 写入存储；SQLite/PostgreSQL 写入和查询边界统一规范化为 UTC，SQLite 读取历史 naive Episode 时也会兼容转换。Dashboard 对 Episode 的两个排序入口增加 UTC 防御，避免混合 naive/aware 历史数据触发 `TypeError: can't compare offset-naive and offset-aware datetimes` 并让情绪日记页面显示为空。

## [0.9.9] - 2026-09-04

### 修复

- **PostgreSQL 读事务泄漏**：长驻 dashboard 连接会停在 `idle in transaction` 并持有 `AccessShareLock`，导致另一个实例初始化时在 FTS `DROP INDEX` 上无限等待。`PostgresMemoryBackend._cursor()` 现在在非显式事务、非 schema bootstrap 的情况下，为每次公开操作建立并结束一个短事务，读操作返回后连接恢复 `IDLE`；显式的 `backend.transaction()` 仍然可以跨多个操作保持原子性。
- **启动 DDL 卡锁**：schema bootstrap 完成 `SET search_path` 后显式提交，不再由 session 设置遗留隐式事务；DDL 设置 `lock_timeout=5s`，遇到异常长锁等待时快速报错；FTS 索引已是目标形态时先查 `pg_indexes` 并完全跳过扩展检查与 DDL。
- **Dashboard 采纳 `conflict` / `needs_review` 候选不再报错**：`promote_candidate` 只允许 `pending`，所以记忆沉淀「采纳」一条 `conflict` 记录会抛错，重跑 5-check worker 也只会再次把它停在 `conflict`。用户批准后现在强制写入 atom（SQLite 与 Postgres 共用这条路径）；已存在的矛盾 atom 被 supersede，召回只保留一侧。

### 变更

- 人工采纳路径达到发布门槛：修复 `PromotionWorker._approve_one()` 的 mypy 控制流类型错误——为分支间共用的 `entity_id` 显式声明 `str | None`，由 return/赋值分支收窄为 `str`，不使用 `cast` / `type: ignore` 掩盖。语义不变：exact duplicate 执行 merge，contradictory atom 被 supersede，candidate/journal 的决策来源记为 `user`。
- 补回 `chore: exclude node_modules from the published sdist`：hatchling 收集 sdist 时只认仓库根目录的 `.gitignore`，缺少 `node_modules/` 规则会把本地 `plugins/openclaw/octopmemory/node_modules`（约 373MB / 1.7 万文件）打进发布产物。
- 把此前滞留在 `## [Unreleased]`、实际已随 `0.9.8` 发布的条目归档到下方 `## [0.9.8]`，`## [Unreleased]` 恢复为空。

## [0.9.8] - 2026-09-02

### Added

- **Manual atom writes**: `Memory.create_atom()` creates a canonical atom (and optionally its entity) through the same RawEvent → Candidate → AtomCard → entity-branch leaf path as promotion. `Memory.replace_atom()` creates an append-only successor without fabricating a new RawEvent/Candidate, records the before/after correction in `user_edit`, and atomically supersedes only an active predecessor (ADR-010/ADR-034). Dashboard RPCs: `create_atom`, `replace_atom`.

- **Idle DB maintenance**: `run_idle_maintenance()` runs prune → GC → `nudge_vacuum` in one fail-soft pass for host idle timers. New SQLite files open with `auto_vacuum=INCREMENTAL` so nudge can reclaim without a prior compact.
- **Legacy SQLite bootstrap**: `maybe_bootstrap_incremental()` runs one idle/startup `compact_vacuum` to enable `INCREMENTAL` on existing NONE databases (≤200MB on short idle; no size cap on quiet startup / long idle). Skips with `insufficient_disk` when free space is below file+WAL + 64MB (retryable).
- **Idle WAL truncate**: `maybe_truncate_wal()` runs `wal_checkpoint(TRUNCATE)` when `-wal` is at least 2MB, only from a confirmed idle window.

### Changed

- **Checkpoint retention (ADR-029)**: default `keep_last` is **1** (latest **sealed** parent snapshot per thread). Before deleting ancestors, `prune_checkpoints` inlines delta-omitted `messages` onto a new root parent. If replay fails, that thread's parent ancestors are skipped. `drop_subgraph_streams=True` still deletes finished `tools:*` / subgraph streams after the parent is sealed. CLI unchanged.
- **Startup slim is cheaper and visible**: orphan-raw GC selects ids only (no full-row hydrate). Hosts can skip that pass via `run_idle_maintenance(include_orphan_raw=False)`. SQLite connections set `busy_timeout=30s`.
- **Journal retention (ADR-028)**: `run_gc` expires `extract_run` / `gc_*` / `page_regen*` / `consolidate` rows older than 14 days. Decision rows (promote/reject/…) are kept. Cleanup does not write journal. Extract passes overwrite `last_extract_run` in meta instead of appending journal; `stats_counts` exposes that summary.
- **`recall_for_prompt` auto-inject is stricter about raw**: default `raw_policy=fallback` keeps raw transcript only when no atom / page / episode candidate exists (no topping up to `limit`). Raw whose `session_id` or `thread_id` intersects the current session scope (`session_id` falling back to `thread_id`) is dropped, including sibling threads in the same session. `memory_search` still passes its configured `raw_policy` through, so `always` can mix raw with atoms. The eval baseline `recall_multi_source` is unchanged.
- **Postgres test suite is no longer skipped by default**: `psycopg` was not a dev dependency, so every test in `tests/test_postgres.py` skipped silently and the Postgres backend shipped unexercised. It is now in the `dev` group, and `mypy` tolerates it missing via an override rather than a per-import `type: ignore[import-not-found]` — the latter flips to "unused ignore" under `--strict` the moment the package *is* installed. The suite still skips (rather than fails) when no server is reachable.
- **Postgres checkpointer is no longer shipped unexercised**: `langgraph-checkpoint-postgres` (and its `psycopg_pool`) was missing from the `dev` group, so `_create_postgres_checkpointer` could only reach its `ImportError` branch and return `None`. The existing test monkeypatched the builder, so it proved the caching contract while never building a real pool. The dep is now declared and a suite builds a genuine Postgres-backed `Memory`: no pool before first use, a real `PostgresSaver` after it, and a checkpoint round-tripped through the database (skipping when no server is reachable). Making the real types visible also surfaced one mismatch — a bare `ConnectionPool` yields tuple rows while `PostgresSaver` requires dict rows; `row_factory=dict_row` already made that true at runtime, and the annotation now says so. The three `mypy --strict` errors this exposed on `main` are fixed too.
- **Idle GC is quieter**: the reason a GC pass is skipped is logged at `debug` instead of `info`, so an hourly idle maintenance timer no longer chats on every skip.

### Fixed

- **Lifecycle GC no longer corrupts a Postgres session**: the SQLite-only guard duck-typed on `_conn` / `_ns`, which `PostgresMemoryBackend` also exposes, so GC ran SQLite statements (`{ns}_atoms`, `?` placeholders) against Postgres. The resulting `UndefinedTable` aborted the shared transaction and every later write failed with `InFailedSqlTransaction`, silently disabling memory capture and extract for the rest of the process. `run_gc` now refuses on backend type up front, before any statement reaches the connection, and returns empty stats (rather than raising) so an hourly maintenance timer keeps ticking.
- **`memory export` no longer poisons a PostgreSQL connection**: `_iter_active_entities` duck-typed the same way, so it ran the SQLite statement (`{ns}_thread_active_entities`) against psycopg. The `UndefinedTable` was swallowed by a bare `except`, with no rollback, leaving the connection aborted — so **every later memory write in that process failed with `InFailedSqlTransaction`**. The exporter now dispatches on backend type. `thread_active_entities` is also genuinely exported on Postgres now instead of silently dropped from the dump.
- **A dropped connection is redialled instead of staying dead**: `_reset_if_aborted` only revives a *live* connection whose transaction is poisoned. A server restart, a network blip or an idle timeout is a different failure — `closed` is 1 and `transaction_status` reports `UNKNOWN`, where `rollback()` is a no-op — and nothing reopened the connection, so memory writes failed with `OperationalError: the connection is closed` until the process restarted. `_cursor()` now reconnects first and re-applies `search_path` (a session setting, not a database one; without it every unqualified table name resolves against `public`). Three exemptions: after an explicit `close()`, inside a `transaction()` block whose earlier writes died with the old connection, and during schema bootstrap. Redial attempts are spaced by a 2s cooldown so a genuinely down server does not cost every call a connect timeout.
- **A failed statement no longer disables Postgres memory for the whole process**: psycopg refuses *every* command — reads included — on a connection left in `INERROR`, but only `transaction()` and `save_raw_batch` recovered, leaving 27 of 29 write methods and all reads unable to come back. Every data-access method now opens its cursor through a `_cursor()` chokepoint that clears an aborted transaction first. Schema bootstrap and explicit `transaction()` blocks stay exempt: they own their own rollback, and an automatic one would undo half-built DDL or hide a failure the caller must see.

  Note for PostgreSQL installs: `run_gc` still no-ops and `prune_checkpoints` still refuses (both SQLite-only, D46-C / M6), so retention has to be handled outside octop-memory for now.

## [0.9.1] - 2026-07-13

### Fixed

- **OpenClaw npm plugin**: bump package version to 0.9.1 in sync with Python package.

## [0.9.0] - 2026-07-13

### Added

- **portable adopt/doctor**: auto-resolve openclaw namespace from `~/.openclaw/openclaw.json`
  (`plugins.entries.octopmemory.config.namespace`), so migrated memory lands in the
  namespace the plugin actually opens — no more silent mismatch.
- **OpenClaw TS plugin**: new `/octopmemory` chat slash command (`status / search / show / reindex`)
  dispatched via `api.registerCommand` (same surface as memory-core's `/dreaming`).
- **OpenClaw TS plugin**: `sdk-shim.ts` extended with `ChatCommandContext`, `ChatCommandReply`,
  `RegisterChatCommandOptions` interfaces; `registerCommand?` added to `OpenClawPluginApi`
  (feature-tested at runtime for older hosts).

### Fixed

- **OpenClaw setup**: always write `hooks.allowConversationAccess: true` into the plugin entry.
  OpenClaw ≥2026.4.29 gates `agent_end` / `llm_input` / `llm_output` hooks behind this explicit
  opt-in; without it, auto-capture silently stores nothing while doctor/status look healthy.
- **OpenClaw setup**: always write `namespace` explicitly (`openclaw__default`) so the plugin
  never derives a divergent name at runtime.
- **OpenClaw doctor**: check `hooks.allowConversationAccess` and exit 1 with a clear remediation
  hint when the field is absent.
- **OpenClaw doctor**: also search npm-managed plugin path
  (`~/.openclaw/npm/projects/*/node_modules/@octop-memory/openclaw`) so `openclaw plugins install`
  layout is accepted.
- **OpenClaw TS plugin**: fix npm package name (`@octopmemory/openclaw-plugin` →
  `@octop-memory/openclaw`).
- **OpenClaw TS plugin**: remove invalid `kind: runtime-slash` from `commandAliases` entry
  (caused plugin load warning on newer hosts).

## [0.2.0] - 2026-06-18

### Added

#### L0–L4 Memory Pipeline
- **L0 Raw Events** — `RawEvent` dataclass + SQLite table with FTS5 index. Immutable, append-only source of truth for all higher layers.
- **Async writer** (`io/`) — background thread with batch commit (50 events / 200 ms), 3-retry exponential backoff, dead-letter table, WAL-mode isolation.
- **L1 Candidates** — `CandidateExtractor` (prompt v2.1, anti-dilution rules, JSON parser with referential-integrity validation). `extract_session()` glue function.
- **L1→L2 Promotion worker** — 5-check pipeline (value → evidence → entity → duplicate → conflict). Pure-rule path + optional LLM escalation hook for grey-zone disambiguation. 7-day fallback rules. `PromotionResult` telemetry.
- **L2 AtomCard** — append-only structured facts with `superseded_by` deprecation chain. FTS5 index over `assertion + verbatim_quote + search_terms`.
- **L3 Entity + Alias** — entity anchor for atoms; alias table for name normalization and co-reference resolution.
- **L3 EntityPage** — long-form markdown summary per entity; `dirty` flag + async cron regen; `headline` hot field; `## My Notes` section preserved verbatim on regen.
- **L4 Journal** — append-only audit log of every promote / merge / conflict / deprecate / gc action.

#### Recall Pipeline (M4)
- **`recall_for_prompt_v2`** — 8-stage pipeline: query parser → router → multi-source gather → rerank (5-factor BM25+importance+confidence+recency+layer\_prior) → diversify → suppress → token budget → cache.
- **Active-entity LRU stack** — per-thread co-reference resolution ("那个项目") via `ActiveEntity` table and `thread_state` helpers.
- **Source layers**: `atom` (primary), `page_headline`, `tree` (MemoryNode), `raw` (fallback). Router selects based on query shape; time-bounded queries exclude tree.
- **`recall_for_prompt`** — simpler M2.9 baseline (atom + raw, no rerank).

#### MemoryNode Tree ↔ Pipeline Bridge
- **Write-side**: `PromotionWorker._sync_atom_to_tree()` — on every successful promote, auto-mirrors the AtomCard as a MemoryNode leaf under an entity branch node. Nodes tagged `metadata["mirror"]="auto"` to distinguish from hand-authored entries. Best-effort (tree failure does not abort promotion).
- **Read-side**: `"tree"` added as a recall source in `gather_candidates()` and `recall_for_prompt_v2`. Layer prior = 0.70 (between atom and raw). Token budget bucket = 30% of total.

#### Host Integrations
- **OpenClaw plugin** (`plugins/openclaw/octopmemory/`) — TypeScript shell spawning the Python bridge; registers `registerMemoryCapability` (D3 path); `setup / doctor / uninstall / print-config` CLI subcommands.
- **Hermes adapter** (`plugins/hermes/octopmemory/`) — Python `MemoryProvider` adapter; `install / doctor` CLI subcommands; runtime policy config.
- **Python bridge** (`bridge/`) — JSON-RPC over stdio server; `host_files` 30-second poll watcher; path projection for cross-OS environments.

#### Migration & Lifecycle (M5)
- **Export / Import** — streaming JSONL dump with optional gzip; full namespace round-trip (raw\_events → candidates → atoms → entities → aliases → entity\_pages → journal → thread\_active\_entities).
- **Rename / Preview** — safe namespace rename with dry-run preview.
- **Backfill** — rebuild L1–L4 from L0 raw events for schema migrations.
- **GC** — `lifecycle/gc.py`: prune rejected candidates, deprecated atoms, orphan raw events. Configurable age thresholds.

#### CLI Commands
New command groups: `raw`, `candidate`, `atom`, `entity`, `journal`, `page`, `recall`, `thread`, `export`, `import`, `migrate`, `backfill`, `gc`, `openclaw`, `hermes`.

#### Dev / Testing
- `pytest-asyncio>=0.23` added to dev extras; `asyncio_mode = "auto"` configured — all 6 async checkpointer tests now pass.
- 763 tests total (13 skipped for missing PostgreSQL environment).

### Changed
- **BREAKING**: `save_conversation` is now an upsert keyed on `thread_id`. Subsequent calls for the same `thread_id` append messages rather than replacing the record. Existing databases need to be recreated.
- `ConversationRecord` and `ConversationSummary` gained a `title` field; `ConversationSummary` exposes `ended_at` and `message_count`.
- `MemoryNode.metadata` is now a free-form dict persisted as JSON (not indexed by FTS); upper layers use it to store `entity_id`, `atom_id`, `mirror` references.
- Router default sources changed from `("atom", "raw")` to `("atom", "tree", "raw")` for non-time-bounded queries.

### Fixed
- `migration/export.py` — mypy strict `[assignment]` error caused by reusing the `row` loop variable across branches of different types; renamed each branch variable to a type-specific name.
- `recall/multi_source.py` — `_gather_atoms`, `_per_token_atom_search`, `_per_token_raw_search`, `_per_token_tree_search` now have fully-typed return annotations (`list[AtomCard]`, `list[RawEvent]`, `list[MemoryNode]`).

## [0.1.1] - 2026-05-24

### Added
- `Memory` class — unified interface for memory storage and recall.
- `MemoryBackend` protocol — pluggable backend abstraction.
- `SqliteMemoryBackend` — default backend with FTS5 full-text search.
- Data types: `MemoryNode`, `ConversationRecord`, `Message`, `SearchResult`, `ConversationSummary`.
- Memory tree: hierarchical root → branch → leaf structure.
- Namespace isolation: multiple agents share one database safely.
- Backend factory: resolve `"sqlite"` / `"postgres"` / `"qdrant"` strings to implementations.

[Unreleased]: https://github.com/TencentCloud/octop-memory/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/TencentCloud/octop-memory/compare/v0.1.3...v0.2.0
