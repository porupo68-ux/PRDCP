$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    py -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if (-not (Test-Path .env)) {
        Copy-Item .env.example .env
    }
    & .\.venv\Scripts\python.exe main.py --doctor
}
finally {
    Pop-Location
}
