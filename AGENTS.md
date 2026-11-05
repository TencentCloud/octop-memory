# AGENTS.md

本文件是所有 coding agent 共用的工作协议。用户入口见 [README_CN.md](README_CN.md)，
文档导航见 [docs/README.md](docs/README.md)，贡献入口见 [CONTRIBUTING.md](CONTRIBUTING.md)，
项目认知保留在 `docs/agent/`，宿主接入见
[docs/integrations.md](docs/integrations.md)。沿用 Harness 的澄清、实现、验证、文档对齐闭环。
本文件约束仓库贡献工作；Agent 实际使用的记忆提示、用户工作区模板与文件权限策略由宿主实现。

## 项目速览

`octop-memory` 是可独立使用、发布并在运行时被调用的记忆基础库，为 Octop / octop-harness 及其他宿主提供记忆能力。
它在生态中提供记忆底座，贡献者按可复用库维护，不能假定用户部署了 Octop 或拥有相邻仓库 checkout。
本仓负责证据捕获规则、事实提炼、召回、存储与迁移；Agent 回合接线、执行调度和产品界面由上层组织。
核心是 stdlib + SQLite，PostgreSQL、向量索引和 LLM 通过可选后端/接口接入。

- 公共入口：`Memory`（`core.py`）与 `MemoryService`（`service.py`）；共享编排在 `application/`。
- 业务流水线在 `pipeline/`，持久化在 `storage/`；CLI / JSON-RPC 在 `adapters/`。
- `plugins/<host>/` 是外部 Agent 适配目录；当前实现包括 OpenClaw、Hermes，新增宿主复用共享业务逻辑。
- 按任务找代码和验证入口，见 [PROJECT_MAP](docs/agent/PROJECT_MAP.md) 与 [TEST_MATRIX](docs/agent/TEST_MATRIX.md)。

## 三仓共建与改动归属

依赖方向为 **Octop → octop-harness → octop-memory**；Octop 的记忆管理 API 也直接使用 octop-memory。
本仓生产代码不反向依赖另两仓。新增能力先判断行为归属，再决定是否联动；不能因问题从 Octop 界面暴露，
就把记忆算法或 Agent 回合逻辑复制到 Octop。

| 要改变什么 | 主改仓库 | 需要检查的联动 |
|---|---|---|
| 提炼结果、晋升/冲突规则、召回来源与排序、证据和数据契约 | octop-memory | 公共接口/结果变化时检查 Harness 消费及 Octop 管理 API；接口不变的算法修复无需另两仓复制实现 |
| 何时 capture/extract、prompt 注入与回放、memory 工具绑定、后台审核/维护、实例生命周期 | octop-harness | 复用 memory API；需要产品开关、持久配置或状态展示时联动 Octop |
| Octop Agent 的辅助模型选择、重试、超时与配置转发 | octop-harness | Octop 负责配置界面/保存/映射；memory 负责通用 LLM 协议和算法所需预算，不能用改 prompt 代替修复宿主调用 |
| USER.md/MEMORY.md 的会话加载、Agent 写保护和运行时记忆提示 | octop-harness | Octop 负责用户编辑入口、所有者权限和文件保存；memory 负责语义对账、冲突候选及召回抑制 |
| 用户记忆设置、HTTP/CLI、权限、记忆页/候选审核交互、主动提醒产品流程 | Octop | 通过 Harness 配置/公共记忆接口落地；新增算法或通用回合能力回到对应库 |
| schema、SQL、namespace、checkpoint 编码、迁移和通用空间维护 | octop-memory | 检查 Harness 的资源复用/关闭，以及 Octop 的统计、备份恢复、线程清理和在线暂停协调 |
| OpenClaw/Hermes hook、工具 schema、安装配置 | 本仓 `plugins/<host>/` | 复用本仓 Runtime；无须为外部宿主修改 octop-harness 或 Octop |

