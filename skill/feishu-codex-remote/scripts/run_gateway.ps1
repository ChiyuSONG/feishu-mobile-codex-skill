$ErrorActionPreference = 'Stop'
$SkillRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $env:LOCALAPPDATA 'CodexFeishuRemote\.venv\Scripts\python.exe'
$Gateway = Join-Path $PSScriptRoot 'remote_gateway.py'
$LogDir = Join-Path $env:LOCALAPPDATA 'CodexFeishuRemote\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

while ($true) {
    $Stamp = Get-Date -Format o
    "[$Stamp] supervisor starting gateway" | Add-Content -LiteralPath (Join-Path $LogDir 'supervisor.log') -Encoding UTF8
    & $Python $Gateway run
    $Code = $LASTEXITCODE
    $Stamp = Get-Date -Format o
    "[$Stamp] gateway exited with code $Code; restarting in 15 seconds" | Add-Content -LiteralPath (Join-Path $LogDir 'supervisor.log') -Encoding UTF8
    Start-Sleep -Seconds 15
}
