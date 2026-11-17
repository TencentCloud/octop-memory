# octop-memory 终端接入（Windows PowerShell）
#
#   .\install.ps1 -Url https://<octop-host>/mcp/memory/ -Token omcp_…
#
# 环境变量等价：OCTOP_MEMORY_URL / OCTOP_MEMORY_TOKEN / OCTOP_TERMINALS / OCTOP_PROJECT_DIR
param(
  [string]$Url = $env:OCTOP_MEMORY_URL,
  [string]$Token = $env:OCTOP_MEMORY_TOKEN,
  [string]$Terminals = $env:OCTOP_TERMINALS,
  [string]$Project = $env:OCTOP_PROJECT_DIR
)
$ErrorActionPreference = 'Stop'
$Repo = if ($env:OCTOP_MEMORY_REPO) { $env:OCTOP_MEMORY_REPO } else { 'https://github.com/TencentCloud/octop-memory' }
$Ref = if ($env:OCTOP_MEMORY_REF) { $env:OCTOP_MEMORY_REF } else { 'main' }
$Spec = "git+$Repo@$Ref#subdirectory=plugins/harness"

if (-not $Url) { throw '缺少 -Url（例如 https://octop.example.com/mcp/memory/）' }
if (-not $Token) { throw '缺少 -Token（或在环境中设置 OCTOP_MEMORY_TOKEN）' }

$argsList = @('install', '--url', $Url, '--token', $Token, '--terminals', $Terminals, '--project', $Project)

if (Get-Command uvx -ErrorAction SilentlyContinue) {
  & uvx --from $Spec octop-memory-harness @argsList
  exit $LASTEXITCODE
}
if (Get-Command pipx -ErrorAction SilentlyContinue) {
  & pipx run --spec $Spec octop-memory-harness @argsList
  exit $LASTEXITCODE
}

$venv = Join-Path $env:TEMP 'octop-memory-harness-venv'
$exe = Join-Path $venv 'Scripts\octop-memory-harness.exe'
if (-not (Test-Path $exe)) {
  Write-Host "· 创建临时虚拟环境: $venv"
  python -m venv $venv
  & (Join-Path $venv 'Scripts\python.exe') -m pip install --quiet --upgrade pip
  & (Join-Path $venv 'Scripts\python.exe') -m pip install --quiet $Spec
}
& $exe @argsList
exit $LASTEXITCODE
