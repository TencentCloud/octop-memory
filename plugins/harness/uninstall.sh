#!/usr/bin/env sh
# 卸载 octop-memory 终端接入：./uninstall.sh [--terminals a,b] [--project <dir>]
set -eu
REPO="${OCTOP_MEMORY_REPO:-https://github.com/TencentCloud/octop-memory}"
REF="${OCTOP_MEMORY_REF:-main}"
SPEC="git+${REPO}@${REF}#subdirectory=plugins/harness"
TERMINALS=""; PROJECT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --terminals) TERMINALS="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done
if command -v uvx >/dev/null 2>&1; then
  exec uvx --from "$SPEC" octop-memory-harness uninstall --terminals "$TERMINALS" --project "$PROJECT"
fi
if command -v pipx >/dev/null 2>&1; then
  exec pipx run --spec "$SPEC" octop-memory-harness uninstall --terminals "$TERMINALS" --project "$PROJECT"
fi
VENV="${TMPDIR:-/tmp}/octop-memory-harness-venv"
[ -x "$VENV/bin/octop-memory-harness" ] || { echo "先运行 install.sh" >&2; exit 1; }
exec "$VENV/bin/octop-memory-harness" uninstall --terminals "$TERMINALS" --project "$PROJECT"
