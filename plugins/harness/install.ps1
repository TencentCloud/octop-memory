# octop-memory 终端接入（Windows PowerShell）
#
#   .\install.ps1 -Url https://<memory-host>/mcp/ -Token omcp_…
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

if (-not $Url) { throw '缺少 -Url（例如 https://memory.example.com/mcp/）' }
if (-not $Token) { throw '缺少 -Token（或在环境中设置 OCTOP_MEMORY_TOKEN）' }

$argsList = @('install', '--url', $Url, '--terminals', $Terminals, '--project', $Project)

$previousToken = $env:OCTOP_MEMORY_TOKEN
$env:OCTOP_MEMORY_TOKEN = $Token
try {
  if (Get-Command uvx -ErrorAction SilentlyContinue) {
    & uvx --from $Spec octop-memory-harness @argsList
    exit $LASTEXITCODE
  }
  if (Get-Command pipx -ErrorAction SilentlyContinue) {
    & pipx run --spec $Spec octop-memory-harness @argsList
    exit $LASTEXITCODE
  }

  $installKey = python -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:20])" $Spec
  if ($LASTEXITCODE -ne 0) { throw "Unable to identify the requested installer revision" }
  $venv = Join-Path $env:TEMP "octop-memory-harness-venv-$installKey"
  $exe = Join-Path $venv 'Scripts\octop-memory-harness.exe'
  if (-not (Test-Path $exe)) {
    Write-Host "· 创建临时虚拟环境: $venv"
    python -m venv $venv
    & (Join-Path $venv 'Scripts\python.exe') -m pip install --quiet --upgrade pip
    & (Join-Path $venv 'Scripts\python.exe') -m pip install --quiet $Spec
  }
  & $exe @argsList
  exit $LASTEXITCODE
} finally {
  $env:OCTOP_MEMORY_TOKEN = $previousToken
}
