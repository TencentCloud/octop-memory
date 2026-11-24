# 给编码助手的接入说明

## Codex 一次性接入提示词

把下面的提示词发给本机 Codex，并填上 Octop Memory 的 MCP 地址。凭据通过本机交给安装器。

> 请在本机为 Codex 接入 Octop Memory。MCP 地址是 `<MCP 地址>`。请使用 TencentCloud/octop-memory 仓库 `main` 分支的 `plugins/harness/install.sh`，Windows 用 `plugins/harness/install.ps1`。将该地址传给 `--url` 或 `-Url`，并通过 `--terminals codex` 或 `-Terminals codex` 选择 Codex。
> 优先使用为本次接入提供的 `OCTOP_MEMORY_TOKEN`。在我确认记忆空间一致时，可以复用本机 Codex 的接入凭据。需要新凭据时，请让我在本机终端隐藏输入一次。
> 使用读写 Token 时，这次用当前 Codex 模型提取记忆，安装前设置 `OCTOP_CODEX_EXTRACTION=codex`。如果我选择服务端提取，设置 `OCTOP_CODEX_EXTRACTION=server`。只读接入设置 `OCTOP_CODEX_READ_ONLY=1`。
> 装完就在发起安装的当前 Codex 对话中检查本次注册的 MCP 服务（包括按需加载的工具）。只读接入检查 `memory_get`，读写接入也检查 `memory_save`；选择当前 Codex 模型提取时，还要找到可调用的 `memory_propose`。只有本对话发现工具后才报告已加载。验证只需发现工具，避免为验证新增记忆或候选。
> 如果安装成功但本对话找不到预期工具，报告“已配置，当前对话待重载”。在桌面 Codex 按 ⌘K（Windows/Linux 按 Ctrl+K），搜索 MCP，打开 MCP 设置，点击“服务器”标题旁的重启图标。回到这段对话再次检查。若使用 CLI，在保存配置后退出并恢复原会话，再检查工具。若重载后仍不可用，报告实际错误。单独检查本机配置、服务端工具列表或新进程无法证明原对话已加载工具。

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/TencentCloud/octop-memory/main/plugins/harness/install.sh \
  | sh -s -- --url "$OCTOP_URL" --terminals codex
```

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/TencentCloud/octop-memory/main/plugins/harness/install.ps1 -OutFile install.ps1
.\install.ps1 -Url $env:OCTOP_URL -Terminals codex
```

把凭据通过 `OCTOP_MEMORY_TOKEN` 传给安装器。使用只读凭据时，同时设置 `OCTOP_CODEX_READ_ONLY=1`，安装器开放 `memory_get`，并停用会话上报。使用读写凭据时，Codex 也会加载 `memory_save`。

使用读写 Token 时，安装前可设置 `OCTOP_CODEX_EXTRACTION=codex`。Stop 上报本轮原文后，如果有可引用的用户消息，就让当前 Codex 对话模型续跑一轮，消耗 Codex 模型额度。模型可以用 `memory_propose` 引用一句用户原话，提交到待审核列表。用户明确要求长期保存时仍可用 `memory_save`。设置 `OCTOP_CODEX_EXTRACTION=server` 并重新安装可恢复服务端提取。安装器和服务端都需要支持候选提议。

在「设置 → Hooks」或 CLI 的 `/hooks` 审核并信任新钩子，即可启用自动召回和适用的会话上报。召回结果中的 `path` 可直接传给 `memory_get`。

选择 Octop 模型时，在「共享记忆」为本终端的读写 Token 保存模型。按页面提示设置 `OCTOP_CODEX_EXTRACTION=octop` 和 `OCTOP_CODEX_REVIEW_URL=<Octop 地址>/api/memory-mcp/codex-review` 后运行安装器。更换 Octop 模型后，下次复核使用新模型。切换到 Codex 会话模型时，按新的提示词重新安装。

## 其他终端

你要做的是：把用户这台机器上的编码终端接到 octop-memory。**只需要两个输入**：

- `OCTOP_URL`：MCP 地址，形如 `https://<memory-host>/mcp/`（本地实例是 `http://127.0.0.1:8790/mcp/`）
- `OCTOP_TOKEN`：该接入端的空间 token，形如 `omcp_…`（由空间管理员为该终端单独签发）

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

- 为每个终端分别签发 token 并运行安装器，例如 `--terminals codex`
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
- 让用户问一句话，然后在空间的接入端列表里检查对应 token 的最近访问时间
- 若日志是 401：token 无效或已撤销；403：检查空间绑定；404：检查 MCP 服务地址

## 卸载

```sh
sh plugins/harness/uninstall.sh [--terminals a,b] [--project <dir>]
# Windows: .\uninstall.ps1 -Terminals a,b
```

## 完成后向用户汇报

- 安装了哪些终端
- 是否通过验证（召回/上报日志）
- 任何需要用户手动做的步骤（重启 IDE、Codex trust hook、DSH 重启）
