param(
    [string]$DeploymentConfigPath = "$env:USERPROFILE\.codex\skills\lucygetup-server-ops\config\deployment.json"
)

$ErrorActionPreference = 'Stop'

function ConvertTo-ShellSingleQuoted {
    param([string]$Value)
    return "'" + $Value.Replace("'", "'`"'`"'") + "'"
}

function ConvertTo-MsysPath {
    param([string]$Path)
    $fullPath = [System.IO.Path]::GetFullPath($Path).Replace('\', '/')
    if ($fullPath -match '^([A-Za-z]):/(.*)$') {
        return '/' + $matches[1].ToLowerInvariant() + '/' + $matches[2]
    }
    return $fullPath
}

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

$sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$config = Get-Content -LiteralPath $DeploymentConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$hostAddress = [string]$config.host.address
$hostUser = [string]$config.host.user
$hostPassword = [string]$config.host.password
$sshPort = [int]$config.host.sshPort
$portfolioRemotePath = [string]$config.portfolio.remotePath
$portfolioPort = [int]$config.portfolio.port
if ($portfolioRemotePath -ne '/opt/lucygetup-portfolio' -or $portfolioPort -ne 51327) {
    throw "Unexpected portfolio deployment target: $portfolioRemotePath port $portfolioPort"
}
if ([string]::IsNullOrWhiteSpace($hostPassword)) {
    throw 'SSH password is missing from the deployment configuration.'
}

$msys2Shell = @(
    "$env:USERPROFILE\scoop\apps\msys2\current\msys2_shell.cmd",
    "$env:USERPROFILE\scoop\shims\msys2.cmd"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($msys2Shell)) {
    throw 'MSYS2 with rsync, openssh, and sshpass is required.'
}
$sshExe = "$env:WINDIR\System32\OpenSSH\ssh.exe"
if (-not (Test-Path -LiteralPath $sshExe)) {
    $sshExe = (Get-Command ssh.exe -ErrorAction Stop).Source
}

$deploymentId = [guid]::NewGuid().ToString('N')
$tempBase = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\', '/')
$tempRoot = [System.IO.Path]::GetFullPath((Join-Path $tempBase "blan-portfolio-deploy-$deploymentId"))
if (-not $tempRoot.StartsWith($tempBase + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe temporary path: $tempRoot"
}
$payloadRoot = Join-Path $tempRoot 'payload'
$askpass = Join-Path $tempRoot 'askpass.cmd'
$rsyncPasswordFile = Join-Path $tempRoot 'rsync-password'
$rsyncKnownHosts = Join-Path $tempRoot 'known-hosts'
$remoteScript = Join-Path $tempRoot 'deploy_remote.sh'
$remote = "$hostUser@$hostAddress"
$remoteStage = "/tmp/blan-portfolio-deploy-$deploymentId"

function Invoke-RsyncUpload {
    param([string]$LocalPath, [string]$RemotePath)
    Set-Content -LiteralPath $rsyncPasswordFile -Value $hostPassword -NoNewline -Encoding ASCII
    $localMsysPath = ConvertTo-MsysPath $LocalPath
    if (Test-Path -LiteralPath $LocalPath -PathType Container) {
        $localMsysPath = $localMsysPath.TrimEnd('/') + '/'
    }
    $passwordMsysPath = ConvertTo-MsysPath $rsyncPasswordFile
    $knownHostsMsysPath = ConvertTo-MsysPath $rsyncKnownHosts
    $sshCommand = "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=$knownHostsMsysPath -o PreferredAuthentications=password -o PubkeyAuthentication=no -o BatchMode=no -p $sshPort"
    $command = "sshpass -f '$passwordMsysPath' rsync -av --checksum -e '$sshCommand' '$localMsysPath' '${remote}:$RemotePath'"
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-NativeProcess $msys2Shell @('-defterm', '-no-start', '-here', '-c', $command)
            return
        }
        catch {
            if ($attempt -eq 3) { throw }
            Start-Sleep -Seconds (2 * $attempt)
        }
    }
}

function Invoke-RemoteCommand {
    param([string]$Command)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-NativeProcess $sshExe @(
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=NUL',
                '-o', 'ConnectTimeout=15',
                '-o', 'PreferredAuthentications=password',
                '-o', 'PubkeyAuthentication=no',
                '-p', $sshPort.ToString(),
                $remote,
                $Command
            )
            return
        }
        catch {
            if ($attempt -eq 3) { throw }
            Start-Sleep -Seconds (2 * $attempt)
        }
    }
}

