# PROJECT_MAP.md

当前模块、调用链和数据契约。设计理由见 [DECISIONS](DECISIONS.md)，限制见
[KNOWN_RISKS](KNOWN_RISKS.md)，按范围验证见 [TEST_MATRIX](TEST_MATRIX.md)。

## 入口与分层

发行包/CLI 为 `octop-memory`，Python 包为 `octop_memory`，宿主插件 ID 为 `octopmemory`，
bridge 为 `octopmemory-bridge`。配置使用 `OCTOP_MEMORY_*` / `OCTOPMEMORY_*` 环境变量；
Python 公共入口为 `from octop_memory import Memory, MemoryService`。

```text
Python host → MemoryService ─────────┐
Hermes → in-process Bridge ──────────┤
OpenClaw → stdio Bridge ─────────────┴→ MemoryRuntime → pipeline → Memory
CLI → application / operations ──────────────────────────────────┘
                                                     ├→ SQLite / PostgreSQL
                                                     └→ optional VectorIndex
```

`MemoryService.recall()` 直接使用 recall pipeline，部分 CLI 直接调用 Memory/operations。
依赖约束：pipeline/storage/domain 不依赖 adapters，storage 不依赖 pipeline，service 不依赖 bridge；
backend-specific SQL 留在 storage。源码 dashboard 直接访问 SQLite，是已有例外。
这些约束由 `tests/test_architecture_boundaries.py` 验证。

### 模块地图

下表路径相对 `src/octop_memory/`。

| 入口 | 职责 |
|---|---|
| `types.py`、`domain/` | 数据结构、alias、UTC 与来源判定规则 |
| `core.py` | `Memory` API、backend factory、可选 LangGraph saver |
| `service.py`、`application/runtime.py` | typed host API；共享 capture/search/get/extract/promote 编排 |
| `application/config.py` | runtime policy 默认值与配置转换；OpenClaw 在 TS 插件侧展开 profile，Python Runtime 不按 profile 标签切换策略 |
| `application/host_files.py`、`path_projection.py` | host Markdown 索引与虚拟路径 |
| `application/checkpoint_maintenance.py`、`storage/backends/sqlite_checkpoint*.py` | SQLite checkpoint 内容复用、离线/在线维护与读取兼容 |
| `storage/backends/__init__.py`、`sqlite.py`、`postgres.py`、`postgres_pool.py` | `MemoryBackend` contract、SQL、FTS、事务、namespace 与连接池 |
| `storage/driver_errors.py` | 各入口允许降级捕获的异常元组 |
| `storage/vector/`、`ports/llm/` | 可选向量索引、embeddings 与 `LLMClient` |
| `pipeline/` | extractor、promotion、page、episode、recall、lifecycle 各业务流水线 |
| `adapters/bridge/`、`adapters/cli/`、`adapters/dashboard/` | JSON-RPC、Click CLI、源码 dashboard |
| `operations/migration/`、`operations/migration/portable/` | JSONL export/import、namespace migration、backfill、portable package |
| `operations/retention.py` | 存量报错治理、手动文档语义核对与审计元数据 |

根目录 `plugins/<host>/` 是外部宿主适配，目前包括 OpenClaw、Hermes；安装与契约见
[集成指南](../integrations.md)。`tests/` 保存行为测试，`evals/recall/` 是合成召回评测；
`examples/basic_usage.py` 通过公共 API 演示临时库的读写、树与 prompt recall。
`scripts/` 提供构建、部署、真实宿主 E2E 和维护启动器，不能以部署脚本运行代替单测。

### 公共 API 的分工

| 使用场景 | 入口与返回 |
|---|---|
| 手工保存确定事实 | `Memory.store()` / `MemoryService.save()` → `MemoryNode` leaf |
| 基础事实检索 | `Memory.recall()` → atom 对应的 tree leaf |
| 搜索归档会话消息 | `Memory.search()` → conversation message 搜索结果 |
| prompt 上下文召回 | `MemoryService.recall()` / `recall_for_prompt()` → `RecallResult.snippets`、`rendered` |
| 宿主搜索/读取工具 | `MemoryService.search()` / `get()` → hits / path excerpt；Bridge 对应 `memory_search` / `memory_get` |
| 捕获与提炼会话 | `MemoryService.capture_turn()` / `extract()`；无 LLM 时仍可捕获和 FTS 检索，提炼返回 `failure_reason` |
| 手动文档审核 | `reconcile_manual_documents()` 显式核对；`review_new_memory()` 供宿主后台 worker 调用 |

