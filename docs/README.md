# 文档导航

按使用场景查阅 Octop Memory 文档。入门和安装见 [中文 README](../README_CN.md) /
[English README](../README.md)，版本变化见 [CHANGELOG](../CHANGELOG.md)。

## 使用与接入

| 目标 | 阅读入口 |
|---|---|
| 在 Python 中保存、召回记忆 | [快速开始](../README_CN.md#快速开始)、[公共 API 示例](../examples/basic_usage.py) |
| 接入自有 Agent、OpenClaw 或 Hermes | [集成指南](integrations.md#接入方式与边界) |
| 升级旧名称的安装与数据目录 | [更名与已有安装迁移](integrations.md#更名与已有安装迁移) |
| 备份、瘦身或回退 checkpoint 格式 | [Checkpoint 维护](../CONTRIBUTING.md#checkpoint-维护) |
| 确认支持范围和未完成能力 | [已知风险](agent/KNOWN_RISKS.md) |

## 理解代码与继续开发

建议依次阅读项目地图、相关设计决策和测试矩阵；接续具体任务时再查看 HANDOFF。

| 文档 | 维护的内容 |
|---|---|
| [PROJECT_MAP](agent/PROJECT_MAP.md) | 模块入口、公共 API、写入/召回流程和存储边界 |
| [DECISIONS](agent/DECISIONS.md) | 稳定 ADR 编号、设计理由、代价与替代关系 |
| [TEST_MATRIX](agent/TEST_MATRIX.md) | 按改动范围选择命令，说明可选环境和验证边界 |
| [KNOWN_RISKS](agent/KNOWN_RISKS.md) | 当前实现的限制、触发条件与应对 |
| [GLOSSARY](agent/GLOSSARY.md) | 核心术语与容易混淆的名称 |
| [README_AUDIT](agent/README_AUDIT.md) | 对外声明与代码契约的核对项 |
| [HANDOFF](agent/HANDOFF.md) | 尚需接续的代码任务、验收和未解决问题 |

贡献和分支流程见 [CONTRIBUTING](../CONTRIBUTING.md)，Agent 工作规则见 [AGENTS](../AGENTS.md)。

## 维护约定

- 安装、配置、宿主排错统一维护在集成指南；插件自己的 README 只保留分发入口。
- 模块和行为事实写入项目地图，理由写入 ADR，限制写入风险清单，验证方法写入测试矩阵。
- HANDOFF 只保留接续所需信息；已合入的任务移出当前任务，历史由 Issue / PR / Git 保存。
- 不复制整份 README 或测试结果基线；以相关源码、测试和配置核对内容，不按文档日期判断新鲜度。
