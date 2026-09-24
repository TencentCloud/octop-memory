# PROJECT_MAP.md

当前模块、调用链和数据契约。架构变化时更新本文；设计原因见 [DECISIONS.md](DECISIONS.md)。

发行包/CLI 为 `octop-memory`，宿主插件 ID 为 `octopmemory`，bridge 为 `octopmemory-bridge`。
配置环境变量使用 `OCTOP_MEMORY_*` / `OCTOPMEMORY_*`；默认目录遵循新名称。
Python 入口为 `from octop_memory import Memory, MemoryService`。

```text
Python host → MemoryService ─────────┐
Hermes → in-process Bridge ──────────┤
OpenClaw → stdio Bridge ─────────────┴→ MemoryRuntime → pipeline → Memory
CLI → application / operations ──────────────────────────────────┘
                                                     ├→ SQLite / PostgreSQL
                                                     └→ optional VectorIndex
```

`MemoryService.recall()` 直接使用 recall pipeline，部分 CLI 直接调用 Memory/operations。
现有 dashboard 直接访问 SQLite；它是源码工具，不是新 adapter 的分层模板。

| 阅读入口（相对 `src/octop_memory/`） | 职责 |
|---|---|
| `types.py`、`domain/` | 数据结构、alias 与 UTC 规则 |
| `core.py` | `Memory` API、backend factory、可选 LangGraph saver |
| `service.py`、`application/runtime.py` | typed host API 和共享 capture/search/get/extract/promote 编排 |
| `application/config.py`、`host_files.py`、`path_projection.py` | policy、host Markdown 索引和 virtual path |
| `storage/backends/__init__.py`、`sqlite.py`、`postgres.py`、`storage/driver_errors.py` | `MemoryBackend` contract、SQL、FTS、事务、namespace；降级捕获的异常元组 |
| `storage/vector/`、`ports/llm/` | 可选向量索引、embeddings 和 `LLMClient` |
| `pipeline/` | extractor、promotion、page、episode、recall、lifecycle |
| `adapters/` | JSON-RPC bridge、Click CLI、源码 dashboard |
| `operations/migration/` | export/import、portable package、namespace migration、backfill |

portable 的 Agent 默认发现目录为 `~/.octop/agents/*/memory.sqlite` 与 `~/.octop-harness/*/memory.sqlite`；
其它位置由 `list_sources(extra_paths=...)` 显式加入，不自动搬移数据。

外部 Agent 适配位于根目录 `plugins/<host>/`，当前包括 OpenClaw、Hermes；验证位于 `tests/`、`evals/`，示例位于 `examples/`。
`examples/basic_usage.py` 合并基础读写、树和 prompt recall，仅通过公共 API 写入临时库；
`evals/recall/run.py` 比较 raw FTS、atom/raw 和完整 recall，使用合成 corpus，不是正式质量基准。
`scripts/` 提供插件构建、部署、真实宿主验收及本地维护启动器；个人诊断和工具配置不纳入公共源码。
根目录 `.editorconfig` 固定 4 空格、LF、UTF-8，Python 行宽 120，与 `ruff` 一致。
pipeline/storage/domain 不依赖 adapters，storage 不依赖 pipeline，service 不依赖 bridge。
`tests/test_architecture_boundaries.py` 固化这些约束；SQL 留在 storage 内。

### 数据与写入

| 数据 | 用途 |
|---|---|
| L0 `RawEvent` | 原始证据，受 capture/privacy 过滤 |
| L1 `Candidate` | 抽取或手工构造的候选及 review 状态 |
| L2 `AtomCard` | canonical fact、证据引用、置信度、替代关系 |
| L2.5 `Episode` / digest | 事件与周期摘要 |
| L3 `Entity` / `EntityPage` | 实体锚点、整合页面与 headline |
| L4 journal | 明确的业务决策与 lifecycle 记录，不是全部 SQL mutation 日志 |

`capture → RawEvent → extraction → Candidate → promotion → AtomCard/Entity → dirty page → regeneration`。
Candidate 可自动晋升、合并、丢弃或进入 conflict/needs_review，不能等同于全量人工审核队列。
Episode 从 RawEvent 独立提炼；页面由 dirty 标记和 regeneration 触发更新，不承诺每次写入即时刷新。
自动提炼使用注入的 `LLMClient`；无模型时 capture 和 FTS 可用，提炼返回不可用原因。

`Memory.store()` 的 leaf 写入使用手工 RawEvent/Candidate/AtomCard 路径，无需 LLM。
`root → branch → leaf` 是组织树，leaf 用 `atom_id` 投影事实正文，不复制第二份事实。
人工 correction 创建 successor、active-only supersede 旧 atom 并写 `user_edit` journal；
不伪造新对话证据。page regeneration 保护 `## My Notes`，绝对事件时间统一为 aware UTC。

### 读取与隔离

- `Memory.recall()` 返回 atom 对应的 tree leaf；`Memory.search()` 搜索归档 conversation messages。
- `MemoryService.recall()` / `recall_for_prompt()` 返回 `RecallResult.snippets` 与 `rendered`。
- 主召回：cache → parse → route → gather → raw policy → rerank → diversify → suppress → budget → render。
  默认 atom/raw，entity anchor 加 page_headline，有 index 加 vector；tree 非独立 source。
- 默认 raw fallback 在命中提炼结果时排除 raw；传入当前 session/thread 时排除本会话 raw。
  工具搜索还可合并允许的 host Markdown，`memory_get` 按虚拟路径读取。
- SQLite 使用 namespace 表名前缀；PostgreSQL 使用共享 `octop_memory` schema 和 namespace-first keys。
  Chroma/Qdrant 仅增强检索，不能替代关系数据和事务。
