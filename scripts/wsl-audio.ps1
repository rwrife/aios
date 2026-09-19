<#
.SYNOPSIS
Diagnose the Windows -> WSLg -> WSL audio path for AIOS voice validation.
.DESCRIPTION
Selects the WSL distribution and exposes bounded Check, Playback, Record, and
RoundTrip modes. Check is read-only and never records. Recording modes visibly
announce capture and stop automatically within ten seconds. Each audio layer
(Windows devices and permissions, WSL/WSLg, the PulseAudio endpoint, sinks and
sources, and the requested probe) is reported separately as passed, failed, or
not-tested with an actionable cause - never as a generic "audio available".
A valid caller-supplied PULSE_SERVER is preserved; the WSLg socket is used only
after validation. Nothing here installs a competing audio daemon, exposes TCP
audio, restarts all WSL distributions, or retains recorded speech by default.
#>
[CmdletBinding()]
param(
    [ValidateSet('Check', 'Playback', 'Record', 'RoundTrip')]
    [string]$Mode = 'Check',
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Distro = 'Ubuntu',
    [ValidateRange(1, 10)][int]$Seconds = 5,
    [string]$PulseServer,
    [switch]$KeepRecording
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$script:failures = 0
$script:notTested = 0

function Write-Stage {
    param([string]$Name, [ValidateSet('passed', 'failed', 'not-tested')][string]$Status, [string]$Cause = '')
    if ($Status -eq 'failed') { $script:failures++ }
    if ($Status -eq 'not-tested') { $script:notTested++ }
    if ($Cause) { Write-Host ("STAGE {0} {1}: {2}" -f $Name, $Status, $Cause) }
    else { Write-Host ("STAGE {0} {1}" -f $Name, $Status) }
}

function Invoke-WslChecked([string[]]$Arguments) {
    # Native stderr status text must not become a terminating error; decide
    # from the exit code, like the camera wrapper does.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & wsl.exe -d $Distro @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    return [pscustomobject]@{ Output = (($output | Out-String) -join "`n").Trim(); Code = $code }
}

function Start-WslKeeper {
    # Keep the selected distro alive across discrete wsl.exe calls without
    # restarting anything. Never use wsl --shutdown here.
    return Start-Process -FilePath wsl.exe -ArgumentList @('-d', $Distro, '--', 'sleep', '60') -WindowStyle Hidden -PassThru
}

function Get-WindowsAudioLayers {
    # Windows-side stages. Read-only; never changes devices or permissions.
    try {
        $sound = @(Get-CimInstance -ClassName Win32_SoundDevice -ErrorAction Stop |
            Where-Object { $_.Status -eq 'OK' })
        if ($sound.Count -gt 0) {
            Write-Stage 'windows_audio_devices' 'passed' "$($sound.Count) working audio device(s): $(($sound | ForEach-Object { $_.Name } | Select-Object -First 3) -join '; ')"
        } else {
            Write-Stage 'windows_audio_devices' 'failed' 'no working Windows audio device is present; connect or enable one in Sound settings first'
        }
    } catch {
        Write-Stage 'windows_audio_devices' 'not-tested' "Win32_SoundDevice query unavailable: $($_.Exception.Message)"
    }
    $micConsent = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'
    try {
        if (Test-Path $micConsent) {
            $value = (Get-ItemProperty -Path $micConsent -Name Value -ErrorAction Stop).Value
            if ($value -eq 'Allow') {
                Write-Stage 'windows_microphone_privacy' 'passed'
            } else {
                Write-Stage 'windows_microphone_privacy' 'failed' "microphone access is '$value' for the machine; enable Settings > Privacy > Microphone (desktop apps) before recording"
            }
        } else {
            Write-Stage 'windows_microphone_privacy' 'not-tested' 'microphone consent store key not present on this Windows edition'
        }
    } catch {
        Write-Stage 'windows_microphone_privacy' 'not-tested' "consent store unreadable: $($_.Exception.Message)"
    }
}

function Get-WslLayer {
    $version = Invoke-WslChecked @('--exec', 'uname', '-r')
    if ($version.Code -ne 0) {
        Write-Stage 'wsl_distro' 'failed' "distribution '$Distro' is not reachable (exit $($version.Code)); install or start it with wsl --install / wsl -d $Distro"
        return $false
    }
    Write-Stage 'wsl_distro' 'passed' "kernel $($version.Output)"
    $wslg = Invoke-WslChecked @('--exec', 'sh', '-c', 'test -S /mnt/wslg/PulseServer && echo socket || echo none')
    if ($wslg.Output -match 'socket') {
        Write-Stage 'wslg_bridge' 'passed' '/mnt/wslg/PulseServer exists'
    } elseif ($PulseServer) {
        Write-Stage 'wslg_bridge' 'not-tested' 'no WSLg socket, but an explicit -PulseServer endpoint was supplied and will be validated'
    } else {
        Write-Stage 'wslg_bridge' 'failed' '/mnt/wslg/PulseServer does not exist; WSLg must be enabled (wsl --version) and one GUI app started once; supply -PulseServer only if you run your own endpoint'
    }
    return $true
}

function Invoke-DistroProbe([string]$RemoteMode) {
    $launcher = Join-Path $repoRoot 'scripts/test-wsl-audio.sh'
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        Write-Stage 'distro_probe' 'failed' "missing $launcher"
        return 1
    }
    $converted = Invoke-WslChecked @('--exec', 'wslpath', '-a', '-u', $launcher)
    if ($converted.Code -ne 0 -or -not $converted.Output) {
        Write-Stage 'distro_probe' 'failed' 'could not convert the repository path for WSL'
        return 1
    }
    $script:keeper = Start-WslKeeper
    try {
        $wslArgs = @('-d', $Distro, '--exec')
        # Preserve a valid caller endpoint verbatim; the distro probe only
        # falls back to the WSLg socket after validating it.
        if ($PulseServer) { $wslArgs += @('env', "PULSE_SERVER=$PulseServer") }
        $wslArgs += @('bash', $converted.Output, $RemoteMode, '--seconds', "$Seconds")
        if ($KeepRecording) {
            $retention = Join-Path $env:TEMP "aios-audio-retained-$PID"
            $retentionWsl = Invoke-WslChecked @('--exec', 'wslpath', '-a', '-u', $retention)
            if ($retentionWsl.Code -eq 0 -and $retentionWsl.Output) {
                $wslArgs += @('--keep-dir', $retentionWsl.Output)
                Write-Host "Recordings will be retained under $retention for this run only; delete it when done."
            } else {
                Write-Warning 'Could not map the retention directory into WSL; this run will not retain recordings.'
            }
        }
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & wsl.exe @wslArgs; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $previous }
        if ($code -eq 0) { Write-Stage 'distro_probe' 'passed' "$RemoteMode completed" }
        else { Write-Stage 'distro_probe' 'failed' "the distro probe reported failing layers above (exit $code)" }
        return $code
    } finally {
        if ($script:keeper -and -not $script:keeper.HasExited) {
            Stop-Process -Id $script:keeper.Id -ErrorAction SilentlyContinue
        }
    }
}

$remoteMode = switch ($Mode) { 'Check' { 'check' } 'Playback' { 'playback' } 'Record' { 'record' } 'RoundTrip' { 'roundtrip' } }
Write-Host "AIOS WSL audio wrapper: mode=$Mode distro=$Distro seconds=$Seconds (recording always stops within 10s)"
if ($Mode -ne 'Check') {
    Write-Host '[aios] Recording modes capture AUDIBLE sound and auto-stop. Recorded speech is not retained by default.'
}
Get-WindowsAudioLayers
if (Get-WslLayer) {
    $code = Invoke-DistroProbe $remoteMode
} else {
    $code = 1
}
Write-Host ("WRAPPER SUMMARY mode={0} windows/distro-stage failures={1} not-tested={2}" -f $Mode, $script:failures, $script:notTested)
if ($script:failures -gt 0 -or $code -ne 0) {
    Write-Host "RESULT $Mode incomplete - fix the failed stages above and re-run Check first."
    exit 1
}
Write-Host "RESULT $Mode ok - layers passed; human listening checks in the runbook still apply."
