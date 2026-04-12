param(
    [int]$Port = 8000,
    [string]$BindHost = "0.0.0.0",
    [ValidateSet("0", "1")]
    [string]$EnableDnsRebindingProtection = "0"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $scriptDir ".venv"
$requirementsPath = Join-Path $scriptDir "requirements.txt"
$requirementsHashPath = Join-Path $venvDir ".requirements.sha256"

function Get-VenvPythonPath {
    param([string]$VenvRoot)

    $windowsPython = Join-Path $VenvRoot "Scripts/python.exe"
    if (Test-Path $windowsPython) {
        return $windowsPython
    }

    $posixPython = Join-Path $VenvRoot "bin/python"
    if (Test-Path $posixPython) {
        return $posixPython
    }

    return $windowsPython
}

$venvPython = Get-VenvPythonPath -VenvRoot $venvDir
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment..."
    Push-Location $scriptDir
    try {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if ($python) {
            & $python.Source -m venv .venv
        } else {
            $py = Get-Command py -ErrorAction SilentlyContinue
            if ($py) {
                & $py.Source -3 -m venv .venv
            } else {
                throw "Python is not installed or not in PATH."
            }
        }
    } finally {
        Pop-Location
    }
    $venvPython = Get-VenvPythonPath -VenvRoot $venvDir
}

if (-not (Test-Path $venvPython)) {
    throw "Virtual environment Python not found: $venvPython"
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
$env:HOST = $BindHost
$env:PORT = "$Port"

Write-Host "Starting server on ${BindHost}:${Port} (ENABLE_DNS_REBINDING_PROTECTION=$EnableDnsRebindingProtection)..."
& $venvPython (Join-Path $scriptDir "server.py")
