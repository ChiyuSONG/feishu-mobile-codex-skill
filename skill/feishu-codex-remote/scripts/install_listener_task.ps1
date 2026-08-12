param(
    [string]$TaskName = "CodexFeishuRemoteGateway"
)

$ErrorActionPreference = "Stop"
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Runner = Join-Path $PSScriptRoot "run_gateway.ps1"

if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "Listener runner does not exist: $Runner"
}

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$WasRunning = $Existing -and $Existing.State -eq "Running"

if ($Existing) {
    $Actions = @($Existing.Actions)
    if ($Actions.Count -ne 1 -or
        [string]$Actions[0].Execute -ne $PowerShell -or
        [string]$Actions[0].Arguments -notlike "*$Runner*") {
        throw "Existing Listener task action is unexpected; refusing to replace it automatically"
    }

    # Updating the registered XML preserves a currently running instance while
    # removing every automatic trigger. The task remains demand-startable.
    [xml]$TaskXml = Export-ScheduledTask -TaskName $TaskName
    while ($TaskXml.Task.Triggers -and $TaskXml.Task.Triggers.HasChildNodes) {
        [void]$TaskXml.Task.Triggers.RemoveChild($TaskXml.Task.Triggers.FirstChild)
    }
    Register-ScheduledTask -TaskName $TaskName -Xml $TaskXml.OuterXml -Force | Out-Null
} else {
    $Action = New-ScheduledTaskAction `
        -Execute $PowerShell `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
    $Settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Days 3650)
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited
    $Definition = New-ScheduledTask -Action $Action -Settings $Settings -Principal $Principal
    Register-ScheduledTask `
        -TaskName $TaskName `
        -InputObject $Definition `
        -Description "On-demand Feishu Codex Listener launcher; owned by Codex Automation" `
        -Force | Out-Null
}

$Installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
[xml]$InstalledXml = Export-ScheduledTask -TaskName $TaskName
$TriggerCount = if ($InstalledXml.Task.Triggers) {
    @($InstalledXml.Task.Triggers.ChildNodes).Count
} else {
    0
}
if ($TriggerCount -ne 0) {
    throw "Listener task still has automatic triggers: $TriggerCount"
}
if ($WasRunning -and $Installed.State -ne "Running") {
    throw "Listener stopped while its task definition was migrated"
}

[pscustomobject]@{
    TaskName = $TaskName
    State = [string]$Installed.State
    TriggerCount = $TriggerCount
    DemandStartOnly = $true
} | ConvertTo-Json -Compress
