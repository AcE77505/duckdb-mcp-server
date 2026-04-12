param(
    [int]$Port = 8000,
    [string]$Host = "0.0.0.0",
    [ValidateSet("0", "1")]
    [string]$EnableDnsRebindingProtection = "0"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $scriptDir ".venv"
$venvPython = Join-Path $venvDir "Scripts/python.exe"
$requirementsPath = Join-Path $scriptDir "requirements.txt"
$requirementsHashPath = Join-Path $venvDir ".requirements.sha256"

function Get-SystemPython {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return "python"
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return "py -3"
    }

    throw "Python is not installed or not in PATH."
}

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment..."
    Push-Location $scriptDir
    try {
        $pythonCmd = Get-SystemPython
        Invoke-Expression "$pythonCmd -m venv .venv"
    } finally {
        Pop-Location
    }
}

$requirementsHash = (Get-FileHash -Path $requirementsPath -Algorithm SHA256).Hash
$installedHash = ""
if (Test-Path $requirementsHashPath) {
    $installedHash = (Get-Content -Path $requirementsHashPath -Raw).Trim()
}

if ($requirementsHash -ne $installedHash) {
    Write-Host "Installing dependencies from requirements.txt..."
    & $venvPython -m pip install -r $requirementsPath
    Set-Content -Path $requirementsHashPath -Value $requirementsHash -Encoding UTF8
} else {
    Write-Host "Dependencies are up to date. Skipping install."
}

$env:ENABLE_DNS_REBINDING_PROTECTION = $EnableDnsRebindingProtection
$env:HOST = $Host
$env:PORT = "$Port"

Write-Host "Starting server on ${Host}:${Port} (ENABLE_DNS_REBINDING_PROTECTION=$EnableDnsRebindingProtection)..."
& $venvPython (Join-Path $scriptDir "server.py")
