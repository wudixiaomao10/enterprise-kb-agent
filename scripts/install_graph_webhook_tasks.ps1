$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$tunnelScript = Join-Path $PSScriptRoot "start_graph_webhook_tunnel.ps1"
$maintenanceScript = Join-Path $PSScriptRoot "run_graph_subscription_maintenance.ps1"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

foreach ($scriptPath in @($tunnelScript, $maintenanceScript)) {
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw "Required task script not found: $scriptPath"
    }
}

$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$tunnelAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$tunnelScript`""
$tunnelTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask `
    -TaskName "Enterprise KB Graph Webhook Tunnel" `
    -Description "Keep the reverse SSH tunnel for Microsoft Graph webhooks healthy." `
    -Action $tunnelAction `
    -Trigger $tunnelTrigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

$maintenanceAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$maintenanceScript`""
$maintenanceTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask `
    -TaskName "Enterprise KB Graph Subscription Maintenance" `
    -Description "Reconcile and renew Microsoft Graph directory subscriptions." `
    -Action $maintenanceAction `
    -Trigger $maintenanceTrigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Start-ScheduledTask -TaskName "Enterprise KB Graph Webhook Tunnel"

Get-ScheduledTask `
    -TaskName "Enterprise KB Graph Webhook Tunnel", "Enterprise KB Graph Subscription Maintenance" |
    Select-Object TaskName, State