$oldAskpass = $env:SSH_ASKPASS
$oldAskpassRequirement = $env:SSH_ASKPASS_REQUIRE
$oldDisplay = $env:DISPLAY
try {
    New-Item -ItemType Directory -Path $payloadRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'app') -Destination (Join-Path $payloadRoot 'app') -Recurse
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'requirements.txt') -Destination $payloadRoot
    foreach ($cacheDirectory in Get-ChildItem -LiteralPath $payloadRoot -Directory -Filter '__pycache__' -Recurse) {
        $cachePath = [System.IO.Path]::GetFullPath($cacheDirectory.FullName)
        if (-not $cachePath.StartsWith($payloadRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Unsafe cache path: $cachePath"
        }
        Remove-Item -LiteralPath $cachePath -Recurse -Force
    }

    Set-Content -LiteralPath $askpass -Value "@echo $hostPassword" -Encoding ASCII
    $env:SSH_ASKPASS = $askpass
    $env:SSH_ASKPASS_REQUIRE = 'force'
    $env:DISPLAY = '1'

    Invoke-RemoteCommand "mkdir -p $(ConvertTo-ShellSingleQuoted $remoteStage)"
    Invoke-RsyncUpload $payloadRoot "$remoteStage/"

    $quotedPassword = ConvertTo-ShellSingleQuoted $hostPassword
    $quotedTarget = ConvertTo-ShellSingleQuoted $portfolioRemotePath
    $quotedStage = ConvertTo-ShellSingleQuoted $remoteStage
    $remoteLines = @(
        '#!/usr/bin/env bash',
        'set -euo pipefail',
        "SUDO_PASSWORD=$quotedPassword",
        "TARGET=$quotedTarget",
        "STAGE=$quotedStage",
        "PORT=$portfolioPort",
        'run_sudo() { sudo -S -p "" "$@" <<< "$SUDO_PASSWORD"; }',
        '[ "$TARGET" = "/opt/lucygetup-portfolio" ]',
        '[[ "$STAGE" == /tmp/blan-portfolio-deploy-* ]]',
        'cleanup() { [[ "$STAGE" == /tmp/blan-portfolio-deploy-* ]] && rm -rf -- "$STAGE"; }',
        'trap cleanup EXIT',
        'test -f "$STAGE/app/main.py"',
        'test -f "$STAGE/requirements.txt"',
        'before_db_identity="$(run_sudo stat -c ''%d:%i'' "$TARGET/data/backtest.sqlite3")"',
        'backup_dir="$TARGET/deploy-backups"',
        'backup_path="$backup_dir/code-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"',
        'run_sudo mkdir -p "$backup_dir"',
        'run_sudo tar -czf "$backup_path" -C "$TARGET" app requirements.txt',
        'run_sudo rsync -a --delete "$STAGE/app/" "$TARGET/app/"',
        'run_sudo install -m 0644 "$STAGE/requirements.txt" "$TARGET/requirements.txt"',
        'run_sudo chown -R www-data:www-data "$TARGET/app" "$TARGET/requirements.txt"',
        'run_sudo systemctl restart lucygetup-portfolio.service',
        'for attempt in $(seq 1 45); do curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 1; done',
        'run_sudo systemctl is-active lucygetup-portfolio.service',
        'curl -fsS --max-time 10 "http://127.0.0.1:$PORT/api/health"',
        'curl -fsS --max-time 10 "http://127.0.0.1:$PORT/api/default-config" | grep -q ''"rebalance_to_target":false''',
        'curl -fsS --max-time 10 "http://127.0.0.1:$PORT/api/default-config" | grep -q ''"symbol":"511090.SH"''',
        'grep -q ''BACKTEST_ENGINE_VERSION = 46'' "$TARGET/app/services/backtest_engine.py"',
        'after_db_identity="$(run_sudo stat -c ''%d:%i'' "$TARGET/data/backtest.sqlite3")"',
        '[ "$before_db_identity" = "$after_db_identity" ]',
        'printf ''\nengine_version=46 database_file_preserved=true backup=%s\n'' "$backup_path"'
    )
    [System.IO.File]::WriteAllText($remoteScript, ($remoteLines -join "`n") + "`n", [System.Text.UTF8Encoding]::new($false))
    $remoteScriptPath = "$remoteStage/deploy_remote.sh"
    Invoke-RsyncUpload $remoteScript $remoteScriptPath
    $quotedRemoteScript = ConvertTo-ShellSingleQuoted $remoteScriptPath
    Invoke-RemoteCommand "bash $quotedRemoteScript; status=`$?; rm -f $quotedRemoteScript; exit `$status"
}
finally {
    $env:SSH_ASKPASS = $oldAskpass
    $env:SSH_ASKPASS_REQUIRE = $oldAskpassRequirement
    $env:DISPLAY = $oldDisplay
    if (Test-Path -LiteralPath $tempRoot) {
        $resolvedTempRoot = [System.IO.Path]::GetFullPath($tempRoot)
        if ($resolvedTempRoot.StartsWith($tempBase + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) -and
            [System.IO.Path]::GetFileName($resolvedTempRoot).StartsWith('blan-portfolio-deploy-')) {
            Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force
        }
    }
}