`MemoryService` 的 config 供 Runtime 的工具搜索、capture、privacy、extraction 等使用。
`MemoryService.recall()` 直接调用 prompt recall，不把 Runtime 的 recall 配置整体转发给该函数；
不能假定工具搜索和 prompt 注入使用完全相同的配置或文件来源。

## 跨仓协作地图

三个仓库分别是 [octop-memory](https://github.com/TencentCloud/octop-memory)、
[octop-harness](https://github.com/TencentCloud/octop-harness)、[Octop](https://github.com/TencentCloud/Octop)。
仓库 checkout 目录名不构成接口；下列路径各自相对所属仓库，不要求开发者按固定目录并排 clone。
本库以运行时库、CLI 和 bridge 提供可复用记忆能力；Harness 是 Agent 运行库，Octop 是应用宿主。
三者独立发布，不能用修改本地库源码推断已安装的应用自动取得修复。

### 两条接入链

```text
聊天执行：
Octop AgentManager._build_harness_config()
  → HarnessAgentConfig → HarnessAgent / octop_harness.memory.MemoryRuntime
  → MemoryMiddleware / memory tools → MemoryService
  → octop_memory MemoryRuntime 或 prompt pipeline → Memory → backend

记忆管理：
Octop dashboard/src/pages/Agent/Memory/ → dashboard/src/api/modules/memoryDashboard.ts
  → api/routers/memory.py、agent_files.py → api/common/memory_client.py / AgentManager
  → Bridge / MemoryService → 本库 application/dashboard_data.py、Runtime、core/pipeline
```

`octop_memory.application.runtime.MemoryRuntime` 组织宿主无关的记忆业务；
`octop_harness.memory.runtime.MemoryRuntime` 创建/复用库实例、注入模型和文档 provider，
绑定工具/中间件并管理资源。这两个类不能互相替代。
Octop 的管理接口可以在 Agent 停止时打开库或临时装配 Harness Runtime，
因此修复不能只验证聊天中的 live Agent 路径。

### 按职责定位文件

表内 memory/Harness 的 Python 路径分别相对 `src/octop_memory/`、`src/octop_harness/`；
Octop 的 Python 路径相对 `src/octop/`，`dashboard/` 路径从 Octop 仓库根目录起。

| 职责 | 仓库 | 代码入口 |
|---|---|---|
| 通用捕获过滤、提炼编排与进度 | memory | `application/runtime.py`、`domain/retention.py`、`pipeline/extractor/` |
| 事实、冲突候选、排序、页面和事件的业务语义 | memory | `core.py`、`pipeline/promotion/`、`pipeline/recall/`、`pipeline/page/`、`pipeline/episode/`、`operations/retention.py` |
| 表结构、事务、FTS、连接池、向量索引、checkpoint 编码与迁移 | memory | `storage/`、`application/checkpoint_maintenance.py`、`operations/migration/` |
| 通用 LLM 协议与独立 bridge 的模型客户端 | memory | `ports/llm/`；不承担 Octop Agent 的 provider/凭据选择 |
| 记忆管理协议、结果和虚拟路径 | memory | `application/dashboard_data.py`、`application/path_projection.py`、`adapters/bridge/handlers.py`；Octop 负责 HTTP 和前端消费 |
| Agent 级开关、字段序列化、Runtime 装配和实例复用 | Harness | `config/__init__.py`、`memory/runtime.py`、`memory/store.py` |
| 每轮采集、召回注入/回放、提炼计时和后台维护 | Harness | `middleware/memory.py`、`middleware/memory_recall.py`；图装配、会话结束与 checkpointer 选择在 `agent.py` |
| 模型看到的 memory_search/get/save 工具及返回适配 | Harness | `builtin/tools/memory_tools.py`；工具内部委托 Service，不另写记忆算法 |
| Octop Agent 辅助模型选择、provider 调用、重试与超时 | Harness | `memory/llm_client.py`；通用接口和独立 bridge 的模型客户端归 memory |
| USER.md/MEMORY.md 的会话快照、运行时提示与 Agent 写保护 | Harness | `agent.py`、`memory/policy.py`、`middleware/filesystem_guard.py`、`backends/workspace.py` |
| 产品配置保存、能力探测、namespace/DB 路径与 Harness 配置映射 | Octop | `infra/agents/manager.py` 的 `_memory_extract_settings` / `_build_harness_config`，以及 `infra/agents/memory/backend.py` |
| HTTP、所有者权限、手动文档编辑、候选采纳时附当前文档 | Octop | `api/routers/memory.py`、`api/routers/agent_files.py`、`api/common/memory_client.py`、`infra/agents/manager.py` |
| 记忆页、设置、冲突候选、用户档案、存储与迁移交互 | Octop | `dashboard/src/pages/Agent/Memory/`、`dashboard/src/api/modules/memoryDashboard.ts`、`dashboard/src/api/modules/memoryPortable.ts` |
| Agent 停机/恢复、备份恢复、在线暂停、线程/Agent 删除、存储统计 | Octop | `infra/agents/memory/`、`infra/agents/manager.py`、`api/routers/memory_storage.py`、`api/routers/memory_portable.py` |
| 从 Episode 选择提醒、主动关怀和 cron 产品调度 | Octop | `infra/proactive/`、`infra/cron/`；不等于 memory 的通用 prompt recall 排序 |

### 典型联动判断

- **搜索结果不准确**：先查 memory 的候选来源/过滤/排序；若结果正确却没有进入下一轮提示，查 Harness 注入与快照回放；若 API 返回正确但页面显示错误，查 Octop。不要把三类问题都归为“改召回算法”。
- **调整提炼模型或预算**：memory 定义算法所需的调用协议和预算；Harness 负责选模型、实际调用及配置转发；Octop 负责让用户填写、持久化并映射参数。新增设置要逐层核对，不能以 UI 保存成功代替执行端生效。
- **手动档案与普通事实冲突**：memory 实现原文核对、软停用和候选/审计；Harness 负责 provider、非阻塞 scheduler、会话快照和 Agent 文件权限；Octop 负责用户文件保存、所有者权限、显式核对和候选审核交互。文件编辑、模型治理和当前会话快照是不同环节。
- **数据库或 checkpoint 格式变更**：算法/迁移属于 memory，选择和复用 saver 属于 Harness，暂停运行、恢复和用户备份流程属于 Octop；同时核对 Octop 的 `infra/history/` 读取与恢复路径。

Octop 已有 `infra/agents/memory/sqlite_storage.py`、`postgres_storage.py`、`thread_cleanup.py`
等直接访问记忆表的管理实现。这些是 schema 变化时必须审计的既有消费者，不能从“算法归 memory”
推断它们不会受影响。新增通用 SQL/数据语义优先归库侧 API；产品所有权、运行锁和停机协调留在宿主。
本仓 `adapters/dashboard/` 是源码诊断工具，Octop 用户记忆页的改动应在 Octop 的 `dashboard/` 完成。

## 数据与写入

| 数据 | 用途 |
|---|---|
| L0 `RawEvent` | 原始来源证据，受 capture/privacy 过滤 |
| L1 `Candidate` | 提炼或手工构造的候选，保留证据与 review 状态 |
| L2 `AtomCard` | canonical fact、证据引用、置信度与替代关系 |
| L2.5 `Episode` / digest | 事件记忆与周期摘要 |
| L3 `Entity` / `EntityPage` | 实体锚点、整合页面与 headline |
| L4 journal | 业务决策与部分 lifecycle 记录，不是所有 SQL mutation 日志 |

```text
capture → RawEvent → extraction → Candidate → promotion → AtomCard → dirty page → regeneration
              └────事件提炼────→ Episode                     └────atom_id────→ tree leaf
```

Candidate 可自动晋升、合并、丢弃或进入 conflict/needs_review，不等同于全量人工审核队列。
`Memory.store()` 无需 LLM，建立手工 RawEvent/Candidate/AtomCard 及 leaf 引用。
`root → branch → leaf` 是组织树，leaf 通过 `atom_id` 投影事实正文，不复制第二份事实。
人工 correction 创建 successor，active-only supersede 旧 atom 并写 `user_edit` journal，
不伪造新的对话证据。绝对事件时间统一为 aware UTC。

### 提炼与增量进度

`pipeline/extractor/__init__.py` 的 `CandidateExtractor` 无持久化副作用。
默认按 **100 条事件**及 **20,000 字符估算预算**拆批；估算为正文长度加每条 200 字符开销，
不是序列化 prompt 的严格字符/token 上限。单条超预算事件仍独占一批，不截断丢弃。
上限可经 extractor 构造参数调整，当前不是宿主 JSON 的配置字段。
各批合并 candidates、warnings、cap_warning 和 llm_calls；一批失败即停止，整体带 `failure_reason`。

`application/runtime.py` 单次读取该 session 最多 10,000 条 raw，再排除已提炼 ID。
增量进度保存于 backend meta 的 `extracted_ids/v1/<session>`；内存最多缓存 16 个 session，
LRU 淘汰或重建 Runtime 后从 meta 恢复，不以进程内状态作为唯一进度。
所有模型批次成功后，候选与游标在同一 backend 事务内提交，之后才更新内存集合。
候选或严格游标写入失败则一起回滚；模型批次失败也不推进游标，后续重试会重跑本轮输入，
不逐批持久化已经成功的部分结果。

上述持久游标和事务保证属于 Runtime/Service/Bridge 的 `extract` 路径。
低层 `extract_session(..., persist=True)` 只把 extractor 返回的候选写入，不维护增量游标，
也不代调用者拒绝带 `failure_reason` 的部分候选；不能将两个入口的失败语义混用。

promotion、页面再生成与 Episode 提炼在候选/游标提交之后运行，不属于该事务。
因此游标表示候选提炼已提交，不表示后续所有阶段成功；pending 候选可通过 promote 入口续处理。
Episode 使用同批 raw、独立 prompt，失败是非致命的增强层失败。
`extract_run` 仅覆盖 `last_extract_run` meta 摘要，不逐次追加 journal；历史 heartbeat 配置已不控制写入。

### 手动文档治理

`MemoryService` / Bridge → `MemoryRuntime` → `operations.retention` 复用同一治理逻辑。
宿主提供当前 USER.md/MEMORY.md 与可选 freshness callback；文档读写和聊天快照加载仍由宿主负责。
`MemoryService.save()` 落库后通知 `set_manual_review_scheduler()` 注册的非阻塞回调，不同步对账。
宿主 worker / 维护拍调用同步的 `review_new_memory(max_atoms=20)`，从 provider 读取当前文档；
未注册 provider 或文档为空返回 `None`。该方法会调用模型，必须在用户回合外运行。
显式 `reconcile_manual_documents()` 默认最多 100 条 Atom，文档总长上限 100,000 字符。

只有明确主体/范围、高置信互斥事实且原文引用有效时，才在事务内软停用旧 Atom、
建立替代 Candidate 和 Journal；替代事实需审核后采纳。不确定或模型失败不强制停用，partial 支持续查。
`manual_documents/v1` 保存版本、已比较 assertion hash、未决项与审计快照；
`manual_documents/recall/v1` 保存小型召回抑制清单，完整文档不批量导入 Atom/FTS。

审计 `uncertain` 字典持久化仍无结论的活跃 assertion hash，已比较不等于已验证。
旧记录缺少该字典时，不信任 checked 进度：状态读取只计算待审项，不修改元数据；
下一次对账有界重审，并在新格式写入后续查。显式空字典是有效已决状态，不重复重审。
召回按实际 session 的 `manual_context` 去重，并屏蔽冲突旧 Atom 的 raw fallback 和待重建页面。
采纳校验最后对账版本；宿主还应提供当前 `manual_documents`，拦截未重新对账的文件变化。

### 运行报错隔离

在线 capture、Candidate/Episode extractor、promotion 与 raw prompt recall 只按结构化来源排除报错，
不使用记忆意图/排障价值关键词；Agent 自行决定调用 `memory_save`。
后台采集保留长度等过滤策略，手工保存不受短消息长度限制。
自定义宿主合成错误回复时应标记 `payload.octop_runtime_error=true`。
octop-harness 的 `on_failure="error"`、checkpoint task error 和团队轮次排除由宿主实现，
不是 memory 自动识别所有错误文本的保证。

`audit_runtime_errors()` 默认预览；显式 apply 也仅在全部证据带错误标记时停用自动 Atom、
拒绝未决 Candidate，人工保存/批准/编辑按来源保护。旧固定报错格式仅生成审核提示；
缺失/混合来源返回 `review_atom_ids` / `review_candidate_ids`，原状态和 raw 证据不改。

### 页面生成

页面按 dirty 标记触发生成，不承诺每次写入即时刷新；生成保护 `## My Notes`。
`summary_version` 随正文生成/编辑增加，storage 管理的 `mutation_version` 在正文 upsert、
dirty 失效、人工编辑及生成成功时推进。regenerator 先确保 dirty stub 存在，再读取修订和 Atom，
模型调用期间不持有数据库事务。

成功与失败写回都在 backend 的同条条件 UPDATE 中核对修订。旧快照返回 `stale_snapshot`
或保留原失败原因、记录失败 journal，不覆盖较新正文、dirty 或失败次数；
当前修订的失败只更新重试状态，不推进 mutation_version。条件写回失败不再无条件 upsert。
事实变化沿用既有 dirty 调用链，绕过它的低层写入不获得额外治理。
SQLite/PG schema 2 自动补默认零修订列：SQLite 在 `BEGIN IMMEDIATE` 内检查/加列；
PG 在初始化 advisory lock 内查列，仅缺列时 `ALTER`，避免 no-op DDL 的独占锁。
普通页 apply 保留旧 backend 签名；显式版本条件要求自定义 backend 实现条件参数与原子校验。

## 召回与读取

主召回流程：cache → parse → route → gather → raw policy → rerank → diversify → suppress → budget → render。
默认路由 atom/raw；entity anchor 加 page_headline，有 index 加 vector，tree 不是独立 source。
默认 raw fallback 在命中提炼结果时排除 raw，不靠 raw 补满数量；
传入当前 session/thread 时排除本会话 raw。工具搜索可按 policy 合并 host Markdown；
`memory_get` 按搜索返回的虚拟路径读取，不应由宿主猜测 ID。

## 存储与连接

SQLite 使用 namespace 表名前缀；PostgreSQL 使用共享 `octop_memory` schema 与 namespace-first keys。
Chroma/Qdrant 是可选 index，不替代关系数据和事务；当前缺真实向量后端集成验证。
LangGraph checkpoint 是可选执行态持久化，`checkpoint_ns` 与 memory namespace 不同。

SQLite 每 backend 一条连接，完整 SQL、结果读取、显式事务、维护与 close 共用可重入锁；
单次失败写入在非显式事务中回滚，close 等待操作结束后关闭且不重开。
不同 backend 锁互不影响，checkpoint 使用独立连接、不与业务 SQL 共用事务。见 ADR-039。

PostgreSQL 按进程、规范化 DSN 和驱动选项共享业务池（8）与 checkpoint/维护池（4），
namespace 不参与池标识。普通 cursor 借用短事务连接，显式事务持有同一连接及 backend 锁；
事务中断不重拨后续写。每池 `min_size=0`、`max_idle=600`，checkout 探测存活，
最多排队 64 个请求、等待 30 秒。租约 close 幂等，最后引用释放才关闭池。
checkpoint DDL 按池串行初始化一次，失败释放租约并允许重试；维护共用 autocommit 池。见 ADR-040。

## 宿主文件与迁移

`HostFilesIndex` 是独立 SQLite 文件索引，即使 memory 使用 PG，也不变为 PG 索引。
启用后扫描 MEMORY.md、USER.md、DREAMS.md、符合日期命名规则的 `memory/*.md`，
以及默认 `topics/*.md` / `projects/*.md` 或显式 allowlist；不扫描整个 workspace。
只接受安全相对 Markdown glob，拒绝锚点、隐藏路径与 `..`；读取使用索引快照而非直接重读文件。
该索引与手动文档语义治理是两个机制，扫描到文件不表示已完成语义审核。

portable 的 Agent 默认发现目录为 `~/.octop/agents/*/memory.sqlite` 与 `~/.octop-harness/*/memory.sqlite`；
其它位置由 `list_sources(extra_paths=...)` 显式加入，不自动搬移数据。
JSONL export/import 与 `.hmpkg` portable 负责跨库/宿主迁移；checkpoint 格式维护只支持 SQLite，
步骤见 [CONTRIBUTING](../../CONTRIBUTING.md#checkpoint-维护)。旧命名安装的配置/目录/schema 迁移见
[集成指南](../integrations.md#更名与已有安装迁移)。
