[CmdletBinding()]
param(
    [string]$Distro = 'Ubuntu',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BuildArgs
)

$ErrorActionPreference = 'Stop'
$rootDir = Split-Path -Parent $PSScriptRoot

function Convert-ToWslPath([string]$Path) {
    $absolute = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $converted = & wsl.exe -d $Distro --exec wslpath -a -u $absolute
    if ($LASTEXITCODE -ne 0) {
        throw "Could not access the repository from WSL distribution '$Distro'."
    }
    return ($converted -join "`n").Trim()
}

try {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw 'WSL is required. Install WSL2, a Linux distribution, and Docker with WSL integration.'
    }

    $buildScript = Convert-ToWslPath (Join-Path $rootDir 'scripts\build.sh')
    $wslArgs = @('-d', $Distro, '--exec', 'env')
    foreach ($name in @('ARCH', 'RELEASE_TAG', 'RUNTIME', 'AIOS_IDENTITY_BUILD', 'AIOS_BUILDER_IMAGE')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) {
            $wslArgs += "$name=$value"
        }
    }
    $wslArgs += @('bash', $buildScript)
    if ($BuildArgs) {
        $wslArgs += $BuildArgs
    }

    & wsl.exe @wslArgs
    exit $LASTEXITCODE
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
