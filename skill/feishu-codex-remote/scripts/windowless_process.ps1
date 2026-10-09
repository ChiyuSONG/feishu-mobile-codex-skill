# One owner for hidden native launches and their actual exit status.
function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Invoke-WindowlessProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = $FilePath
    $Info.Arguments = (($ArgumentList | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $Info.UseShellExecute = $false
    $Info.CreateNoWindow = $true
    $Info.RedirectStandardOutput = $true
    $Info.RedirectStandardError = $true
    $Info.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $Info.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $Info
    try {
        if (-not $Process.Start()) { throw "Native process did not start" }
        # Drain both pipes concurrently. Waiting is required even when the
        # executable's launcher would make PowerShell's call operator return early.
        $OutTask = $Process.StandardOutput.ReadToEndAsync()
        $ErrTask = $Process.StandardError.ReadToEndAsync()
        $Process.WaitForExit()
        [Console]::Out.Write($OutTask.GetAwaiter().GetResult())
        [Console]::Error.Write($ErrTask.GetAwaiter().GetResult())
        return $Process.ExitCode
    } finally {
        $Process.Dispose()
    }
}
