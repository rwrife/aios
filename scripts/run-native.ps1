# Optional native Windows fallback; run.ps1 uses WSL for better performance.
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$IsoPath,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$rootDir = Split-Path -Parent $PSScriptRoot

function Get-Setting([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($value)) { return $Default }
    return $value
}

function Find-QemuTool([string]$Name) {
    $command = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $installed = Join-Path $env:ProgramFiles "qemu\$Name.exe"
    if (Test-Path -LiteralPath $installed -PathType Leaf) { return $installed }
    throw "$Name.exe not found. Install QEMU for Windows and add its directory to PATH."
}

try {
    if ($env:AIOS_VM_CAMERA_BUS -or $env:AIOS_VM_CAMERA_ADDR) {
        throw 'Linux USB camera passthrough requires the WSL launcher. Run without -Native.'
    }
    if (-not $IsoPath) {
        $latest = Get-ChildItem -LiteralPath "$rootDir/distro/alpine/out" -Filter '*-x86_64.iso' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) { $IsoPath = $latest.FullName }
    }
    if (-not $IsoPath -or -not (Test-Path -LiteralPath $IsoPath -PathType Leaf)) {
        throw 'No ISO found. Build with bash scripts/build.sh in WSL/Linux, or pass an ISO: .\scripts\run.ps1 C:\images\aios-x86_64.iso'
    }
    $IsoPath = (Resolve-Path -LiteralPath $IsoPath).Path
    $dry = $DryRun -or $env:DRY_RUN -eq '1'
    $diskPath = [IO.Path]::GetFullPath((Get-Setting 'AIOS_VM_DISK' "$rootDir/.tmp-aios-live.qcow2"))
    $diskSize = Get-Setting 'AIOS_VM_DISK_SIZE' '64G'
    $memory = Get-Setting 'AIOS_VM_MEM_MB' '16384'
    $cpuCount = Get-Setting 'AIOS_VM_CPUS' '4'
    $audioBackend = Get-Setting 'AIOS_QEMU_AUDIO' 'dsound'
    $accelerator = Get-Setting 'AIOS_QEMU_ACCEL' 'tcg'
    $qemu = 'qemu-system-x86_64.exe'
    if (-not $dry) { $qemu = Find-QemuTool 'qemu-system-x86_64' }

    $qemuArgs = @(
        '-name', 'AIOS',
        '-m', $memory, '-smp', $cpuCount,
        '-accel', $accelerator, '-cpu', 'max',
        '-boot', 'd', '-cdrom', $IsoPath,
        '-drive', "if=virtio,file=$($diskPath.Replace(',', ',,')),format=qcow2",
        '-nic', 'user,model=virtio-net-pci',
        '-audiodev', "$audioBackend,id=audio0",
        '-device', 'intel-hda', '-device', 'hda-duplex,audiodev=audio0'
    )
    if ($env:AIOS_QEMU_UEFI -eq '1') {
        if (-not $env:AIOS_OVMF_CODE -or -not (Test-Path -LiteralPath $env:AIOS_OVMF_CODE -PathType Leaf)) {
            throw 'Set AIOS_OVMF_CODE to your OVMF_CODE firmware file.'
        }
        $qemuArgs += @('-drive', "if=pflash,format=raw,readonly=on,file=$($env:AIOS_OVMF_CODE.Replace(',', ',,'))")
    }
    if ($env:AIOS_QEMU_HEADLESS -eq '1') {
        $qemuArgs += @('-display', 'none', '-serial', 'mon:stdio')
    } else {
        $qemuArgs += @('-display', 'sdl,full-screen=off')
    }
    if ($dry) {
        # Print a copyable PowerShell command, without creating a disk or starting QEMU.
        Write-Output ('& ' + ((@($qemu) + $qemuArgs | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }) -join ' '))
        exit 0
    }
    if (-not (Test-Path -LiteralPath $diskPath)) {
        $qemuImg = Find-QemuTool 'qemu-img'
        & $qemuImg create -f qcow2 $diskPath $diskSize
        if ($LASTEXITCODE -ne 0) { throw "Could not create VM disk: $diskPath" }
    }
    & $qemu @qemuArgs
    exit $LASTEXITCODE
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
