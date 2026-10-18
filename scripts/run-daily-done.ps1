param([string]$Config = (Join-Path (Split-Path $PSScriptRoot -Parent) 'config.yaml'))
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Missing project Python environment.' }
Push-Location $projectRoot
try {
    $desktop = Start-Process -FilePath $pythonPath -ArgumentList @('-X','utf8','-m','oa_knowledge.desktop_collection',$Config) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $projectRoot 'runtime\desktop-collection.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'runtime\desktop-collection.stderr.log') -PassThru
    # Always start the worker supervisor. The worker's existing exclusive lock
    # prevents duplicate browser/queue consumers if one is already running.
    $worker = Start-Process -FilePath $pythonPath -ArgumentList @('-X','utf8','-m','oa_knowledge.cli','worker','--config',$Config) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $projectRoot 'runtime\daily-worker.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'runtime\daily-worker.stderr.log') -PassThru
    $coordinator = Start-Process -FilePath $pythonPath -ArgumentList @('-X','utf8','-m','oa_knowledge.cli','daily-done','--watch','--config',$Config) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $projectRoot 'runtime\daily-done.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'runtime\daily-done.stderr.log') -PassThru
    $coordinator.WaitForExit()
    exit $coordinator.ExitCode
} finally { Pop-Location }
