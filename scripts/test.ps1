[CmdletBinding()]
param(
    [string]$Distro = 'Ubuntu'
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
        throw 'WSL is required. Install WSL2 and a Linux distribution.'
    }

    foreach ($relativePath in @('scripts\test.sh', 'scripts\test-identity-display.sh')) {
        $testScript = Convert-ToWslPath (Join-Path $rootDir $relativePath)
        & wsl.exe -d $Distro --exec bash $testScript
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
    exit 0
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
