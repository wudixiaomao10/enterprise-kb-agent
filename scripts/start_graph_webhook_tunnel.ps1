param(
    [string]$SshHost = "ALiYun",
    [int]$RemotePort = 18010,
    [int]$LocalPort = 8010
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$stateDirectory = Join-Path $projectRoot ".codex-tmp\graph-webhook"
$logDirectory = Join-Path $projectRoot ".codex-logs"
$pidFile = Join-Path $stateDirectory "ssh-tunnel.pid"
$sshPath = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"

New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

if (-not (Test-Path -LiteralPath $sshPath)) {
    throw "OpenSSH client not found at $sshPath"
}

try {
    $health = Invoke-WebRequest `
        -UseBasicParsing `
        -Uri "http://127.0.0.1:$LocalPort/health" `
        -TimeoutSec 5
    if ($health.StatusCode -ne 200) {
        throw "Knowledge API health check returned HTTP $($health.StatusCode)"
    }
} catch {
    throw "Knowledge API is not healthy on 127.0.0.1:$LocalPort. $($_.Exception.Message)"
}

if (Test-Path -LiteralPath $pidFile) {
    $existingPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$existingPid)) {
        $existingProcess = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($existingProcess -and $existingProcess.ProcessName -eq "ssh") {
            Write-Output "Graph webhook tunnel is already running with PID $existingPid"
            exit 0
        }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

$forward = "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}"
$sshArguments = @(
    "-N",
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "StrictHostKeyChecking=yes",
    "-R", $forward,
    $SshHost
)
$stdoutLog = Join-Path $logDirectory "graph-webhook-tunnel.out.log"
$stderrLog = Join-Path $logDirectory "graph-webhook-tunnel.err.log"
$process = Start-Process `
    -FilePath $sshPath `
    -ArgumentList $sshArguments `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Start-Sleep -Seconds 2
if ($process.HasExited) {
    $details = Get-Content -LiteralPath $stderrLog -Raw -ErrorAction SilentlyContinue
    throw "Graph webhook tunnel failed to start. $details"
}

Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii
Write-Output "Graph webhook tunnel started with PID $($process.Id)"
