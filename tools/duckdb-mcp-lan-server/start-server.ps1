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

    return $null
}

$venvPython = Get-VenvPythonPath -VenvRoot $venvDir
if (-not $venvPython -or -not (Test-Path $venvPython)) {
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

if (-not $venvPython -or -not (Test-Path $venvPython)) {
    throw "Virtual environment Python executable not found in: $venvDir"
}

$requirementsHash = (Get-FileHash -Path $requirementsPath -Algorithm SHA256).Hash
$installedHash = ""
if (Test-Path $requirementsHashPath) {
    $installedHash = (Get-Content -Path $requirementsHashPath -Raw).Trim()
}

function Test-RequiredModules {
    param([string]$PythonExe)

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $checkScript = @"
import importlib
import json

required_modules = ["duckdb", "matplotlib", "scipy", "mcp", "pypdf"]
missing = []
for module_name in required_modules:
    try:
        importlib.import_module(module_name)
    except Exception as ex:
        missing.append(module_name)

print(json.dumps({"ok": len(missing) == 0, "missing": missing}))
if missing:
    raise SystemExit(1)
"@
        $output = & $PythonExe -c $checkScript 2>&1
        $resultText = ($output | Out-String).Trim()
        if (-not $resultText) {
            Write-Warning "Dependency import check failed: no output from Python."
            return $false
        }
        try {
            $result = $resultText | ConvertFrom-Json
        } catch {
            Write-Warning "Dependency import check failed."
            return $false
        }
        if (-not $result.ok) {
            $missingModules = @($result.missing) -join ", "
            Write-Warning "Dependency import check failed. Missing modules: $missingModules"
            return $false
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Dependency import check failed."
            return $false
        }
        return $true
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

$needsInstall = $false
if ($requirementsHash -ne $installedHash) {
    $needsInstall = $true
} elseif (-not (Test-RequiredModules -PythonExe $venvPython)) {
    Write-Host "Detected missing/broken dependencies in virtual environment. Reinstalling..."
    $needsInstall = $true
}

if ($needsInstall) {
    Write-Host "Installing dependencies from requirements.txt..."
    & $venvPython -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. Please resolve the pip errors above and retry."
    }
    Set-Content -Path $requirementsHashPath -Value $requirementsHash -Encoding UTF8
} else {
    Write-Host "Dependencies are up to date. Skipping install."
}

$env:ENABLE_DNS_REBINDING_PROTECTION = $EnableDnsRebindingProtection
$env:HOST = $BindHost
$env:PORT = "$Port"

Write-Host "Starting server on ${BindHost}:${Port} (ENABLE_DNS_REBINDING_PROTECTION=$EnableDnsRebindingProtection)..."
& $venvPython (Join-Path $scriptDir "server.py")
