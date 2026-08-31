param(
    [string]$SinceUtc = '2026-08-31 02:35:00 UTC',
    [string]$UntilUtc = '2026-08-31 03:00:00 UTC',
    [ValidateRange(1, 5000)]
    [int]$Lines = 1000,
    [string]$DeploymentConfigPath = "$env:USERPROFILE\.codex\skills\lucygetup-server-ops\config\deployment.json"
)

$ErrorActionPreference = 'Stop'

function ConvertTo-ProcessArgument {
    param([string]$Value)
    if ($Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '\\(?=("|$))', '\\' -replace '"', '\"') + '"'
}

function Invoke-NativeProcess {
    param([string]$FileName, [string[]]$Arguments)
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FileName
    $psi.UseShellExecute = $false
    if ($null -ne $psi.ArgumentList) {
        foreach ($argument in $Arguments) { [void]$psi.ArgumentList.Add($argument) }
    }
    else {
        $psi.Arguments = ($Arguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $psi
    [void]$process.Start()
    try {
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "$FileName failed with exit code $($process.ExitCode)"
        }
    }
    finally {
        $process.Dispose()
    }
}

$config = Get-Content -LiteralPath $DeploymentConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$hostAddress = [string]$config.host.address
$hostUser = [string]$config.host.user
$hostPassword = [string]$config.host.password
$sshPort = [int]$config.host.sshPort
if ([string]::IsNullOrWhiteSpace($hostPassword)) {
    throw 'SSH password is missing from the deployment configuration.'
}
if ([string]::IsNullOrWhiteSpace($SinceUtc) -or [string]::IsNullOrWhiteSpace($UntilUtc)) {
    throw 'SinceUtc and UntilUtc are required.'
}
if ($SinceUtc -match "'" -or $UntilUtc -match "'") {
    throw 'Unsafe journal time range.'
}

$sshExe = "$env:WINDIR\System32\OpenSSH\ssh.exe"
if (-not (Test-Path -LiteralPath $sshExe)) {
    $sshExe = (Get-Command ssh.exe -ErrorAction Stop).Source
}
$tempBase = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\', '/')
$askpass = [System.IO.Path]::GetFullPath((Join-Path $tempBase ("blan-portfolio-logs-{0}.cmd" -f [guid]::NewGuid().ToString('N'))))
if (-not $askpass.StartsWith($tempBase + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe temporary path: $askpass"
}

$oldAskpass = $env:SSH_ASKPASS
$oldAskpassRequirement = $env:SSH_ASKPASS_REQUIRE
$oldDisplay = $env:DISPLAY
try {
    Set-Content -LiteralPath $askpass -Value "@echo $hostPassword" -Encoding ASCII
    $env:SSH_ASKPASS = $askpass
    $env:SSH_ASKPASS_REQUIRE = 'force'
    $env:DISPLAY = '1'
    $remoteCommand = "journalctl -u lucygetup-portfolio.service --since '$SinceUtc' --until '$UntilUtc' --no-pager -o short-iso | tail -n $Lines"
    Invoke-NativeProcess $sshExe @(
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=NUL',
        '-o', 'ConnectTimeout=15',
        '-o', 'PreferredAuthentications=password',
        '-o', 'PubkeyAuthentication=no',
        '-p', $sshPort.ToString(),
        "$hostUser@$hostAddress",
        $remoteCommand
    )
}
finally {
    $env:SSH_ASKPASS = $oldAskpass
    $env:SSH_ASKPASS_REQUIRE = $oldAskpassRequirement
    $env:DISPLAY = $oldDisplay
    if (Test-Path -LiteralPath $askpass) {
        Remove-Item -LiteralPath $askpass -Force
    }
}
