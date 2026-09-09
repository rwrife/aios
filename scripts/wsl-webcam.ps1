<#
.SYNOPSIS
Diagnose and hand off a selected USB webcam between Windows and WSL.
.DESCRIPTION
Status is read-only. Mutating actions require an explicit action and bus ID.
Administrative actions must run in an elevated PowerShell. No action reboots,
disables Windows Hello, closes raw handles, or changes service startup types.
#>
[CmdletBinding()]
param(
    [ValidateSet('Status','Bind','Attach','Detach','Unbind','FindOwner','StopLogiTune','RestoreLogiTune','TraceAttach','Probe')]
    [string]$Action = 'Status',
    [ValidatePattern('^[0-9]+-[0-9]+$')][string]$BusId,
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Distro = 'Ubuntu',
    [ValidatePattern('^/dev/video[0-9]+$')][string]$VideoDevice = '/dev/video0',
    [switch]$ForceShare,
    [string]$HandlePath,
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repoRoot 'tmp\wsl-webcam' }
$usbCommand = Get-Command usbipd.exe -ErrorAction SilentlyContinue
$usbExe = if ($usbCommand) { $usbCommand.Source } else { Join-Path $env:ProgramFiles 'usbipd-win\usbipd.exe' }
if (-not (Test-Path -LiteralPath $usbExe)) {
    throw 'Install usbipd-win first: winget install --interactive --exact dorssel.usbipd-win'
}

function Require-Admin {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run the $Action action in Administrator PowerShell."
    }
}
function Invoke-Native([string]$File, [string[]]$NativeArguments, [int[]]$AcceptedExitCodes = @(0)) {
    # Windows PowerShell 5.1 can turn successful native stderr status messages
    # into terminating errors. Decide success from the native exit code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $File @NativeArguments; $nativeCode = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previous }
    if ($nativeCode -notin $AcceptedExitCodes) { throw "$File exited with code $nativeCode" }
}
function Get-SelectedDevice {
    if (-not $BusId) { throw 'Select a bus ID from the Status output using -BusId.' }
    $state = (Invoke-Native $usbExe @('state') | Out-String | ConvertFrom-Json)
    $selected = @($state.Devices | Where-Object { $_.BusId -eq $BusId })
    if ($selected.Count -ne 1) { throw 'The selected bus ID is no longer connected. Run Status again.' }
    return $selected[0]
}
function Start-WslKeeper {
    # Keep Ubuntu alive across Windows commands without restarting any distro.
    return Start-Process -FilePath wsl.exe -ArgumentList @('-d',$Distro,'--','sleep','45') -WindowStyle Hidden -PassThru
}
function Invoke-Attach {
    $keeper = Start-WslKeeper
    try {
        Start-Sleep -Milliseconds 1500
        Invoke-Native $usbExe @('attach','--wsl','--busid',$BusId)
    } finally {
        if (-not $keeper.HasExited) { Stop-Process -Id $keeper.Id -ErrorAction SilentlyContinue }
    }
}

