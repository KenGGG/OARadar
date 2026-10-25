# Run from the Windows account that will also run the daily scheduled task.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.yaml'
Push-Location $projectRoot
try {
    # Refuse to interrupt a scan or download that is still writing its checkpoint.
    & $pythonPath -c "from pathlib import Path; from sqlalchemy import select; from sqlalchemy.orm import Session; from oa_knowledge.config import load_settings; from oa_knowledge.db.engine import create_db_engine; from oa_knowledge.db.models import OperationJob; s=load_settings(Path('config.yaml')); e=create_db_engine(s.database_path); db=Session(e); active=db.scalar(select(OperationJob.id).where(OperationJob.status.in_(['running','queued']))); db.close(); e.dispose(); raise SystemExit(1 if active else 0)"
    if ($LASTEXITCODE -ne 0) { throw 'Pause or finish active OA jobs before restarting.' }
    $port = & $pythonPath -c "from pathlib import Path; from oa_knowledge.config import load_settings; print(load_settings(Path('config.yaml')).web.port)"
    if ($LASTEXITCODE -ne 0) { throw 'Could not read local configuration.' }
    $listeners = @(Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $server = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        if ($server.CommandLine -notmatch 'oa_knowledge|uvicorn') {
            throw 'The configured port is used by an unrecognized process; no process was stopped.'
        }
        & taskkill /PID $server.ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop the old web server. Run this script as its Windows user.' }
    }
    $collectors = @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and $_.CommandLine.Contains($configPath) -and
        $_.CommandLine -match 'oa_knowledge\.(desktop_collection|cli daily-done)'
    })
    foreach ($collector in $collectors) {
        if (Get-Process -Id $collector.ProcessId -ErrorAction SilentlyContinue) {
            & taskkill /PID $collector.ProcessId /T /F | Out-Null
            if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $collector.ProcessId -ErrorAction SilentlyContinue)) {
                throw 'Could not stop the old browser coordinator. Use its Windows account.'
            }
        }
    }
    # Remove only this application's old OA entry, never other saved credentials.
    & $pythonPath -c "from pathlib import Path; from oa_knowledge.config import load_settings; from oa_knowledge.collector.windows_credentials import delete_credential; delete_credential(load_settings(Path('config.yaml')).browser.base_url)"
    if ($LASTEXITCODE -ne 0) { throw 'Could not remove the legacy OA credential entry.' }
    $webArgs = @('-X','utf8','-m','oa_knowledge.cli','web','--config',('"' + $configPath + '"'))
    Start-Process -FilePath $pythonPath -ArgumentList $webArgs -WorkingDirectory $projectRoot -WindowStyle Hidden
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + (Join-Path $PSScriptRoot 'run-daily-done.ps1') + '"')) -WorkingDirectory $projectRoot -WindowStyle Hidden
    Write-Output "Restarted as $([Security.Principal.WindowsIdentity]::GetCurrent().Name). Refresh http://127.0.0.1:$port and use browser login to save the OA password in Chrome."
} finally { Pop-Location }
