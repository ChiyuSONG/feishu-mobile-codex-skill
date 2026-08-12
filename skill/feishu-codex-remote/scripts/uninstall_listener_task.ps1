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
$Actions = @($Task.Actions)
if ($Actions.Count -ne 1 -or
    [string]$Actions[0].Execute -ne $ExpectedPowerShell -or
    [string]$Actions[0].Arguments -notlike "*$ExpectedRunner*") {
    throw "Existing task is not owned by Feishu Remote Codex; refusing to remove it"
}

if ($Task.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
[pscustomobject]@{ Removed = $true; TaskName = $TaskName } | ConvertTo-Json -Compress
