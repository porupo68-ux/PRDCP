$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $Python = if (Test-Path .\.venv\Scripts\python.exe) {
        ".\.venv\Scripts\python.exe"
    } else {
        "py"
    }
    & $Python scripts\verify.py
}
finally {
    Pop-Location
}