两个同名类不可混用：本仓 `application.runtime.MemoryRuntime` 是宿主无关编排；
`octop_harness.memory.runtime.MemoryRuntime` 是 Agent 侧装配与生命周期适配。
具体文件和端到端调用链见 [跨仓协作地图](docs/agent/PROJECT_MAP.md#跨仓协作地图)。

跨仓任务按以下方式交付：

1. 在 Issue / PR 中列明主改仓库、受影响消费者、API/配置/数据兼容性，以及各仓验收条件；编辑其它仓库前读取其 AGENTS。
2. 公共字段、RPC 返回、配置项或持久化格式变化时沿真实消费链核对，不只验证本库单测。
   Octop 已有直接读记忆表的管理代码属于需审计的消费者，不能作为新增业务绕过库接口的模板。
3. 按 [跨仓验证](docs/agent/TEST_MATRIX.md#跨仓验证) 运行相关库测试与真实宿主契约，记录实际加载的版本或源码 revision；
   本仓绿色、旧安装包绿色、PG/宿主 skip 均不能代替跨仓验收。
4. 相关 PR 相互链接，说明依赖顺序。需要消费新接口时先发布底层库，再更新 Harness/Octop 的依赖声明与锁文件，
   确认消费环境实际加载新版；本地源码联调不代表发布用户已取得修复。只改产品展示时不要求其它仓库陪改。

## 工作方式

- 开始前检查 `git status --short` 和相关 diff，保留用户已有改动。
- 非平凡改动先说明目标、涉及文件、预期行为、最小验证和风险；大任务分阶段，保持范围聚焦。
- 代码、测试、配置和文档同步更新；不顺手重构无关模块，不为每次任务新增文档。
- 中文解释设计，保留代码实体、字段、CLI、环境变量与错误信息原文。
- 示例使用通用占位符和临时数据库，不提交私人路径、运行记录、凭据、导出记忆或真实数据库。

## Harness 文档闭环

**记录范围**：`docs/agent/` 用于理解代码和接续开发，记录业务逻辑、架构、public API、数据契约、
召回/写入行为，以及相关 bug、新需求、设计取舍和未解决风险。
banner、排版、措辞、链接、Python 版本、依赖/锁文件、构建/CI 和个人工具配置等常规维护，
只更新对应文件及必要的 README/CONTRIBUTING/CHANGELOG，不写入 Harness，也不为此新建 HANDOFF 或 ADR。
若这类维护同时改变了运行行为或数据契约，仅记录其代码影响，不记录维护操作流水账。
以下回写要求仅适用于上述代码相关范围；判断依据是实际影响，不是改动文件数量或是否修改了代码文件。

1. **入场核对**：快速扫描相关代码、diff 与近期相关提交，对照 [HANDOFF](docs/agent/HANDOFF.md)
   和 [PROJECT_MAP](docs/agent/PROJECT_MAP.md)。核对召回 source/顺序、public API、配置、prompt 与验证命令；
   不依赖日期或提交数推断新鲜度。发现漂移，修正相关描述；范围外缺口记入 [KNOWN_RISKS](docs/agent/KNOWN_RISKS.md)。
2. **文本澄清**：在现有 Issue / PR 或 HANDOFF 中写明 bug 的实际/预期行为、最小复现，
   或需求的使用场景、范围/不做什么、可验证验收条件；列出假设和待澄清问题。
   会改变范围、兼容性或数据安全的未决问题需先澄清；已明确授权的工作继续执行，不重复索要确认。
3. **实现与验证**：按最小可验证闭环实现，运行相关测试及完整门禁。涉及召回/写入或双 backend 时执行下述硬规则。
4. **认知回写**：代码相关的非平凡任务更新 HANDOFF 的当前状态、关键文件、验证结果、风险与下一步；
   模块/调用链/运行策略配置变更更新 PROJECT_MAP；重要代码取舍更新 [DECISIONS](docs/agent/DECISIONS.md)；
   行为验证方式变更更新 [TEST_MATRIX](docs/agent/TEST_MATRIX.md)；代码相关缺口更新 KNOWN_RISKS；
   术语变更更新 [GLOSSARY](docs/agent/GLOSSARY.md)，对外行为变更核对 [README_AUDIT](docs/agent/README_AUDIT.md)。

每份文档只承载自己的事实，不重复复制。HANDOFF 保留当前任务/未决项；已合入任务移出当前任务，
必要时只留历史入口和接续边界，稳定知识归入地图、决策、测试或风险。代码已合入不代表当前环境已重新验收。
DECISIONS 保留稳定 ADR 编号及替代关系，状态需核对当前实现，不追加日期流水账。
核对代码并修正文档漂移时，直接更新对应认知文件，无需将文档整理另记为代码任务。
只改 HANDOFF 而遗漏受影响的地图或测试矩阵不算完成。

## 架构约束

- `Memory` 为公共存储 API；`MemoryService` / `Bridge` 的共享宿主操作复用 `application.MemoryRuntime`。
  `MemoryService.recall()` 直接调用 prompt pipeline；工具搜索和 prompt 召回的配置、来源需分别核对。
- pipeline、storage、domain 不依赖 adapters；storage 不依赖 pipeline；service 不依赖 bridge。
- 新宿主通过 application 接入；现有源码 dashboard 的直接 SQLite 访问不是新模块的模板。
- `AtomCard` 是事实真源，tree leaf 只引用 atom；Chroma/Qdrant 是可选 index，不是关系 backend。
- Runtime 增量提炼的候选与持久游标必须同事务提交，提交后再推进内存进度；不先标记成功再落候选，
  不将游标提交等同于后续 promotion/page/Episode 全部完成。低层 extractor 入口的失败语义另行核对。
- 自动页面再生成路径必须传入并原子校验读取时的 `mutation_version`；条件失败不降级为无条件写入。
  保留公共底层页接口未传版本条件时的旧调用兼容性；所有页面写入者维护修订契约。
- `MemoryService.save()` 只落库并通知非阻塞 scheduler；同步的 `review_new_memory()` 由宿主后台执行。
  手动文档的当前文件用于治理，实际聊天快照用于召回去重，不互相替代。
- 宿主配置转发、hook 调度和实际执行分别验证；模型配置可用不等于已触发提炼，profile 策略不跨宿主推定一致。
- 不擅自改变 public API、数据格式、配置默认值或增加生产依赖。

上述流程与入口的详细契约见 [PROJECT_MAP](docs/agent/PROJECT_MAP.md)，宿主现状和限制见
[集成指南](docs/integrations.md) 与 [KNOWN_RISKS](docs/agent/KNOWN_RISKS.md)。

## 双 backend gate

SQLite/PostgreSQL 均为正式支持的 memory backend。涉及 public 读写、持久化编排、backend contract、
schema、SQL、transaction、FTS 或 lifecycle 时：

1. 同步维护两种 backend，用同一组行为断言验证，优先扩展 `tests/test_backend_parity.py`。
2. 使用真实专用 PostgreSQL 和 `TEST_POSTGRES_DSN`，确认相关 PG cases 未 skip。
3. 有意的单 backend 能力必须明确拒绝另一 backend，并在 KNOWN_RISKS 中说明原因。
4. backend-specific SQL 留在 `storage/backends/`；不把既有能力缺口当作新代码可以静默分叉的理由。

## 召回与写入敏感路径

| 范围 | 需要说明的影响 |
|---|---|
| `core.py`、`storage/backends/` | 公共写入、事务、schema、索引、namespace |
| `pipeline/extractor/`、`pipeline/promotion/`、`pipeline/episode/` | 哪些内容成为 Candidate、AtomCard 或 Episode |
| `pipeline/recall/`、`pipeline/page/` | source、排序、raw policy、去重、budget、页面内容 |
| `application/runtime.py`、`application/path_projection.py`、`application/host_files.py` | 工具搜索、路径可读内容和 host 文件 |
| `adapters/bridge/handlers.py`、宿主插件 hook | capture/extract、memory_search/memory_get 的宿主行为 |

修改这些行为时，用 `⚠️ 召回/写入路径变更` 明确说明具体影响、受影响 source/layer 和验证命令。
仅编辑说明或构建清单时说明行为未变，不将文档整理描述为 runtime 变更。

## 验证与交付

```bash
make install-hooks    # 每个 clone 一次，提交前不得绕过失败门禁
make all              # format + lint + strict mypy + pytest
```

代码改动先运行最小相关测试，再执行完整门禁；纯文档维护只需相关文档检查，提交前仍执行完整门禁。
文档检查使用 `tests/test_agent_docs.py` 与 `tests/test_architecture_boundaries.py`；新增/移动文件或修改标题时，
同步导航、本地链接与标题锚点，并核对公开文档检查范围、目录约定和 sdist allowlist。
失败时找到首个根因、做范围内最小修复并重跑；
环境或既有问题阻止验证时，明确报告失败/skip 和复现命令，不声称通过。
交付前确认实现符合验收、相关文档与代码一致；属于 Harness 记录范围的任务将实际验证和未解决项写入 HANDOFF；
无法通过的门禁必须说明根因及后续命令，不能把未验证状态写成完成验收。
完成回复简述改动、验证和剩余限制即可，不要求固定多节交接模板。

未经用户明确授权，不执行 push、重写 Git 历史、大目录删除、生产部署或密钥修改。
详细验证与双 backend 命令见 [TEST_MATRIX](docs/agent/TEST_MATRIX.md)，分支与发布检查见 [CONTRIBUTING.md](CONTRIBUTING.md)（`develop` 集成 + `/publish` skill）。
