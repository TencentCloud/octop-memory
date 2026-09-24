# TEST_MATRIX.md

按改动范围选择最小验证；提交门禁为 `make all`。代码任务的实际结果写入 [HANDOFF.md](HANDOFF.md)，不维护带日期的测试数基线。

| 改动范围 | 最小命令 |
|---|---|
| 包名 / 宿主命名 | `uv run pytest tests/test_cli_entry.py tests/test_cli_config.py tests/test_bridge_server.py tests/test_hermes_adapter.py tests/test_hermes_install.py -q`；`uv build`；在 `plugins/openclaw/octopmemory/` 执行 `npm test`、`npm run typecheck`、`npm run build` |
| 文档 / import 边界 | `uv run pytest tests/test_agent_docs.py tests/test_architecture_boundaries.py -q` |
| Memory / runtime | `uv run pytest tests/test_core.py tests/test_service.py -q` |
| 召回 | `uv run pytest tests/test_recall_pipeline.py tests/test_recall_multi_source.py tests/test_recall_risk_fixes.py -q` |
| 提炼 | `uv run pytest tests/test_extractor.py tests/test_promotion.py -q` |
| 写入 / backend | `uv run pytest tests/test_memory_atom_write.py tests/test_backend_parity.py -q -rs` |
| 向量接口 / 可选 index | `uv run pytest tests/test_vector_chroma.py -q`；使用 mock index 和 `tmp_path` 数据库，不访问默认数据目录 |
| Checkpoint | `uv run pytest tests/test_checkpoint_compaction.py tests/test_checkpoint_gc_safety.py -q` |
| 迁移 | `uv run pytest tests/test_migration_export_import.py tests/test_portable_e2e.py tests/test_portable_sources.py -q` |
| 公共 API 示例 | `uv run python examples/basic_usage.py` |
| Recall 评测 | `uv run python -m evals.recall.run` |
| 示例 / 评测静态检查 | `uv run ruff check examples evals` 与 `uv run ruff format --check examples evals` |

SQLite/PostgreSQL 均为生产 backend。public 读写、持久化编排、contract、schema、transaction、FTS、
lifecycle 修改必须同步维护两种实现，并以同一组断言运行真实 PostgreSQL：

```bash
# 使用专用测试数据库；以下凭据仅为占位。
TEST_POSTGRES_DSN='postgresql://user:pass@localhost/octop_memory_test' \
  uv run pytest tests/test_backend_parity.py tests/test_postgres.py \
    tests/test_memory_atom_write.py tests/test_checkpointer.py -q -rs
```

PG skip 不算通过。backend-specific 能力必须显式拒绝另一 backend 并记录限制。
测试使用 `tmp_path`，不能写用户真实记忆库；可选依赖和宿主 E2E 的 skip 单独报告。
若 uv 缓存不可用但虚拟环境工具齐全，可用 `make all RUN='.venv/bin/python -m'`。

## 验证边界

portable 默认发现测试将用户目录展开隔离到 `tmp_path`，覆盖当前 Agent 路径和显式 `extra_paths`。
portable dry-run 使用临时目标库并比较前后数据；召回降级/page headline 测试也使用临时库，不能访问用户默认库。

文档检查覆盖本地链接、公开内容和目录约定；不能证明全文不存在敏感信息。
宿主安装、optional dependencies、真实 PostgreSQL 与 recall 质量需分别验证，不能由基础单测替代。
合成评测的历史 `P@5` 标签实际分母为返回的前五条数量，不固定为 5；MRR 使用整个返回列表。
该小样本偏向实体查询，日期与 ID 每次生成；默认比较 raw_fts 和 full_pipeline，退出码只表示配置的 delta gate。
它没有接入 `make all` / CI，不能据此声明生产召回质量。只改注释时不调整评测口径或 seed 数据。
