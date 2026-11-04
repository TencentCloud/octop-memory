# KNOWN_RISKS.md

只记录仍影响维护的风险：触发条件、影响、应对与验证。修复后删除失效描述或保留简短 Resolved 条目和对应 PR。

## 写入、提炼与审核

- 提炼的 20,000 字符是正文加开销估算，不是严格 prompt/token 上限；单条超预算事件仍自成一批，可能继续触发模型长度限制。Runtime 单次最多读取 10,000 条 session raw，不代表已扫描全部历史。任一模型批次失败会让本轮全部候选和游标不提交，重试可能重复模型成本；候选/游标提交之后的 promotion、page、Episode 不在同一事务，不能把游标当作全部阶段成功标记。复现与重试验证见 TEST_MATRIX 的提炼条目。
- 低层 `extract_session(..., persist=True)` 会持久化返回的部分候选，不检查整轮 `failure_reason`，也不维护 Runtime 游标。需要增量失败回滚语义的宿主使用 Service/Bridge extract；自行调用低层 extractor 时先检查结果再决定入库。
- EntityPage 的修订校验依赖所有写入者维护 mutation_version。升级需同步更新使用该库的写入进程；旧版本进程或直接 SQL 绕过修订/dirty 调用链时不提供同样保证。未指定版本条件的旧页接口保留原 backend 调用方式；自定义 backend 使用自动再生成须实现新增条件参数与修订维护，不能忽略条件或将错误降级成无条件重放。旧结果被拒绝后不立即重试，dirty 页由宿主后续触发；clean 页上的较新用户编辑/生成结果保持 clean。
- 后台手动文档审核需要宿主注册非阻塞 scheduler/provider，并在 worker / 维护拍调用公共 `MemoryService.review_new_memory`。该入口从 1.0.3 可用；仅升级宿主而保留 memory 1.0.2 会缺少入口并跳过审核，宿主须提高最低依赖。该方法同步执行一批模型调用，不能直接放回用户保存路径。

- 手动文档语义对账依赖辅助模型；只接受原文引用、明确主体/范围和高置信结果，不确定/失败不强制停用。单次最多 100 条未核对 Atom、100,000 文档字符，宿主需呈现 partial/uncertain/unavailable 状态并支持续查。外部宿主未注册 provider 时，仅显式对账生效。外部文件并发写依赖宿主 freshness callback；采纳需传当前文档，底层 worker 至少校验最后对账版本。旧版本候选保留审计但不可直接采纳，需拒绝并核对新版。冲突 raw 整条从自动 fallback 排除，其中混合的其它事实仍可经有效 Atom/直接证据读取；不批量删除历史 Episode。见 ADR-042。
- 审计元数据缺少 `uncertain` 字典的旧库需一次保守重审，包含旧报告为 complete 的情况；历史进度无法区分已验证与仅比较，因此会有额外模型成本。状态读取只列为待审，对账仍遵守现有批次上限；新格式写入后按正常进度续查，不每轮清零。

- 未带结构化标记的合成报错无法可靠区别于正常对话，在线链路保留，不能声称已隔离所有旧报错。octop-harness / Octop 主路径已抛出异常；自定义宿主仍合成错误时必须传 `payload.octop_runtime_error=true`。旧版固定报错格式仅用于存量审核提示；缺失/混合来源的 review_*_ids 是只读清单，不是新增 UI 待办或自动停用，需人工核对。手工保存/批准/编辑不按措辞推断。见 ADR-041。

## 存储与维护

- PG 共享池预算仅覆盖同一进程、同一连接配置；多进程、不同配置和宿主控制面要额外预留服务端连接。长事务会占用业务池，饱和时有界等待后报错，不会无限扩容。CI/release 已使用专用 PG；本地未配置测试服务的 skip 仍不算 PG 验收。
- `run_gc()` retention 当前仅 SQLite 实现，PG 返回空统计，是待修复的既有能力缺口。
  删除行、checkpoint retention 和物理空间回收是不同操作；不能据此推断宿主已自动调度。
- checkpoint pruning 和内容去重/展开等格式维护仅支持 SQLite；PG 明确拒绝这些入口，使用其原生 saver。宿主不能把 PG 的 vacuum 当作已完成 checkpoint retention，见 [Checkpoint 维护](../../CONTRIBUTING.md#checkpoint-维护)。
- PostgreSQL 的 `btree_gin` 不可用时退回普通 GIN，namespace 过滤正确性保留，性能隔离减弱。
  共享 checkpoint 表维护需 database 级范围判断；普通 VACUUM 也不能承诺无锁等待。
- 部分 SQLite 运行时的只读 FTS 检查可能报 `attempt to write a readonly database`；先验证引擎兼容性，不跳过完整性检查。

## 宿主适配与分发

- Hermes 配置 `llm` / `extraction` 只把能力传入 Runtime；当前 `sync_turn()`、`on_memory_write()`、`on_pre_compress()` 只 capture，不自动提炼。需要额外宿主调度显式调用 extract，不能仅凭模型配置宣称会生成 Atom。当前 shutdown 对每线程最多 join 5 秒，不主动关闭 Memory backend；慢后台任务及资源释放需单独检查。实现见 [provider](../../plugins/hermes/octopmemory/__init__.py)，接入见 [集成指南](../integrations.md#hermes)。
- `HostFilesIndex` 在 PG memory 下仍使用 SQLite sidecar；文件扫描不等同于手动文档语义审核。`MemoryService.recall()` 和 Hermes prefetch 直接调用 prompt recall，不自动应用 Runtime 工具搜索的全部配置或 host 文件来源。
- Hermes/Python Runtime 不展开 profile 策略；仅设置 `profile="privacy"` 不会自动收紧捕获或脱敏配置。宿主应显式设置各 policy，并通过 stats 检查有效值；OpenClaw 的 TS profile 表只适用于其插件。
- dashboard 直接访问 SQLite 且不在 wheel 内。可选依赖版本、大库迁移耗时和真实 host hook 需要单独验证。
- Hermes 的安装解释器、`.pth` 位置和 standalone 与 installer 分发方式需要分别验收；OpenClaw hybrid 依赖真实 host 调用 prefetch。
- `scripts/memory-slim --online` 依赖同级 Octop checkout，且脚本优先使用其虚拟环境。
  独立用户使用 `octop-memory db slim`。在线维护仍需宿主协调。
- `deploy-openclaw.sh` 会安装依赖、更新 checkout、替换插件目录。`--setup-dry-run` 只预览配置步骤。
  部署和真实宿主 E2E 需在专用环境另验。

## 检索质量与验证

- recall 墙钟预算可能受机器负载影响；超时失败需区分调度抖动与实际召回回归。
- Chroma/Qdrant 适配当前只有 fake/mock 验证，缺真实向量后端集成验收；安装 extra 不自动初始化/注入索引，不能当作已启用向量召回。
- 合成 recall eval 的标签、时间与小样本限制见 [TEST_MATRIX](TEST_MATRIX.md#验证边界)，尚不能作为正式质量门禁。
- 驱动、I/O 和 payload 错误按各入口的异常元组降级，其它异常抛回调用方；未列入 `VECTOR_ERRORS` 的向量客户端错误不会被吞掉。见 ADR-038。

## 已有安装的迁移

源码使用当前发行包、插件 ID、环境变量、默认目录和 PG schema。旧安装不会自动迁移。
步骤见 [集成指南](../integrations.md#更名与已有安装迁移)，存储协议取舍见 ADR-037。
