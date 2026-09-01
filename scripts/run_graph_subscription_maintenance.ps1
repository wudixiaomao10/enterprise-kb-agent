$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = "D:\Anaconda\envs\enterprise-kb-agent\python.exe"
$maintenanceScript = Join-Path $PSScriptRoot "maintain_graph_subscriptions.py"
$logDirectory = Join-Path $projectRoot ".codex-logs"
$logPath = Join-Path $logDirectory "graph-subscription-maintenance.log"

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "enterprise-kb-agent Python not found at $pythonPath"
}

$timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$output = & $pythonPath $maintenanceScript 2>&1
$exitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
Add-Content -LiteralPath $logPath -Value "[$timestamp] exit=$exitCode"
Add-Content -LiteralPath $logPath -Value ($output | Out-String)

if ($exitCode -ne 0) {
    throw "Graph subscription maintenance failed with exit code $exitCode"
}

$output
