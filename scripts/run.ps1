# Launch Linux QEMU through WSLg, sharing the Linux launcher and its defaults.
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$IsoPath,
    [string]$Distro = 'Ubuntu',
    [switch]$DryRun,
    [switch]$Native
)
$ErrorActionPreference = 'Stop'
if ($Native) {
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
    $launcher = Convert-ToWslPath "$PSScriptRoot/run.sh"
    $wslArgs = @('-d', $Distro, '--exec', 'env')
    foreach ($name in @('AIOS_VM_MEM_MB', 'AIOS_VM_CPUS', 'AIOS_VM_DISK_SIZE',
                        'AIOS_QEMU_AUDIO', 'AIOS_QEMU_HEADLESS', 'AIOS_QEMU_UEFI', 'AIOS_QEMU_SERIAL')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) { $wslArgs += "$name=$value" }
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
