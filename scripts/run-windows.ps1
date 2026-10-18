param(
    [ValidateSet('web', 'worker', 'markdown-worker', 'doctor', 'status')]
    [string]$Command = 'web'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Missing .venv. Install dependencies first with uv sync --extra dev.'
}
$env:PATH = "$(Join-Path $projectRoot '.venv\Scripts');$env:PATH"
Push-Location $projectRoot
try {
    & $pythonPath -m oa_knowledge.cli $Command --config (Join-Path $projectRoot 'config.yaml')
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
