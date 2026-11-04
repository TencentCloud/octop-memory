# README_AUDIT.md

对外行为变化时核对下表及中英文 README；只维护当前契约，不保存逐次编辑报告。

| 核对项 | 当前约束 / 代码依据 |
|---|---|
| 代码架构 | README 展示源码目录职责、宿主调用链和长期记忆数据流；详细入口见 PROJECT_MAP |
| 文档入口 | docs/README 按使用场景导航；安装/配置集中在 integrations，贡献流程在 CONTRIBUTING，agent 文档各守单一职责 |
| Candidate / 页面 | 候选可由 promotion 自动处理，不等于全量人工审核；dirty page 按触发再生成，mutation_version 条件写回拒绝旧快照；summary_version 仍只随正文生成/编辑增加，Episode 从 raw 独立提炼 |
| 项目命名 | `octop-memory` / `octop_memory` / `octopmemory`；Python Agent 宿主名为 `octop-harness`；默认目录与 PG schema 改名，升级入口链接集成指南 |
| 快速开始 | core 不要求可选依赖；MemoryService.recall 返回 snippets/rendered；`core.py`、`service.py` |
| 事实与树 | AtomCard 是事实真源，leaf 是引用；`core.py`、`storage/backends/` |
| 搜索范围 | Memory.recall 为 leaf 投影，Memory.search 为归档消息，prompt recall 为多源；`core.py`、`pipeline/recall/` |
| 召回策略 | 默认 atom/raw；entity anchor 加 page_headline、index 加 vector；raw fallback 与 session 排除；`router.py`、`multi_source.py`、recall 入口 |
| backend | SQLite/PostgreSQL 是关系 backend；PG 共享 8+4 连接预算、30 秒等待及 64 请求队列，注明进程/配置边界；Chroma/Qdrant 是 index，不宣称 PG skip 等于通过 |
| capture / page | 不承诺每条消息都捕获，dirty 不等于已再生成；`application/runtime.py`、`pipeline/page/` |
| 提炼进度 | 默认 100 条/20,000 字符估算分批，超大单条不丢弃；候选与持久游标同事务，失败整体重试；游标不代表后续 promotion/page/Episode 成功；`pipeline/extractor/`、`application/runtime.py` |
| 运行报错 | 在线仅按结构化错误标记排除，主动记忆由 Agent 调用 memory_save；无记忆意图/排障价值关键词。维护操作仅停用全部证据均有标记的自动记录；旧版格式仅列审核清单，缺失/混合来源不改状态，人工保护来自记录；`domain/retention.py`、`operations/retention.py` |
| 宿主接入 | OpenClaw/Hermes 共享一份指南；Hermes 模型配置不等于自动提炼，sync_turn 当前只 capture；真实 host hook、安装环境与纯 Python 单测分开验证 |
| 宿主文件 | 默认含根文件 MEMORY/USER/DREAMS、日期日记及 topics/projects；HostFilesIndex 始终用 SQLite，扫描不等于语义审核；Service recall/Hermes prefetch 不自动继承工具搜索的全部配置 |
| 数据维护 | 不将去重、retention、vacuum 混为一谈；明确备份、offline 声明与旧 reader 兼容性 |
| 手动文档 | 手动 USER.md/MEMORY.md 优先；save 仅落库和通知 scheduler，宿主 worker 调用公共 `MemoryService.review_new_memory(max_atoms=20)` 读取 provider 并审核；该方法同步执行，不能阻塞用户回合。原文证据与置信校验、事务软停用和替代候选不变；重复仅按实际 session 快照抑制，完整文档不入 Atom/索引；`service.py`、`operations/retention.py`、`pipeline/recall/` |
| 发布内容 | sdist allowlist、wheel 与独立插件分别验证；不将源码 dashboard 宣称为 wheel 功能 |

实现入口与命令统一见 [PROJECT_MAP](PROJECT_MAP.md)、[TEST_MATRIX](TEST_MATRIX.md)。
发现不一致先修正受影响的声明，不复制整段 README，也不记录私人运行样本。
