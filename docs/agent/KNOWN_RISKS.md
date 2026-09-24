# KNOWN_RISKS.md

只记录仍影响维护的风险：触发条件、影响、应对与验证。修复后删除失效描述或保留简短 Resolved 条目和对应 PR。

- PG fixtures 在服务不可用时 skip，当前 CI/release workflow 未配置专用 PG service；不能仅凭基础 CI 证明兼容。
- `run_gc()` retention 当前仅 SQLite 实现，PG 返回空统计，是待修复的既有能力缺口。
  删除行、checkpoint retention 和物理空间回收是不同操作；不能据此推断宿主已自动调度。
- PostgreSQL 的 `btree_gin` 不可用时退回普通 GIN，namespace 过滤正确性保留，性能隔离减弱。
  共享 checkpoint 表维护需 database 级范围判断；普通 VACUUM 也不能承诺无锁等待。
- recall 墙钟预算可能受机器负载影响；超时失败需区分调度抖动与实际召回回归。
- dashboard 直接访问 SQLite 且不在 wheel 内。可选依赖版本、大库迁移耗时和真实 host hook 需要单独验证。
- 部分 SQLite 运行时的只读 FTS 检查可能报 `attempt to write a readonly database`；先验证引擎兼容性，不跳过完整性检查。
- Hermes 的安装解释器、`.pth` 位置和 standalone 与 installer 分发方式需要分别验收；OpenClaw hybrid 依赖真实 host 调用 prefetch。
- 合成 recall eval 的标签、时间与小样本限制见 TEST_MATRIX，尚不能作为正式质量门禁。
- 驱动、I/O 和 payload 错误会降级。其它异常会抛回调用方。未列入 `VECTOR_ERRORS` 的向量客户端错误不会被吞掉。见 ADR-038。
- `scripts/memory-slim --online` 依赖同级 Octop checkout，且脚本优先使用其虚拟环境。
  独立用户使用 `octop-memory db slim`。在线维护仍需宿主协调。
- `deploy-openclaw.sh` 会安装依赖、更新 checkout、替换插件目录。`--setup-dry-run` 只预览配置步骤。
  部署和真实宿主 E2E 需在专用环境另验。

## 已有安装的迁移

源码使用当前发行包、插件 ID、环境变量、默认目录和 PG schema。旧安装不会自动迁移。
步骤见 [集成指南](../integrations.md#更名与已有安装迁移)，存储协议取舍见 ADR-037。
