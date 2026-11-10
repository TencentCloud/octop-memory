#!/usr/bin/env sh
# octop-memory 终端接入（macOS / Linux）
#
#   ./install.sh --url https://<octop-host>/mcp/memory/ --token omcp_…
#
# 也支持环境变量：OCTOP_MEMORY_URL / OCTOP_MEMORY_TOKEN / OCTOP_TERMINALS / OCTOP_PROJECT_DIR
# 脚本只负责把安装器拉起来；token 不会写进仓库，只落在各终端 hook 目录的 config.json（0600）。
set -eu

REPO="${OCTOP_MEMORY_REPO:-https://github.com/TencentCloud/octop-memory}"
REF="${OCTOP_MEMORY_REF:-main}"
SPEC="git+${REPO}@${REF}#subdirectory=plugins/harness"

URL="${OCTOP_MEMORY_URL:-}"
TOKEN="${OCTOP_MEMORY_TOKEN:-}"
TERMINALS="${OCTOP_TERMINALS:-}"
PROJECT="${OCTOP_PROJECT_DIR:-}"

usage() {
  cat <<'USAGE'
用法: install.sh --url <MCP 地址> [--token <omcp_…>] [--terminals a,b] [--project <dir>]

  --url       必填，例如 https://octop.example.com/mcp/memory/
  --token     用户级 MCP token；也可用环境变量 OCTOP_MEMORY_TOKEN
  --terminals 逗号分隔：claude-code,codex,cursor,kiro,dsh（默认交互选择）
  --project   只装到这个项目目录（默认全局）
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --terminals) TERMINALS="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
done

if [ -z "$URL" ]; then echo "缺少 --url" >&2; usage; exit 1; fi
if [ -z "$TOKEN" ]; then echo "缺少 --token（或用 OCTOP_MEMORY_TOKEN）" >&2; usage; exit 1; fi

run_installer() {
  OCTOP_MEMORY_TOKEN="$TOKEN" "$@" install \
    --url "$URL" --token "$TOKEN" --terminals "$TERMINALS" --project "$PROJECT"
}

if command -v uvx >/dev/null 2>&1; then
  OCTOP_MEMORY_TOKEN="$TOKEN" uvx --from "$SPEC" octop-memory-harness install \
    --url "$URL" --token "$TOKEN" --terminals "$TERMINALS" --project "$PROJECT"
  exit 0
fi
if command -v pipx >/dev/null 2>&1; then
  OCTOP_MEMORY_TOKEN="$TOKEN" pipx run --spec "$SPEC" octop-memory-harness install \
    --url "$URL" --token "$TOKEN" --terminals "$TERMINALS" --project "$PROJECT"
  exit 0
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "需要 python3（或安装 uv: https://docs.astral.sh/uv/）" >&2
  exit 1
fi

VENV="${TMPDIR:-/tmp}/octop-memory-harness-venv"
if [ ! -x "$VENV/bin/octop-memory-harness" ]; then
  echo "· 创建临时虚拟环境: $VENV"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet "git+${REPO}@${REF}#subdirectory=plugins/harness"
fi
OCTOP_MEMORY_TOKEN="$TOKEN" "$VENV/bin/octop-memory-harness" install \
  --url "$URL" --token "$TOKEN" --terminals "$TERMINALS" --project "$PROJECT"
