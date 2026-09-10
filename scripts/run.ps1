# Launch Linux QEMU through WSLg, sharing the Linux launcher and its defaults.
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$IsoPath,
    [string]$Distro = 'Ubuntu',
    [ValidatePattern('^[0-9]+-[0-9]+$')][string]$CameraBusId,
    [switch]$DryRun,
    [switch]$Native
)
$ErrorActionPreference = 'Stop'
if ($Native) {
    if ($CameraBusId) {
        Write-Error '-CameraBusId is supported only by the WSL QEMU launcher.'
        exit 1
    }
    & "$PSScriptRoot/run-native.ps1" -IsoPath $IsoPath -DryRun:$DryRun
    exit $LASTEXITCODE
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
        if (-not $DryRun -and -not $selected[0].ClientIPAddress) {
            & $usbCommand.Source attach --wsl --busid $CameraBusId
            if ($LASTEXITCODE -ne 0) { throw "Could not attach USB device $CameraBusId to WSL." }
            Start-Sleep -Seconds 2
        }
    }
    $cameraBus = if ($CameraBusId) { '' } else { [Environment]::GetEnvironmentVariable('AIOS_VM_CAMERA_BUS') }
    $cameraAddr = if ($CameraBusId) { '' } else { [Environment]::GetEnvironmentVariable('AIOS_VM_CAMERA_ADDR') }
    if ([bool]$cameraBus -xor [bool]$cameraAddr) {
        throw 'Set both AIOS_VM_CAMERA_BUS and AIOS_VM_CAMERA_ADDR, or neither.'
    }
    if (-not $cameraBus) {
        if ($cameraUsbId) {
            $usbLines = @()
            $attempts = if ($DryRun) { 1 } else { 10 }
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
    if ($cameraBus -and -not $DryRun) {
        $linuxUser = ((& wsl.exe -d $Distro --exec id -un) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0 -or -not $linuxUser) { throw 'Could not determine the WSL user.' }
        $cameraDevice = '/dev/bus/usb/{0:D3}/{1:D3}' -f [int]$cameraBus, [int]$cameraAddr
        & wsl.exe -d $Distro -u root --exec setfacl -m "u:${linuxUser}:rw" $cameraDevice
        if ($LASTEXITCODE -ne 0) { throw "Could not grant QEMU access to $cameraDevice." }
        Write-Host "Passing WSL camera $cameraDevice through to AIOS."
    }
    $launcher = Convert-ToWslPath "$PSScriptRoot/run.sh"
    $wslArgs = @('-d', $Distro, '--exec', 'env')
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
    & wsl.exe @wslArgs
    exit $LASTEXITCODE
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
