#!/usr/bin/env sh
# 一行接入：下载并运行 octop-memory-harness 安装器（不内置任何凭据）。
#
#   curl -fsSL https://raw.githubusercontent.com/TencentCloud/octop-memory/main/plugins/harness/install.sh \
#     | sh -s -- --url https://<octop-host>/mcp/memory/ --token omcp_…
#
# 需要 uv（推荐）或 pipx；安装器会在运行时把 token 写进各终端 hook 目录的 config.json。
set -eu

REPO="${OCTOP_MEMORY_REPO:-https://github.com/TencentCloud/octop-memory}"
REF="${OCTOP_MEMORY_REF:-main}"
SPEC="git+${REPO}@${REF}#subdirectory=plugins/harness"

if command -v uvx >/dev/null 2>&1; then
  exec uvx --from "$SPEC" octop-memory-harness install "$@"
fi
if command -v pipx >/dev/null 2>&1; then
  exec pipx run --spec "$SPEC" octop-memory-harness install "$@"
fi

echo "需要 uv（https://docs.astral.sh/uv/）或 pipx 才能运行安装器" >&2
exit 1
