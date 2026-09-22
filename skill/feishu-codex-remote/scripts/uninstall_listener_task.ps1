param(
    [string]$TaskName = "CodexFeishuRemoteGateway",
    [Parameter(Mandatory = $true)]
    [string]$ExpectedRunner
)

$ErrorActionPreference = "Stop"
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Task) {
    [pscustomobject]@{ Removed = $false; TaskName = $TaskName } | ConvertTo-Json -Compress
    exit 0
}

$ExpectedPowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$ExpectedPythonw = Join-Path $env:LOCALAPPDATA "CodexFeishuRemote\.venv\Scripts\pythonw.exe"
$ExpectedSupervisor = Join-Path (Split-Path -Parent $ExpectedRunner) "supervisor.py"
$Actions = @($Task.Actions)
$LegacyAction = $Actions.Count -eq 1 -and [string]$Actions[0].Execute -eq $ExpectedPowerShell -and [string]$Actions[0].Arguments -eq "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ExpectedRunner`""
$SilentAction = $Actions.Count -eq 1 -and [string]$Actions[0].Execute -eq $ExpectedPythonw -and [string]$Actions[0].Arguments -eq "`"$ExpectedSupervisor`""
if (-not ($LegacyAction -or $SilentAction)) {
    throw "Existing task is not owned by Feishu Remote Codex; refusing to remove it"
}

if ($Task.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
[pscustomobject]@{ Removed = $true; TaskName = $TaskName } | ConvertTo-Json -Compress
