# octop-memory-harness

把本机终端接到 octop-memory MCP 的**客户端侧**安装器与 hooks。服务端（Octop）只提供
MCP 接口和 token；这个包负责在 Claude Code / Codex / Cursor / Kiro / DSH 里自动召回、
会话上报和 MCP 条目注册。

## 快速安装（一行）

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/TencentCloud/octop-memory/main/plugins/harness/install.sh \
  | sh -s -- --url https://<octop-host>/mcp/memory/ --token omcp_…
```

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/TencentCloud/octop-memory/main/plugins/harness/install.ps1 -OutFile install.ps1
.\install.ps1 -Url https://<octop-host>/mcp/memory/ -Token omcp_…
```

脚本优先用 `uvx`/`pipx`，都没有时用 `python3 -m venv` 建临时环境安装 `octop-memory-harness`，
token 只在运行时传递，不写进仓库。

要把「一串 token + 一页说明」直接丢给 AI 完成安装，用 [AI-SETUP.md](./AI-SETUP.md)。

## 手动安装

```bash
pip install octop-memory-harness
# 或在本仓库里：pip install ./plugins/harness
```

然后：

```bash
octop-memory-harness install \
  --url https://<octop-host>/mcp/memory/ \
  --token omcp_…                     # 用户级 MCP token，在 Octop 的「MCP 接入」页创建
```

- `--terminals claude-code,codex,cursor,kiro,dsh`：只装其中几个（默认交互选择，非交互模式读这个参数）
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
| Codex | `~/.codex/hooks/` | `~/.codex/config.toml` | 首次使用需在 Codex 里 trust 该 hook |
| Cursor | `~/.cursor/hooks/` | `~/.cursor/hooks.json` | 同上 |

DSH 原生插件在独立仓库发布：<https://github.com/ArvinQi/dsh-octop-memory>。

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
  "url": "https://<octop-host>/mcp/memory/",
  "token": "omcp_…",
  "tokenHeader": "Authorization",
  "client": "claude-code"
}
```

`client` 会以 `X-Octop-Client` 头发给服务端，用于 Dashboard 的「接入端」统计与按接入端禁用。

## 验证

```bash
# 召回：提问前 hook 会打一行 hits=… http=200
tail -f ~/.claude/hooks-events/octop-hook.log

# 上报：Stop 后应看到 imported=N http=200
tail -f ~/.claude/hooks-events/octop-hook.log
```

## 脱敏

安装器、hooks 与文档里不包含任何内部域名、员工姓名或实例凭据；token 只在运行时由用户传入。
