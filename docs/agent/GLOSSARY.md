# GLOSSARY.md

仅解释核心术语；模块和数据流见 [PROJECT_MAP](PROJECT_MAP.md)。代码实体保持英文。

| 术语 | 含义 |
|---|---|
| octop-memory / octop_memory / octopmemory | 项目与发行包名 / Python 包名 / 宿主插件 ID |
| Harness 维护闭环 | 文本澄清 → 最小实现 → 验证 → 项目认知回写；不等同于新增任务日志 |
| Memory / MemoryBackend | 公共存储 API / SQLite 与 PostgreSQL 共用 contract |
| MemoryRuntime | MemoryService 和 Bridge 复用的宿主无关编排 |
| Bridge | JSON-RPC adapter；OpenClaw 通过 stdio 调用，Hermes 在进程内复用 |
| RawEvent / L0 | 不可变来源证据，受 capture policy 过滤 |
| Candidate / L1 | 待 review/promotion 的提炼结果，保留证据引用 |
| AtomCard / L2 | canonical fact；修正追加 successor，旧事实失效 |
| Episode / L2.5 | 事件记忆；绝对时间统一 aware UTC |
| Entity / EntityPage / L3 | 实体锚点 / 有版本、可重新生成的整合页面 |
| journal / L4 | 业务决策与部分 lifecycle 记录，不是全部 SQL 操作日志 |
| root / branch / leaf | 组织树层级；leaf 通过 atom_id 引用事实 |
| source | recall 的候选来源；默认 atom/raw，按条件加入 page_headline/vector |
| host file / virtual path | 宿主 Markdown 的索引快照 / 数据库记录投影的可读路径；工具读取不等于 prompt 自动注入 |
| raw_policy | 控制 raw 参与 prompt recall 的方式；fallback 为默认兜底 |
| extraction cursor | meta 的 `extracted_ids/v1/<session>`；表示候选提炼已提交，不表示 promotion/page/Episode 全部完成 |
| summary_version / mutation_version | 页面正文生成/编辑版本 / storage 管理的变更修订；后者用于拒绝旧生成快照写回 |
| manual_documents / manual_context | 当前手动文件用于语义治理 / 实际会话快照用于召回去重；治理不刷新聊天快照 |
| uncertain | 手动审核中已比较但仍无结论的活跃 assertion hash；不是 complete，也不等于尚未比较 |
| scheduler / review worker | 保存后立即返回的排队回调 / 实际调用同步审核方法的宿主后台任务 |
| namespace | 记忆隔离标识；SQLite 表名前缀、PostgreSQL shared schema 中的 namespace key |
| checkpoint_ns | LangGraph 执行流命名空间，不等同于 memory namespace |
| retention / vacuum | 历史留存删除 / 物理空间维护；不可互相替代 |
| VectorIndex | 可选向量检索增强，不替代关系 backend |
| ADR | 带稳定编号的设计决策，保留理由、代价与 superseded 关系 |
