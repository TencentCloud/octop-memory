# octop-memory-harness

把本机终端接到 octop-memory MCP 的**客户端侧**安装器与 hooks。独立记忆服务提供
MCP 接口和 token；这个包负责在 Claude Code / Codex / Cursor / Kiro / DSH 里自动召回、
会话上报和 MCP 条目注册。

## 快速安装（一行）

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/TencentCloud/octop-memory/main/plugins/harness/install.sh \
  | sh -s -- --url https://<memory-host>/mcp/ --token omcp_…
```

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/TencentCloud/octop-memory/main/plugins/harness/install.ps1 -OutFile install.ps1
.\install.ps1 -Url https://<memory-host>/mcp/ -Token omcp_…
```

脚本优先用 `uvx`/`pipx`，都没有时用 `python3 -m venv` 建临时环境安装 `octop-memory-harness`，
token 只在运行时传递，不写进仓库。

要交给本机 Codex 安装，复制 [一次性接入提示词](./AI-SETUP.md#codex-一次性接入提示词)，再通过本机提供凭据。其他终端的助手接入说明也在同一页。

## 手动安装

```bash
pip install octop-memory-harness
# 或在本仓库里：pip install ./plugins/harness
```

然后：

```bash
octop-memory-harness install \
  --url https://<memory-host>/mcp/ \
  --token omcp_…                     # 为该接入端签发的空间 token
```

- `--terminals codex`：选择一个终端。每个终端使用自己的 token，分别安装
- `--project /path/to/repo`：只注册到这个项目（hook 脚本和含 token 的 `config.json` 仍留在用户目录）
- 卸载：`octop-memory-harness uninstall [--terminals …] [--project …]`

安装器不内置任何凭据：token 由你在运行时传入，写进各终端 hook 目录下权限 `0600` 的
`config.json`，不会出现在仓库或命令行历史之外的地方。

## 五个终端

| 终端 | hooks 目录 | 注册位置 | 说明 |
|------|-----------|---------|------|
| Claude Code | `~/.claude/hooks-events/` | `~/.claude/settings.json` | UserPromptSubmit 召回 + Stop 上报 |
| Kiro | `~/.kiro/hooks/` | `~/.kiro/hooks/*.json` | 同上 |
| DSH | — | `~/.dsh/bundles/dsh-octop-memory/` + profile | 原生插件 `dsh-octop-memory`（npm 安装，进程内召回/上报），不装 hook 桥接 |
| Codex | `~/.codex/hooks/` | `~/.codex/config.toml`，hooks 可沿用现有 `hooks.json` | UserPromptSubmit 召回 + Stop 上报，MCP 开放 `memory_get` 和 `memory_save` |
| Cursor | `~/.cursor/hooks/` | `~/.cursor/hooks.json` | 同上 |

DSH 原生插件在独立仓库发布：<https://github.com/ArvinQi/dsh-octop-memory>。

## Codex MCP

安装器注册 Streamable HTTP MCP。`http_headers_helper` 从权限为 `0600` 的本机 hook 配置读取凭据，TOML 只保存端点和辅助命令。Agent 可用 `memory_get` 展开召回结果附带的 `path`，也可用 `memory_save` 保存用户明确要求记住的信息。

使用读写 Token 时，安装前可设置 `OCTOP_CODEX_EXTRACTION=codex`。Stop 上报本轮原文后，如果有可引用的用户消息，就让当前 Codex 对话模型续跑一轮，消耗 Codex 模型额度。每轮可以跳过或一次通过 `memory_propose(facts=[...])` 提交 1–3 条有原话依据的事实。每条正文只写一个可独立更新的事实、偏好或决定，另附对应用户消息的 ID 和原文短引。用户明确要求长期保存时仍可用 `memory_save`。设置 `OCTOP_CODEX_EXTRACTION=server` 并重新安装可恢复服务端提取。安装器和服务端都需要支持候选提议。

Codex 自提取会先调用 `memory_codex_related`；Octop 代提取从同一服务读取规则和相关记录。模型逐句判断意图与适用期限，只提交长期事实、偏好或要求。临时指令和纯提问跳过。重复陈述会给同一条记忆追加原话证据。明确改口会替换已确认的旧版，并保留历史；不确定的变更进入待审核。

使用只读凭据时设置 `OCTOP_CODEX_READ_ONLY=1`，仅开放 `memory_get` 并停用会话上报。安装后在「设置 → MCP Servers」重启连接，并用 `/mcp` 检查工具列表。在「设置 → Hooks」或 CLI 的 `/hooks` 审核并信任新钩子。

## DSH 版本兼容

DSH 原生插件 `dsh-octop-memory` 在 `package.json` 声明 `dsh.engines.dsh` / `peerDependencies`
（当前 `>= 0.2.0`，已在 `0.2.2-rc.1` 验证）。安装器拉取的版本会带上这个约束，DSH 版本过低时
插件不会加载，安装日志会提示。

## 对话记录怎么上报

服务端不再解析 transcript。Stop / stop hook 把新的字节交给本地
`lib/octop_ingest.py`（随安装器一起复制），由它按终端格式解析成
`user_message` / `assistant_message` 事件，再调用 `memory_capture(events=[...])`：

- 解析在客户端完成，服务端只有一条写入路径（`MemoryRuntime.capture`），脱敏、
  回声保护、长度限制统一生效
- 增量 offset 存在 hook 目录的 `octop-transcript-state.json`，只在 2xx 后推进
- 支持 Claude Code / Kiro / Codex / Cursor 四种 JSONL 格式（解析器在
  `octopmemory_harness/transcript.py`）

## 配置

`config.json` 形状（安装器写入）：

```json
{
  "url": "https://<memory-host>/mcp/",
  "token": "omcp_…",
  "tokenHeader": "Authorization"
}
```

服务端按签发的 token 记录用量。要禁用某个接入端，撤销它的 token，同一空间的其他终端可以继续使用。

## 验证

```bash
# 召回：提问前 hook 会打一行 hits=… http=200
tail -f ~/.claude/hooks-events/octop-hook.log

# 上报：Stop 后应看到 imported=N http=200
tail -f ~/.claude/hooks-events/octop-hook.log
```

## 脱敏

安装器、hooks 与文档里不包含任何内部域名、员工姓名或实例凭据；token 只在运行时由用户传入。
