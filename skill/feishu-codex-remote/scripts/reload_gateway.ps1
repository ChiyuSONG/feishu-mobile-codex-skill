param(
    [int]$WaitTimeoutSeconds = 3600,
    [switch]$RequestOnly
)

$ErrorActionPreference = "Stop"
$TaskName = "CodexFeishuRemoteGateway"
$Runtime = Join-Path $env:LOCALAPPDATA "CodexFeishuRemote"
$Python = Join-Path $Runtime ".venv\Scripts\python.exe"
$Gateway = Join-Path $PSScriptRoot "remote_gateway.py"

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($Task.State -ne "Running") {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
}

if ($RequestOnly) {
    & $Python $Gateway reload --wait-timeout $WaitTimeoutSeconds --request-only
    exit $LASTEXITCODE
}

& $Python $Gateway reload --wait-timeout $WaitTimeoutSeconds
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# The Listener writes its acknowledgement before exiting. Give Task Scheduler
# time to observe that exit, then start it only if its restart policy has not.
Start-Sleep -Seconds 2
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($Task.State -ne "Running") {
    Start-ScheduledTask -TaskName $TaskName
}

$Deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} while ($Task.State -ne "Running" -and (Get-Date) -lt $Deadline)
if ($Task.State -ne "Running") {
    throw "Feishu Listener task did not restart after graceful reload"
}
