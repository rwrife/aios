# Launch Linux QEMU through WSLg, sharing the Linux launcher and its defaults.
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$IsoPath,
    [string]$Distro = 'Ubuntu',
    [string]$Name,
    [switch]$Camera,
    [ValidatePattern('^[0-9]+-[0-9]+$')][string]$CameraBusId,
    [switch]$DryRun,
    [switch]$Native,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$RemainingArguments
)
$ErrorActionPreference = 'Stop'
$remaining = @($RemainingArguments)
if ($IsoPath -eq '--camera') {
    $Camera = $true
    $IsoPath = $null
}
foreach ($argument in $remaining) {
    if ([string]::IsNullOrEmpty($argument)) {
        continue
    } elseif ($argument -eq '--camera') {
        $Camera = $true
    } elseif (-not $IsoPath) {
        $IsoPath = $argument
    } else {
        throw "Unknown argument: $argument"
    }
}
$effectiveDryRun = $DryRun -or $env:DRY_RUN -eq '1'
$cameraRequested = $Camera -or [bool]$CameraBusId
$cameraOriginalAcl = $null
$cameraNodeIdentity = $null
$cameraAclChanged = $false
$configuredCameraBus = if ($cameraRequested -and -not $CameraBusId) {
    [Environment]::GetEnvironmentVariable('AIOS_VM_CAMERA_BUS')
} else { '' }
$configuredCameraAddr = if ($cameraRequested -and -not $CameraBusId) {
    [Environment]::GetEnvironmentVariable('AIOS_VM_CAMERA_ADDR')
} else { '' }
if ([bool]$configuredCameraBus -xor [bool]$configuredCameraAddr) {
    throw 'Set both AIOS_VM_CAMERA_BUS and AIOS_VM_CAMERA_ADDR, or neither.'
}
if ($Native) {
    if ($cameraRequested) {
        Write-Error '-Camera and -CameraBusId are supported only by the WSL QEMU launcher.'
        exit 1
    }
    & "$PSScriptRoot/run-native.ps1" -IsoPath $IsoPath -Name $Name -DryRun:$DryRun
    exit $LASTEXITCODE
}
if ($cameraRequested -and -not $CameraBusId -and -not $configuredCameraBus) {
    $savedCameraBusId = [Environment]::GetEnvironmentVariable('AIOS_VM_CAMERA_BUS_ID')
    if (-not $savedCameraBusId) {
        $savedCameraBusId = [Environment]::GetEnvironmentVariable('AIOS_VM_CAMERA_BUS_ID', 'User')
    }
    if ($savedCameraBusId -and $savedCameraBusId -notmatch '^[0-9]+-[0-9]+$') {
        throw 'AIOS_VM_CAMERA_BUS_ID must be a Windows USB bus ID such as 2-2.'
    }
    if ($savedCameraBusId) { $CameraBusId = $savedCameraBusId }
}
try {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw 'WSL is required. Install WSL2 with Ubuntu and WSLg, or use -Native for native Windows QEMU.'
    }
    function Convert-ToWslPath([string]$Path) {
        if ($Path.StartsWith('/')) { return $Path }
        $absolute = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
        $converted = & wsl.exe -d $Distro --exec wslpath -a -u $absolute
        if ($LASTEXITCODE -ne 0) { throw "Could not convert path for WSL: $Path" }
        return ($converted -join "`n").Trim()
    }
    function Get-WslgDisplayState {
        # Use the latest state, with no embedded double quotes for PowerShell 5.1's WSL argument handling.
        $probe = @'
if [ ! -r /mnt/wslg/weston.log ]; then
    echo unavailable
    exit 0
fi
state=$(sed -n 's/.*RDP backend: use_gfxredir = \([01]\)$/\1/p' /mnt/wslg/weston.log | tail -n 1)
case $state in
    1) echo ready ;;
    0) echo broken ;;
    *) echo starting ;;
