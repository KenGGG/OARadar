param(
    [ValidateSet('standard', 'basic', 'flash')]
    [string]$Tier = 'standard',
    [int]$Port = 0
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if ($Port -eq 0) {
    Push-Location $projectRoot
    try {
        $configuredPort = & (Join-Path $projectRoot '.venv\Scripts\python.exe') -X utf8 -c 'import sys; from pathlib import Path; from urllib.parse import urlsplit; from oa_knowledge.config import load_settings; print(urlsplit(load_settings(Path(sys.argv[1])).mineru.api_url).port or 58000)' (Join-Path $projectRoot 'config.yaml')
        if ($LASTEXITCODE -ne 0) { throw 'Could not read configured MinerU port.' }
        $Port = [int]$configuredPort
    } finally { Pop-Location }
}
$kit = Join-Path $projectRoot '.mineru\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $kit)) { throw 'Install MinerU 4.0.11 in .mineru first.' }
$env:MINERU_HOME = Join-Path $projectRoot 'data\mineru'
$env:MINERU_MODEL_SOURCE = 'local'
& $kit (Join-Path $PSScriptRoot 'mineru-local.py') api-server --host 127.0.0.1 --port $Port --tier $Tier --concurrency 1 --disable-image-analysis --upload-dir (Join-Path $env:MINERU_HOME 'uploads')
exit $LASTEXITCODE
