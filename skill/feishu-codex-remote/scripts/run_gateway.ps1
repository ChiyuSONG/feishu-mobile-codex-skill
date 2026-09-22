$ErrorActionPreference = 'Stop'
$Python = Join-Path $env:LOCALAPPDATA 'CodexFeishuRemote\.venv\Scripts\python.exe'
# Compatibility entrypoint; the registered task uses pythonw.exe directly.
$Supervisor = Join-Path $PSScriptRoot 'supervisor.py'
$Info = New-Object System.Diagnostics.ProcessStartInfo
$Info.FileName = $Python
$Info.Arguments = "`"$Supervisor`""
$Info.UseShellExecute = $false
$Info.CreateNoWindow = $true
$Process = [System.Diagnostics.Process]::Start($Info)
$Process.WaitForExit()
exit $Process.ExitCode
