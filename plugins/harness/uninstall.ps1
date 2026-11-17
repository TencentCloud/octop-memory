# 卸载 octop-memory 终端接入：.\uninstall.ps1 [-Terminals a,b] [-Project <dir>]
param(
  [string]$Terminals = $env:OCTOP_TERMINALS,
  [string]$Project = $env:OCTOP_PROJECT_DIR
)
$ErrorActionPreference = 'Stop'
$Repo = if ($env:OCTOP_MEMORY_REPO) { $env:OCTOP_MEMORY_REPO } else { 'https://github.com/TencentCloud/octop-memory' }
$Ref = if ($env:OCTOP_MEMORY_REF) { $env:OCTOP_MEMORY_REF } else { 'main' }
$Spec = "git+$Repo@$Ref#subdirectory=plugins/harness"
$argsList = @('uninstall', '--terminals', $Terminals, '--project', $Project)
if (Get-Command uvx -ErrorAction SilentlyContinue) { & uvx --from $Spec octop-memory-harness @argsList; exit $LASTEXITCODE }
if (Get-Command pipx -ErrorAction SilentlyContinue) { & pipx run --spec $Spec octop-memory-harness @argsList; exit $LASTEXITCODE }
$venv = Join-Path $env:TEMP 'octop-memory-harness-venv'
$exe = Join-Path $venv 'Scripts\octop-memory-harness.exe'
if (-not (Test-Path $exe)) { throw '先运行 install.ps1' }
& $exe @argsList
exit $LASTEXITCODE