esac
'@
        $state = ((& wsl.exe -d $Distro --exec sh -c $probe) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0 -or $state -notin @('ready', 'broken', 'starting', 'unavailable')) {
            throw 'Could not inspect the WSLg display service. QEMU was not started.'
        }
        return $state
    }
    function Invoke-UsbAttach([string]$UsbExe, [string]$BusId) {
        $keeper = Start-Process -FilePath wsl.exe -ArgumentList @(
            '-d', $Distro, '--', 'sleep', '45'
        ) -WindowStyle Hidden -PassThru
        try {
            Start-Sleep -Milliseconds 1500
            & $UsbExe attach --wsl --busid $BusId
            if ($LASTEXITCODE -ne 0) { throw "Could not attach USB device $BusId to WSL." }
        } finally {
            if (-not $keeper.HasExited) {
                Stop-Process -Id $keeper.Id -ErrorAction SilentlyContinue
            }
        }
    }
    $cameraUsbId = ''
    if ($CameraBusId) {
        $usbCommand = Get-Command usbipd.exe -ErrorAction SilentlyContinue
        if (-not $usbCommand) {
            throw 'usbipd-win is required for -CameraBusId. Install it with winget install --interactive --exact dorssel.usbipd-win.'
        }
        $state = (& $usbCommand.Source state | Out-String | ConvertFrom-Json)
        $selected = @($state.Devices | Where-Object { $_.BusId -eq $CameraBusId })
        if ($selected.Count -ne 1) {
            throw "USB device $CameraBusId is no longer connected. Run usbipd list again."
        }
        $idMatch = [regex]::Match($selected[0].InstanceId, 'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})',
            [Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if (-not $idMatch.Success) {
            throw "Could not identify USB device $CameraBusId."
        }
        $cameraUsbId = ($idMatch.Groups[1].Value + ':' + $idMatch.Groups[2].Value).ToLowerInvariant()
        if (-not $effectiveDryRun -and -not $selected[0].ClientIPAddress) {
            Invoke-UsbAttach $usbCommand.Source $CameraBusId
            Start-Sleep -Seconds 2
        }
    }
    $cameraBus = $configuredCameraBus
    $cameraAddr = $configuredCameraAddr
    if ($cameraRequested -and -not $cameraBus) {
        if ($cameraUsbId) {
            $usbLines = @()
            $attempts = if ($effectiveDryRun) { 1 } else { 10 }
            for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
                $usbLines = & wsl.exe -d $Distro --exec lsusb -d $cameraUsbId
                if ($LASTEXITCODE -eq 0 -and $usbLines) { break }
                Start-Sleep -Milliseconds 500
            }
            $location = [regex]::Match(($usbLines -join "`n"), 'Bus\s+0*([0-9]+)\s+Device\s+0*([0-9]+):')
            if (-not $location.Success) { throw "USB camera $cameraUsbId is not visible in WSL." }
            $cameraBus = $location.Groups[1].Value
            $cameraAddr = $location.Groups[2].Value
        } else {
            $probe = @'
shopt -s nullglob
candidates=(/dev/v4l/by-id/*-video-index0)
[ "${#candidates[@]}" -eq 1 ] || exit 0
node=$(readlink -f "${candidates[0]}")
path=$(readlink -f "/sys/class/video4linux/${node##*/}/device")
while [ "$path" != / ]; do
    if [ -r "$path/busnum" ] && [ -r "$path/devnum" ]; then
        printf '%s %s\n' "$(cat "$path/busnum")" "$(cat "$path/devnum")"
        exit 0
    fi
    path=${path%/*}
done
'@
            $location = ((& wsl.exe -d $Distro --exec bash -lc $probe) -join "`n").Trim()
            if ($LASTEXITCODE -ne 0) { throw 'Could not inspect attached WSL cameras.' }
            if ($location -match '^0*([0-9]+)\s+0*([0-9]+)$') {
                $cameraBus = $Matches[1]
                $cameraAddr = $Matches[2]
            }
        }
    }
    if ($cameraRequested -and -not $cameraBus) {
        throw 'No camera is attached to WSL. Supply -CameraBusId with the current USB bus ID, or attach one camera first.'
    }
    if ($cameraBus -and -not $effectiveDryRun) {
        if ($cameraBus -notmatch '^[1-9][0-9]{0,2}$' -or $cameraAddr -notmatch '^[1-9][0-9]{0,2}$') {
            throw 'Camera bus and address must identify one current Linux USB node.'
        }
        $linuxUser = ((& wsl.exe -d $Distro --exec id -un) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0 -or -not $linuxUser) { throw 'Could not determine the WSL user.' }
        $cameraDevice = '/dev/bus/usb/{0:D3}/{1:D3}' -f [int]$cameraBus, [int]$cameraAddr
        $cameraNodeIdentity = ((& wsl.exe -d $Distro --exec stat -Lc '%d:%i:%t:%T' $cameraDevice) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0 -or -not $cameraNodeIdentity) { throw 'Could not identify the selected camera node.' }
        $cameraOriginalAcl = ((& wsl.exe -d $Distro -u root --exec getfacl -cp $cameraDevice) -join "`n")
        if ($LASTEXITCODE -ne 0 -or -not $cameraOriginalAcl) { throw 'Could not preserve the selected camera ACL.' }
        $cameraAclChanged = $true
        & wsl.exe -d $Distro -u root --exec setfacl -m "u:${linuxUser}:rw" $cameraDevice
        if ($LASTEXITCODE -ne 0) { throw "Could not grant QEMU access to $cameraDevice." }
        Write-Host "Passing WSL camera $cameraDevice through to AIOS."
    }
    $launcher = Convert-ToWslPath "$PSScriptRoot/run.sh"
    $wslArgs = @('-d', $Distro, '--exec', 'env')
    $vmName = if ($Name) { $Name } else { $env:AIOS_VM_NAME }
    if ($vmName) { $wslArgs += "AIOS_VM_NAME=$vmName" }
    # Preserve a caller-supplied PulseAudio endpoint through PowerShell -> WSL
    # -> the Linux launcher so QEMU's pa backend reaches the same server the
    # host diagnostics validated. Never synthesize one here.
    if ($env:PULSE_SERVER) { $wslArgs += "PULSE_SERVER=$env:PULSE_SERVER" }
    foreach ($name in @('AIOS_VM_MEM_MB', 'AIOS_VM_CPUS', 'AIOS_VM_DISK_SIZE',
                        'AIOS_QEMU_AUDIO', 'AIOS_QEMU_HEADLESS', 'AIOS_QEMU_UEFI', 'AIOS_QEMU_SERIAL')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) { $wslArgs += "$name=$value" }
    }
    if ($cameraBus) {
        $wslArgs += "AIOS_VM_CAMERA_BUS=$cameraBus"
        $wslArgs += "AIOS_VM_CAMERA_ADDR=$cameraAddr"
    }
    foreach ($name in @('AIOS_VM_DISK', 'AIOS_OVMF_CODE')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) { $wslArgs += "$name=$(Convert-ToWslPath $value)" }
    }
    if ($DryRun -or $env:DRY_RUN -eq '1') { $wslArgs += 'DRY_RUN=1' }
    $wslArgs += @('bash', $launcher)
    if ($IsoPath) { $wslArgs += Convert-ToWslPath $IsoPath }
    if (-not ($DryRun -or $env:DRY_RUN -eq '1' -or $env:AIOS_QEMU_HEADLESS -eq '1')) {
        $displayState = Get-WslgDisplayState
        for ($attempt = 0; $displayState -in @('starting', 'unavailable') -and $attempt -lt 20; $attempt++) {
            Start-Sleep -Milliseconds 500
            $displayState = Get-WslgDisplayState
        }
        if ($displayState -eq 'broken') {
            Write-Warning 'WSLg is in COPY MODE: QEMU can boot but its desktop window may be invisible.'
            $answer = Read-Host 'Restart the WSLg display service? This closes ALL WSL GUI apps, but does not shut down WSL or Docker. [y/N]'
            if ($answer -notmatch '^(?i:y|yes)$') {
                throw 'WSLg repair was declined. QEMU was not started.'
            }
            $restart = @'
set -eu
set -- $(pidof weston)
if [ $# -ne 1 ]; then
    echo 'Expected one WSLg Weston process; refusing to restart an ambiguous target.' >&2
    exit 1
fi
kill -TERM $1
'@
            & wsl.exe -d $Distro --system --exec sh -c $restart
            if ($LASTEXITCODE -ne 0) { throw 'Could not restart the WSLg display service. QEMU was not started.' }
            # WSLg also recycles PulseAudio; the display log can report ready before that teardown finishes.
            Start-Sleep -Seconds 4
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                Start-Sleep -Milliseconds 500
                $displayState = Get-WslgDisplayState
                if ($displayState -eq 'ready') { break }
            }
            if ($displayState -eq 'ready') { Write-Host 'WSLg display restored.' }
        }
        if ($displayState -ne 'ready') {
            throw 'WSLg is not ready. QEMU was not started. Check /mnt/wslg/weston.log; WSL2 with WSLg is required for windowed launches.'
        }
    }
    & wsl.exe @wslArgs
    exit $LASTEXITCODE
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
} finally {
    if ($cameraAclChanged) {
        # USB addresses can be reused after unplug/replug. Never restore onto a
        # different node, and never remove a pre-existing user ACL indiscriminately.
        $currentIdentity = ((& wsl.exe -d $Distro --exec stat -Lc '%d:%i:%t:%T' $cameraDevice 2>$null) -join "`n").Trim()
        if ($LASTEXITCODE -eq 0 -and $currentIdentity -eq $cameraNodeIdentity) {
            $cameraOriginalAcl | & wsl.exe -d $Distro -u root --exec setfacl --set-file=- $cameraDevice
            if ($LASTEXITCODE -ne 0) { Write-Warning 'Could not restore the selected camera ACL; inspect that node before reuse.' }
        }
    }
}
