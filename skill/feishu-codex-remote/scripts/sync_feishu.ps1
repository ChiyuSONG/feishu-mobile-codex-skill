param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectKey,
    [int]$WaitTimeoutSeconds = 3600,
    [switch]$RequestOnly,
    [switch]$ReportUsage,
    [switch]$FirstInspectionMessage,
    [switch]$InspectionReport
)

$ErrorActionPreference = "Stop"
$EffectiveRequestOnly = $RequestOnly -or $ReportUsage -or $InspectionReport
$TaskName = "CodexFeishuRemoteGateway"
$Runtime = Join-Path $env:LOCALAPPDATA "CodexFeishuRemote"
$Python = Join-Path $Runtime ".venv\Scripts\python.exe"
$Gateway = Join-Path $PSScriptRoot "remote_gateway.py"

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
[xml]$TaskXml = Export-ScheduledTask -TaskName $TaskName
$TriggerCount = if ($TaskXml.Task.Triggers) {
    @($TaskXml.Task.Triggers.ChildNodes).Count
} else {
    0
}
if ($TriggerCount -ne 0) {
    throw "Feishu Listener launcher must be demand-start-only; run install_listener_task.ps1 to remove automatic triggers"
}
if ($Task.State -ne "Running") {
    Start-ScheduledTask -TaskName $TaskName
    $Deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    } while ($Task.State -ne "Running" -and (Get-Date) -lt $Deadline)
    if ($Task.State -ne "Running") {
        throw "Feishu Listener task did not enter Running state"
    }
}

$SyncArgs = @($Gateway, "sync", "--project-key", $ProjectKey, "--wait-timeout", $WaitTimeoutSeconds)
if ($EffectiveRequestOnly) {
    $SyncArgs += "--request-only"
}
if ($ReportUsage) {
    $SyncArgs += "--report-usage"
}
if ($FirstInspectionMessage) {
    $SyncArgs += "--first-inspection-message"
}
if ($InspectionReport) {
    $SyncArgs += "--inspection-report"
}
& $Python @SyncArgs
exit $LASTEXITCODE
