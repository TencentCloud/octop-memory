# HANDOFF.md

代码相关任务的文本澄清与接续入口。使用 Issue / PR 编号或简短标题识别任务，不以日期建流水账。
有 Issue / PR 时链接正文，只在这里保留状态、关键结论和下一步；未开 Issue 时直接使用下方模板。
已完成记录压缩或替换，稳定知识移入对应文档，历史由 Git / Issue / PR 保存。

## 当前状态

当前代码基线为 `pyproject.toml` 声明的 **1.0.3**。下列事项已合入，不再列作待审阅或待发布任务；
这表示代码已进入版本基线，不表示当前工作环境重新完成了 PostgreSQL、真实宿主或发布产物验收。

| 已合入事项 | 历史入口 | 稳定知识与接续边界 |
|---|---|---|
| 提炼分批、跨重启游标与候选/游标原子提交 | [#25](https://github.com/TencentCloud/octop-memory/issues/25)、[PR #29](https://github.com/TencentCloud/octop-memory/pull/29) | [提炼与增量进度](PROJECT_MAP.md#提炼与增量进度)；失败回滚、重试和跨重启验证见 [TEST_MATRIX](TEST_MATRIX.md) |
| 手动文档审核移至后台、补齐公共入口、保守升级旧审计进度 | [PR #31](https://github.com/TencentCloud/octop-memory/pull/31)、[PR #32](https://github.com/TencentCloud/octop-memory/pull/32)、[PR #33](https://github.com/TencentCloud/octop-memory/pull/33) | [手动文档治理](PROJECT_MAP.md#手动文档治理)、ADR-042；宿主仍需注册 scheduler/provider，并使用包含公共审核入口的版本 |
| EntityPage 条件写回与并发修订迁移 | [PR #24](https://github.com/TencentCloud/octop-memory/pull/24) | ADR-043、[页面生成](PROJECT_MAP.md#页面生成)；所有写入进程和自定义 backend 必须维护修订契约 |
| Windows host glob、Hermes 模型配置与安装说明 | [PR #26](https://github.com/TencentCloud/octop-memory/pull/26)、[PR #15](https://github.com/TencentCloud/octop-memory/pull/15)、[PR #16](https://github.com/TencentCloud/octop-memory/pull/16) | [宿主文件](PROJECT_MAP.md#宿主文件与迁移)、[集成指南](../integrations.md)；真实宿主安装与生命周期另验 |

更早的连接容量、结构化报错隔离、手动文档优先和命名收尾已纳入当前实现，见
[PROJECT_MAP](PROJECT_MAP.md)、ADR-037～042 与 [CHANGELOG](../../CHANGELOG.md)。
历史测试数量和私人复现场景不作为新任务的验收基线，原始结果在对应 PR / Git 中查阅。

## 接续入口

本文目前没有仍需继续实现的任务记录。仓库的开放 Issue / PR 应以其实际状态为准。
现存能力缺口集中在 [KNOWN_RISKS](KNOWN_RISKS.md)，验证命令集中在 [TEST_MATRIX](TEST_MATRIX.md)。
开始新代码任务时补充下方记录；纯文档排版、链接或构建维护不新增任务日志。

## 任务模板

- **任务 / 状态 / 链接**：待澄清 → 可实现 → 进行中 → 待验证 → 完成；阻塞时写明原因。
- **问题与场景**：bug 的实际/预期行为及最小复现；新需求的使用者、场景和期望结果。
- **范围与验收**：要做/不做、可验证条件、兼容性及数据影响。
- **待澄清 / 假设**：哪些已确认，哪些影响实现；不能把未回答的问题记成确认。
- **方案与关键文件**：最小实现、受影响调用链；重要取舍链接 DECISIONS。
- **验证**：确切命令、实际结果、失败/skip/未覆盖，必要时附脱敏证据。
- **接续**：当前进度、未解决风险、下一步；受影响文档是否同步。
