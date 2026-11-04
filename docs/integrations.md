# 外部 Agent 集成指南

本页维护外部 Agent 的接入方式、宿主适配和验证约定；当前已有 OpenClaw、Hermes 实现。
核心 API 与开发流程见
[README](../README_CN.md) 和 [CONTRIBUTING](../CONTRIBUTING.md)，全部文档见 [文档导航](README.md)。

按场景阅读：[Python 宿主](#python-宿主) · [JSON-RPC bridge](#json-rpc-bridge) · [OpenClaw](#openclaw) · [Hermes](#hermes) · [排错](#共同验证与排错) · [旧安装迁移](#更名与已有安装迁移)。

## 接入方式与边界

Python 宿主优先使用 `MemoryService`；跨语言或隔离进程通过 stdio `octopmemory-bridge` 接入。
需要指定 Agent 的插件生命周期、工具 schema 和安装配置时，在 `plugins/<host>/` 实现适配。
`src/octop_memory/adapters/` 提供库侧 CLI/协议入口，`plugins/` 负责外部宿主侧接线，两者职责不同。

新增适配应明确四件事：namespace/session/thread 如何映射、何时 capture、何时召回和提炼、退出时如何清理后台任务。
记忆处理规则复用 `MemoryService` / `MemoryRuntime`，不要在插件中复制抽取、promotion 或排序逻辑。
在临时库上测试宿主契约，再以真实宿主验证安装、读写与生命周期；Python 单测不能代替宿主 E2E。

## Python 宿主

从 `octop_memory` 导入 `Memory` / `MemoryService`；可运行示例见
[`examples/basic_usage.py`](../examples/basic_usage.py)，示例只写临时数据库。
宿主负责提供 namespace/session/thread、选择何时调用和管理后台任务，库不启动自动记忆循环。

| 阶段 | 调用与契约 |
|---|---|
| 初始化 | `Memory(namespace=..., backend_config={"db_path": ...})`，再构造 `MemoryService(memory, llm=..., config=..., host=...)` |
| 显式保存 | `service.save(content, topic=...)` 建立确定事实并返回 tree leaf，无需 LLM |
| 回合结束 | `capture_turn(user=..., assistant=..., session_id=..., thread_id=...)` 按 policy 写 L0；是否放后台由宿主决定 |
| 提炼 | `extract(session_id, incremental=True, promote=True, regen_pages=True)`；同步调用，应在回复路径外执行；检查 `failure_reason` 与各阶段结果 |
| prompt 注入 | `recall(query, session_id=..., thread_id=..., manual_context=...)`，使用返回的 `rendered` 或 `snippets` |
| 工具读取 | `search(query, max_results=..., corpus=...)` 返回 `hits`；把实际返回的 path 交给 `get(path, start=..., lines=...)` |
| 关闭 | 宿主先停止/等待自己创建的后台任务，再释放自己持有的 backend、checkpoint 和文件索引资源 |

`Memory.search()` 检索归档会话消息，`Memory.recall()` 返回事实的 leaf 投影，不能与 Service 的
多源工具搜索或 prompt 召回混为一谈。Service 的 `recall()` 直接使用 prompt pipeline；Runtime 的
`recall` 配置主要服务工具搜索，不会被该方法整体转发，也不会自动把 host 文件加入 prompt 召回。

手动 USER.md/MEMORY.md 治理是可选接线：构造 Service 时提供 `manual_documents_provider`，
通过 `set_manual_review_scheduler()` 注册立即返回的排队回调；宿主 worker 调用同步的
`review_new_memory(max_atoms=20)`。不要在 scheduler 内直接等待审核。显式核对使用
`reconcile_manual_documents()`；`manual_context` 则是实际聊天快照，两者不可互相替代。
公共后台入口从 1.0.3 可用，详细状态与版本校验见 [手动文档治理](agent/PROJECT_MAP.md#手动文档治理)。

## JSON-RPC bridge

基础安装即提供 `octopmemory-bridge`，无需 CLI extra；先运行 `octopmemory-bridge --probe`
确认 SQLite/FTS5 环境。跨进程调用示例：

```bash
octopmemory-bridge --namespace demo --db-path /path/to/demo.sqlite
```

stdin/stdout 每行一个 JSON 对象，日志写 stderr。例如依次发送：

```json
{"jsonrpc":"2.0","id":1,"method":"handshake","params":{"client_version":"my-agent"}}
{"jsonrpc":"2.0","id":2,"method":"memory_search","params":{"query":"Python","maxResults":5,"corpus":"all"}}
```

响应包含同一 id 和 `result` 或 `error`。协议/参数错误使用 JSON-RPC error；
模型提炼不可用可能在正常 result 的 `failure_reason` 中报告，不能只看是否有 error。
常用方法为 `capture`、`extract`、`promote`、`memory_search`、`memory_get`、`stats`、`reindex`、
`reconcile_manual_documents` 与 `manual_document_review_status`；字段以
[dispatch / handlers](../src/octop_memory/adapters/bridge/handlers.py) 和
[Runtime](../src/octop_memory/application/runtime.py) 为准，不承诺与 Service 方法名一一对应。
提炼分批与进度持久化复用 [Runtime 契约](agent/PROJECT_MAP.md#提炼与增量进度)。

## 当前适配

| | OpenClaw | Hermes |
|---|---|---|
| 插件 | npm `@octop-memory/openclaw` | native provider（本仓 `plugins/hermes/octopmemory`）|
| 接入 | TypeScript → stdio `octopmemory-bridge` | Python provider → 进程内 `Bridge` |
| Python 环境 | 独立 uv/pipx 环境，bridge 路径必须可用 | 必须装进 Hermes 实际使用的解释器 |
| 存储 | 默认 SQLite，可配置 PostgreSQL | 当前 provider 构造 SQLite |
| 自动提炼触发 | `agent_end` capture 后，在开关启用且配置 `llm.endpoint` 时调用 | 当前 `sync_turn()` 只 capture，不自动调用 extract |
| 默认 namespace | `openclaw__default` | `hermes__default` |
| 配置 | `openclaw.json` 中 `plugins.entries.octopmemory.config` | `$HERMES_HOME/octopmemory.json` |

Python 要求 3.12+，SQLite 需要 FTS5。宿主兼容要求以
[OpenClaw manifest](../plugins/openclaw/octopmemory/package.json) 和 Hermes provider contract 为准；
Hermes 适配要求具备 `MemoryProvider` 接口的 v0.10.0+。安装说明不代表所有后续宿主版本均已验收。

## OpenClaw

### 安装与激活

```bash
uv tool install 'octop-memory[cli]'
octopmemory-bridge --probe
openclaw plugins install @octop-memory/openclaw
octop-memory openclaw setup
openclaw gateway restart
octop-memory openclaw doctor
```

也可用 `pipx install 'octop-memory[cli]'`。probe 应返回 `ok: true` 与 `fts5: true`。
首次 setup 用 Python CLI：插件尚未取得 memory slot 时，其 `openclaw octopmemory` 子命令可能还未注册。
setup 配置 `plugins.slots.memory=octopmemory`、conversation access、namespace 和 bridge 路径。
单独在任意目录执行 npm install 不等于将插件注册到宿主。

```bash
octop-memory openclaw setup --profile proactive --namespace my_project
octop-memory openclaw setup --db-path /path/to/memory.sqlite
octop-memory openclaw setup --bridge-python /path/to/python
octop-memory openclaw print-config --profile privacy
```

setup 默认数据位于 `~/.openclaw/octopmemory/<namespace>/memory.sqlite`；自定义 OpenClaw home 时随其变化。
未显式配置路径的 bridge 才回退到 `~/.octopmemory/<namespace>/memory.sqlite`，以实际生成的配置为准。
`bridge.command` 优先于 `bridge.python`，前者由 setup 定位 console script。
切换 PostgreSQL 时在 bridge 的 Python 环境安装 `[postgres]`，设置 `backend: "postgres"`，
并向启动插件的进程注入 `OCTOP_MEMORY_DSN`；不要把含密码的 DSN 放入会经 argv 转发的 JSON 配置。

### 配置与 profile

配置顺序为 profile 默认值 < 显式 override。不要把旧 profile 表当作运行时配置真源；
完整字段、defaults 和合并逻辑见 [config.ts](../plugins/openclaw/octopmemory/src/config.ts)，
宿主校验见 [openclaw.plugin.json](../plugins/openclaw/octopmemory/openclaw.plugin.json)。

| Profile | 主要用途 |
|---|---|
| `balanced` | 默认工具提示、原始对话 capture，raw 作 fallback |
| `low_latency` | 减少搜索来源与捕获范围 |
| `proactive` | `hybrid` 自动召回；依赖宿主调用 prefetch hook |
| `privacy` | 收紧来源，raw 正文替换为占位文本，仍可保留 metadata |
| `archive` | 更完整的对话留存，包括更多 tool 内容 |
| `eval` | 用于合成评测，避免短消息过滤影响 fixture |

常用 override（合并到 `plugins.entries.octopmemory.config`）：

```json
{
  "profile": "balanced",
  "namespace": "my_project",
  "recall": {"raw_policy": "fallback", "default_max_results": 5},
  "capture": {"include_roles": ["user", "assistant"], "include_tool_results": false},
  "privacy": {"redact_secrets": true, "store_tool_payloads": false}
}
```

| 配置组 | 关键字段与语义 |
|---|---|
| `recall` | `mode`: `off` / `tool_hint` / `hybrid`；`default_corpus`: `all` / `memory` / `sessions` / `wiki` |
| `recall` | `raw_policy`: `never` / `fallback` / `always`；`host_files_policy`: `off` / `include` / `only` |
| `recall` | `default_max_results`、`max_prompt_chars`、`layer_order`、`citation_policy` 控制数量、展示预算和排序 |
| `capture` | `agent_end_hook`、`include_roles`、`min_message_chars`、`include_tool_calls/results` 控制入库范围 |
| `capture` | `skip_memory_echo` 过滤已注入片段；`host_files_watcher` 控制 Markdown 索引 |
| `privacy` | `redact_secrets`、`redact_patterns`、`store_raw_content`、`store_tool_payloads` |
| `llm` | `endpoint`、`model`、`model_heavy`、`api_key_env`、timeout/retries；优先环境变量，避免明文 key 经 argv 转发 |
| `extraction` | `promote`、`regen_pages`、`max_candidates`、`page_regen_limit`；Python Runtime/Hermes 另支持 `max_tokens`，字段以各宿主 schema 为准 |
| `compaction` | 控制宿主上下文 flush 阈值与后续索引；不等于数据库 VACUUM |
| `bridge` | `command` / `python`、`log_level`、`spawn_timeout_ms` |

`recall.mode=off` 只关闭 prompt section，不自动关闭 capture 或删除历史。
`tool_hint` 提示模型使用工具；`hybrid` 在 host 调用 `recallPrefetcher` 后渲染结果。
OpenClaw 的 `capture.extract_on_agent_end` 还需要配置 `llm.endpoint` 才触发自动提炼；无模型时只捕获 L0。
旧字段 `recall.limit` / `total_chars` 保留兼容，新配置使用 `default_max_results` / `max_prompt_chars`。

## Hermes

### 安装与激活

Hermes 的插件安装器支持 `owner/repo/path/to/plugin` 子目录规格，可以直接从本仓安装 provider，
不需要独立的插件仓库：

```bash
hermes plugins install TencentCloud/octop-memory/plugins/hermes/octopmemory \
  --ref <完整 40 位 commit SHA>
hermes memory setup
hermes gateway restart
hermes memory status
```

`--ref` 需要完整的 40 位 commit SHA（短 SHA 会被宿主拒绝），用来把插件钉在验证过的 revision 上；
安装来源与 revision 会记录在 `~/.hermes/plugins/.install-metadata.json`。
在 `hermes memory setup` 中选择 `octopmemory`，宿主会安装 `plugin.yaml` 声明的 `octop-memory[cli]` 依赖；
随后用 `hermes memory status` 确认加载，并用 `hermes octopmemory search "Python" -n 5 --corpus all`
搜索测试对话中的词完成验收。

依赖安装失败时，先确定网关实际使用的解释器：`hermes --print-runtime-command` 返回的第一个参数
才是真正的 Python，依赖必须装进该解释器；不要从 `hermes` 命令的 shebang 猜测，它可能是 shell wrapper。
在该解释器中安装 `octop-memory[cli]` 后再重启网关。

仓库没有向 PyPI 发布独立的 `octop-memory-hermes` 插件包（按该命令执行会 404）；
`plugins/hermes/octopmemory/installer.py` 是仓库内 E2E 验证使用的 copy + 激活工具，不属于发布的安装路径。
维护者也可用 `scripts/build-hermes-plugin.sh` 生成独立插件目录另行分发。本仓不假定插件已被上游内置收录。

### 配置和文件

| 路径 | 用途 |
|---|---|
| `$HERMES_HOME/config.yaml` | 选择 `memory.provider` |
| `$HERMES_HOME/octopmemory.json` | 插件配置；省略时使用默认值 |
| `$HERMES_HOME/octopmemory/memory.sqlite` | 默认数据文件 |
| `$HERMES_HOME/memories/` | 默认 host Markdown root |

`hermes memory setup` 调用 provider 的 `get_config_schema()` / `save_config()`；点号 key 会转换为嵌套配置，
并深合并已有文件。配置支持 profile、namespace、db_path、recall/capture/privacy、llm/extraction 与 host files，
实际字段见 [provider 源码](../plugins/hermes/octopmemory/__init__.py)。不要将 OpenClaw 的 TS profile 默认值照搬到 Hermes。
当前 Hermes/Python Runtime 的 `profile` 只是保留的配置标签，不会展开 OpenClaw 的 profile 策略；
需要隐私或低延迟策略时，显式设置 `recall`、`capture`、`privacy` 等字段，并检查 status 中的有效配置。

```json
{
  "namespace": "hermes__default",
  "recall": {"default_max_results": 5, "raw_policy": "fallback"},
  "capture": {"min_message_chars": 0, "include_roles": ["user", "assistant"]},
  "privacy": {"redact_secrets": true, "store_tool_payloads": false},
  "llm": {"base_url": "https://<openai-compatible>/v1", "model": "<model>", "model_heavy": "<model>"},
  "extraction": {"promote": true, "regen_pages": true}
}
```

`llm` / `extraction` 块按 bridge 的 `--config-json` 语义转发给运行时。有效的 `endpoint` 或
`base_url` 及 `model` 可启用模型客户端；未配置或配置被拒绝时回退到 `NoopLLMClient`，
显式提炼返回 `failure_reason`，不影响 raw 捕获。`extraction` 控制 `promote`、`regen_pages`、
`max_candidates`、`max_tokens` 与 `page_regen_limit`。

当前 provider 的 `sync_turn()` 只在后台调用 capture，`on_memory_write()` / `on_pre_compress()`
也只写 raw；这些 hook 不自动调用 `extract`。仅填入模型配置不会自行生成 Candidate/Atom。
需要提炼时由额外宿主调度调用共享 Runtime/Bridge 的 `extract`；
provider 没有另设自动提炼开关。Hermes 的 `prefetch()` 直接调用 prompt recall，
工具的 `recall.raw_policy` 等配置不因此自动成为 prefetch 参数。
连接凭据优先用环境变量：不设 `api_key` 时读取 `api_key_env` 指定的变量（默认 `OCTOPMEMORY_LLM_API_KEY`），
避免把明文 key 写入共享的配置文件。

## 共同验证与排错

先在测试环境完成几轮合成对话，再搜索实际写入的词：

```bash
openclaw octopmemory status
openclaw octopmemory search "Python"
hermes octopmemory status
hermes octopmemory search "Python" -n 5 --corpus all
```

用搜索返回的 path 调用 `memory_get` 或宿主 `show`；不要自行猜测 atom ID。
检查命中层、正文、namespace，再检查模型 prompt 是否使用这些结果。

| 现象 | 检查顺序 |
|---|---|
| bridge 启动失败 | probe 的 FTS5、有效 Python 路径、依赖、handshake 日志 |
| Hermes import 失败 | `hermes --print-runtime-command` 返回的实际解释器是否能 import，而非终端默认 Python |
| 插件未加载 | memory slot/provider、安装目录、重启和宿主 status |
| 没有 raw | conversation access、capture 开关、roles、短消息阈值、privacy、实际 db/namespace |
| 没有 atom | 有效 LLM、实际是否触发 extract、`failure_reason` 和 Candidate 状态；Hermes `sync_turn()` 本身不触发提炼 |
| 工具能搜但 prompt 没注入 | OpenClaw recall.mode、host 是否调用 prefetch；不要用 doctor 成功推断自动注入 |
| host 文件没命中 | `host_files_root`、watcher、allowlist；默认不扫描整个工作目录 |

启用 host 文件索引后，默认扫描根目录 `MEMORY.md`、`USER.md`、`DREAMS.md`，
符合日期命名规则的 `memory/*.md`（含带 slug 的日记），以及 `topics/*.md` / `projects/*.md`。
自定义 allowlist 需安全相对 Markdown glob，不扫描整个 workspace。文件命中使用 SQLite 索引快照，
即使 memory backend 为 PG，该文件索引仍是独立 SQLite；索引命中不代表已完成手动文档语义审核。
迁移使用核心 CLI 的 `portable list-sources / pack / adopt / doctor`；导出的 `.hmpkg` 含记忆数据，不提交到仓库。

## 修改适配器

共用业务逻辑放 `application/`；宿主层只转换 hook/tool contract。现有 OpenClaw/Hermes 适配暴露
`memory_search(query, maxResults?, minScore?, corpus?)` 与 `memory_get(path, from?, lines?, corpus?)`。
搜索结果含 `hits[]`，路径读取返回 `path/excerpt`；corpus 和字段以实际工具 schema 为准。

Hermes 的 `initialize` 必须接受 `hermes_home`；provider 方法和 manifest 事件 hook 是两类接口，不能混放。
`sync_turn` 负责后台 capture，`prefetch` 同步召回，`queue_prefetch` 当前是 no-op；
当前 `shutdown()` 对每个线程最多等待 5 秒并停止文件索引，不保证慢任务已全部结束，也不主动关闭 Memory backend。
[plugin.yaml](../plugins/hermes/octopmemory/plugin.yaml) 和 [contract tests](../tests/test_hermes_contract.py)
共同维护接口。fallback stub 只能支撑本仓单测，真实 Hermes 兼容性仍需 E2E。

```bash
uv run pytest tests/test_hermes_contract.py tests/test_hermes_adapter.py tests/test_hermes_install.py -q
HERMES_SOURCE=/path/to/hermes-agent bash scripts/e2e_hermes_test.sh
npm ci --prefix plugins/openclaw/octopmemory
npm test --prefix plugins/openclaw/octopmemory
npm run typecheck --prefix plugins/openclaw/octopmemory
npm run build --prefix plugins/openclaw/octopmemory
bash scripts/e2e_openclaw_test.sh
```

E2E 可能安装或启动宿主，使用隔离环境并阅读脚本；缺少真实 host 导致的 skip 不算兼容性通过。

## 更名与已有安装迁移

项目统一使用 `octop-memory`。Python import 为 `octop_memory`，主命令为 `octop-memory`，
bridge 为 `octopmemory-bridge`，OpenClaw/Hermes 插件 ID 为 `octopmemory`。
这是入口名称的破坏性变更，不提供旧命令、import 或环境变量别名；磁盘协议兼容范围见下文。

如果从旧项目升级，先停止宿主和所有数据库写入者，备份配置及数据库，再修改安装和启动配置：

| 配置项 | 当前名称 |
|---|---|
| 发行包 / Python import | `octop-memory` / `octop_memory` |
| bridge / Hermes 插件 | `octopmemory-bridge` / 本仓 `plugins/hermes/octopmemory` |
| OpenClaw 包 / 插件 ID | `@octop-memory/openclaw` / `octopmemory` |
| 环境变量前缀 | `OCTOP_MEMORY_*` / `OCTOPMEMORY_*` |
| 本地数据目录 | `~/.octop-memory/` / `~/.octopmemory/` |
| 宿主插件数据目录 | `~/.openclaw/octopmemory/` / `$HERMES_HOME/octopmemory/` |
| 部署目录参数 | `OCTOP_MEMORY_DIR` / `--octop-memory-dir` |
| Hermes 配置文件 | `$HERMES_HOME/octopmemory.json` |
| Python Agent 宿主 | `octop-harness` |

- 安装新发行包后更新 Python import、bridge 路径、OpenClaw entries/slot/allow/install 配置和
  Hermes provider。迁移旧插件配置到新 ID，重新运行 setup/doctor；旧包需要自行卸载，避免宿主重复加载。
- SQLite 数据库结构、namespace、`hm_*` 内部表、`harness-checkpoint-v1` 编码和 `.hmpkg`
  文件格式保持不变。可以通过显式 `db_path` 继续读取原数据库，也可以停机后备份并迁移整个数据目录；
  不要只复制仍在写入的 `.sqlite` 文件而遗漏 WAL。Chroma 的目录也需迁移或显式指定。
- PostgreSQL 新共享 schema 为 `octop_memory`。使用旧数据库时保持原 DSN，在备份、停机并确认
  新 schema 尚不存在后，由数据库所有者核实旧 schema 的实际名称，再执行
  `ALTER SCHEMA "<旧 schema 的实际名称>" RENAME TO octop_memory;`（先替换占位符）。
  已存在两个 schema 时不要覆盖，需要单独核对并合并数据。默认 DSN 数据库名也改为 `octop_memory`；
  数据库本身不要求改名，可通过 `OCTOP_MEMORY_DSN` 显式配置原数据库。
- portable 的 Agent 默认扫描路径为 `~/.octop/agents/*/memory.sqlite` 和
  `~/.octop-harness/*/memory.sqlite`。旧目录不再自动扫描；可迁移到新位置，或在 Python
  `list_sources(extra_paths=[("agent", "/path/to/previous/*/memory.sqlite")])` 中显式补充扫描路径。
- 新默认路径/schema 不会自动发现旧位置；安装新包不会自动搬移数据库，已有安装应先完成迁移再启动。
