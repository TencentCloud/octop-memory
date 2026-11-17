# 给 AI 的接入说明（把这一页 + token 交给 AI 即可）

你要做的是：把用户这台机器上的编码终端接到 octop-memory。**只需要两个输入**：

- `OCTOP_URL`：MCP 地址，形如 `https://<octop-host>/mcp/memory/`（本地实例是 `http://127.0.0.1:8088/mcp/memory/`）
- `OCTOP_TOKEN`：用户级 MCP token，形如 `omcp_…`（用户在 Octop「个性化 → 记忆 → 终端接入」创建）

## 规则（必须遵守）

1. 不要把 token 写进任何仓库、脚本文件或提交里；只通过命令行参数/环境变量传给安装器
2. 不要 `echo`/打印 token，也不要写进日志
3. 安装器会写 `~/.claude/hooks-events/config.json` 等文件（权限 0600），不要把它复制进项目目录
4. 只运行下面与当前系统对应的命令；不要自己改 hook 配置或手写 `settings.json`

## macOS / Linux

```sh
curl -fsSL https://raw.githubusercontent.com/TencentCloud/octop-memory/main/plugins/harness/install.sh \
  | sh -s -- --url "$OCTOP_URL" --token "$OCTOP_TOKEN"
```

或从本地仓库：`sh plugins/harness/install.sh --url "$OCTOP_URL" --token "$OCTOP_TOKEN"`

- 只装部分终端：追加 `--terminals claude-code,codex`
- 装到某个项目：追加 `--project "$PWD"`

依赖：`uvx`（推荐）或 `pipx` 或 `python3`（脚本会用临时 venv 装安装器）。

## Windows

```powershell
irm https://raw.githubusercontent.com/TencentCloud/octop-memory/main/plugins/harness/install.ps1 -OutFile install.ps1
.\install.ps1 -Url $env:OCTOP_URL -Token $env:OCTOP_TOKEN
```

依赖：`uvx` / `pipx` / `python`（脚本会用 `%TEMP%` 下的临时 venv）。

## 验证（AI 自己做完要报告结果）

```sh
# 召回日志：应出现 hits=N http=200
tail -n 5 ~/.claude/hooks-events/octop-hook.log    # macOS/Linux
# Windows: Get-Content $env:USERPROFILE\.claude\hooks-events\octop-hook.log -Tail 5
```

- 让用户重开终端 / IDE（Codex 首次需要在 Codex 里 trust hook；DSH 需要重启）
- 让用户问一句话，然后在 Octop「终端接入」页看 token 的「接入端」是否出现对应客户端
- 若日志是 401：token 无效/被吊销；403：该接入端被禁用；404：服务端没开 `OCTOP_MEMORY_MCP_ENABLED`

## 卸载

```sh
sh plugins/harness/uninstall.sh [--terminals a,b] [--project <dir>]
# Windows: .\uninstall.ps1 -Terminals a,b
```

## 完成后向用户汇报

- 安装了哪些终端
- 是否通过验证（召回/上报日志）
- 任何需要用户手动做的步骤（重启 IDE、Codex trust hook、DSH 重启）