switch ($Action) {
    'Status' {
        Invoke-Native $usbExe @('list')
        $background = @(Get-Process -Name LogiTuneAgent,LogiTuneUpdater -ErrorAction SilentlyContinue | ForEach-Object {
            [pscustomobject]@{Kind='Process'; Name=$_.ProcessName; Id=$_.Id; State='Running'}
        })
        $background += @(Get-Service -Name LogiTuneUpdaterService -ErrorAction SilentlyContinue | ForEach-Object {
            [pscustomobject]@{Kind='Service'; Name=$_.Name; Id=$null; State=$_.Status}
        })
        $background | Format-Table -AutoSize
    }
    'Bind' {
        Require-Admin
        $null = Get-SelectedDevice
        $bindArgs = @('bind','--busid',$BusId)
        if ($ForceShare) { $bindArgs += '--force' }
        Invoke-Native $usbExe $bindArgs
    }
    'Attach' { $null = Get-SelectedDevice; Invoke-Attach }
    'Detach' { $null = Get-SelectedDevice; Invoke-Native $usbExe @('detach','--busid',$BusId) }
    'Unbind' { Require-Admin; $null = Get-SelectedDevice; Invoke-Native $usbExe @('unbind','--busid',$BusId) }
    'FindOwner' {
        Require-Admin
        $device = Get-SelectedDevice
        if (-not $HandlePath -or -not (Test-Path -LiteralPath $HandlePath)) {
            throw 'Supply -HandlePath to Microsoft Sysinternals handle64.exe (see docs/wsl-webcam.md).'
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $HandlePath
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
            throw 'Handle must have a valid Microsoft signature.'
        }
        # The owner holds a camera interface, often not its composite USB parent.
        $prefix = ($device.InstanceId -split '\\')[0..1] -join '\'
        foreach ($candidate in Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId.StartsWith($prefix) }) {
            $current = $candidate.InstanceId
            for ($depth = 0; $depth -lt 8 -and $current -and $current -ne $device.InstanceId; $depth++) {
                $current = (Get-PnpDeviceProperty -InstanceId $current -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue).Data
            }
            if ($current -ne $device.InstanceId) { continue }
            $pdo = (Get-PnpDeviceProperty -InstanceId $candidate.InstanceId -KeyName 'DEVPKEY_Device_PDOName' -ErrorAction SilentlyContinue).Data
            if ($pdo) {
                Write-Output "Checking $($candidate.FriendlyName): $pdo"
                # Search only. Never use Handle's -c option to close driver handles.
                # Handle returns 1 for no match; keep checking video interfaces.
                Invoke-Native $HandlePath @('-accepteula','-nobanner','-a',$pdo) @(0,1)
            }
        }
        Get-CimInstance Win32_Process -Filter "Name='LogiTuneAgent.exe'" | Select-Object Name,ProcessId,ParentProcessId
        Get-CimInstance Win32_Service -Filter "Name='LogiTuneUpdaterService'" | Select-Object Name,State,ProcessId
    }
    'StopLogiTune' {
        Require-Admin
        $service = Get-Service LogiTuneUpdaterService -ErrorAction Stop
        New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
        $snapshot = Join-Path $OutputDirectory 'logitune-service.json'
        if (Test-Path -LiteralPath $snapshot) { throw 'A previous service snapshot exists. Restore it before starting another stop cycle.' }
        @{ wasRunning=($service.Status -eq 'Running') } | ConvertTo-Json | Set-Content -LiteralPath $snapshot -Encoding UTF8
        Stop-Service -Name LogiTuneUpdaterService
        Get-Process -Name LogiTuneAgent -ErrorAction SilentlyContinue | Stop-Process
        Write-Output 'Logi Tune watchdog and agent stopped. Startup type is unchanged. Use RestoreLogiTune after detaching the camera.'
    }
    'RestoreLogiTune' {
        Require-Admin
        $snapshot = Join-Path $OutputDirectory 'logitune-service.json'
        $saved = Get-Content -LiteralPath $snapshot -Raw | ConvertFrom-Json
        if ($saved.wasRunning -eq $true) { Start-Service -Name LogiTuneUpdaterService }
        Remove-Item -LiteralPath $snapshot
        Write-Output 'Previous Logi Tune service state restored.'
    }
    'TraceAttach' {
        Require-Admin
        $null = Get-SelectedDevice
        $state = (Invoke-Native $usbExe @('state') | Out-String | ConvertFrom-Json)
        if (@($state.Devices | Where-Object { $_.ClientIPAddress }).Count) {
            throw 'Detach active USB/IP devices before restarting the bridge for diagnostics.'
        }
        New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
        $logDir = Join-Path $OutputDirectory (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
        New-Item -ItemType Directory -Path $logDir | Out-Null
        $wasRunning = (Get-Service usbipd).Status -eq 'Running'
        $server = $keeper = $null
        try {
            Stop-Service usbipd
            $server = Start-Process -FilePath $usbExe -ArgumentList @('server','Logging:LogLevel:Default=Debug','usbipd:PcapNg:Path=') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir 'server.log') -RedirectStandardError (Join-Path $logDir 'server-error.log')
            $keeper = Start-WslKeeper
            Start-Sleep -Seconds 2
            $attach = Start-Process -FilePath $usbExe -ArgumentList @('attach','--wsl','--busid',$BusId) -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $logDir 'attach.log') -RedirectStandardError (Join-Path $logDir 'attach-error.log')
            Write-Output "Attach exit code: $($attach.ExitCode); diagnostics: $logDir"
        } finally {
            if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id }
            if ($wasRunning) { Start-Service usbipd }
            if ($keeper -and -not $keeper.HasExited) { Stop-Process -Id $keeper.Id -ErrorAction SilentlyContinue }
        }
        Write-Output 'The diagnostic server is stopped. Run Attach again to keep a successful connection.'
    }
    'Probe' {
        $linuxRepo = (Invoke-Native wsl.exe @('-d',$Distro,'--exec','wslpath','-u',$repoRoot.Replace('\','/')) | Out-String).Trim()
        Invoke-Native wsl.exe @('-d',$Distro,'--cd',$linuxRepo,'--','env','PYTHONPATH=apps','python3','-m','aios.camera','--probe',$VideoDevice)
    }
}
