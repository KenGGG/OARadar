[CmdletBinding()]
param([switch]$StartOnlineAudit)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script in Administrator PowerShell using the Windows account that saved the OA browser password.'
}
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.yaml'
Push-Location $projectRoot
try {
    & $pythonPath -X utf8 'tools\prepare_admin_cleanup.py'
    if ($LASTEXITCODE -ne 0) { throw 'Safety verification failed; nothing was deleted.' }
    $planPath = Join-Path $projectRoot 'runtime\state\runtime\admin-cleanup-plan.json'
    $plan = Get-Content -LiteralPath $planPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $results = @()
    foreach ($entry in $plan.targets) {
        $targetPath = [IO.Path]::GetFullPath([string]$entry.path)
        if (-not $targetPath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Deletion target outside project.'
        }
        $relative = $targetPath.Substring($projectRoot.Length + 1)
        $allowed = ($relative -match '^runtime\\(test-[^\\]+|repair-probe[^\\]*|publication-acl-smoke[^\\]*|longpath-check[^\\]*)$') -or
            ($relative -match '^runtime\\[^\\]+\.log$') -or ($relative -eq 'runtime\tools\downloads') -or
            ($relative -match '^runtime\\cache\\work\\parse\\[0-9]+$') -or
            ($relative -match '^runtime\\cleanup-remnants\\[^\\]+\\[^\\]+$') -or
            ($relative -match '^runtime\\state\\runtime\\duplicate-attachment-backup\\[0-9]{8}T[0-9]{6}Z$')
        if (-not $allowed) { throw "Target outside permitted cleanup categories: $relative" }
        try {
            if (Test-Path -LiteralPath $targetPath) {
                $item = Get-Item -LiteralPath $targetPath -Force
                if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Link target preserved.' }
                if ($item.PSIsContainer) {
                    $link = Get-ChildItem -LiteralPath $targetPath -Recurse -Force -ErrorAction Stop |
                        Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 } | Select-Object -First 1
                    if ($link) { throw 'Tree contains a link; preserved.' }
                }
                Remove-Item -LiteralPath $targetPath -Recurse -Force -ErrorAction Stop
            }
            $results += [pscustomobject]@{path=$targetPath;kind=$entry.kind;status='removed'}
        } catch {
            $results += [pscustomobject]@{path=$targetPath;kind=$entry.kind;status='preserved';reason=$_.Exception.Message}
        }
    }
    $plan | Add-Member -NotePropertyName results -NotePropertyValue $results -Force
    $plan | Add-Member -NotePropertyName completed_at -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force
    $plan | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $planPath -Encoding UTF8
    & $pythonPath -X utf8 'tools\prepare_admin_cleanup.py' --record-results
    if ($LASTEXITCODE -ne 0) { throw 'Cleanup finished; recovery report update needs attention.' }
    Write-Output "Removed $(@($results | Where-Object status -eq 'removed').Count) targets; preserved $(@($results | Where-Object status -eq 'preserved').Count)."
    Write-Output "Current database/config recovery point: $($plan.current_recovery_snapshot)"

    if ($StartOnlineAudit) {
        & $pythonPath -c "from pathlib import Path; import sqlite3; from oa_knowledge.config import load_settings; s=load_settings(Path('config.yaml')); db=sqlite3.connect(s.database_path); busy=db.execute('SELECT 1 FROM operation_jobs WHERE status IN (?,?)',('running','queued')).fetchone() or db.execute('SELECT 1 FROM parse_jobs WHERE status=?',('running',)).fetchone() or db.execute('SELECT 1 FROM pipeline_tasks WHERE status=?',('running',)).fetchone(); db.close(); raise SystemExit(1 if busy else 0)"
        if ($LASTEXITCODE -ne 0) { throw 'Cleanup finished, but an operation is active. No workers were restarted; launch audit after it finishes.' }
        $webPort = & $pythonPath -c "from pathlib import Path; from oa_knowledge.config import load_settings; print(load_settings(Path('config.yaml')).web.port)"
        if ($LASTEXITCODE -ne 0) { throw 'Could not read web port.' }
        $webServers = @()
        foreach ($listener in @(Get-NetTCPConnection -LocalPort ([int]$webPort) -State Listen -ErrorAction SilentlyContinue)) {
            $server = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
            if ($server.CommandLine -notmatch 'oa_knowledge|uvicorn') { throw 'Web port belongs to an unrecognized process; nothing was restarted.' }
            $webServers += $server
        }
        # Reload OA workers only after confirming that no OA/parse operation is active.
        # User browser profiles, originals and local conversion services are preserved.
        $scheduled = Get-ScheduledTask -TaskName 'OARadar-DailyDone' -ErrorAction SilentlyContinue
        if ($scheduled) { Stop-ScheduledTask -TaskName 'OARadar-DailyDone' -ErrorAction SilentlyContinue }
        $oldWorkers = @(Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -and $_.CommandLine.Contains($configPath) -and
            $_.CommandLine -match 'oa_knowledge\.(desktop_collection|cli\s+(worker|daily-done))'
        })
        foreach ($worker in $oldWorkers) {
            if (Get-Process -Id $worker.ProcessId -ErrorAction SilentlyContinue) {
                & taskkill /PID $worker.ProcessId /T /F | Out-Null
            }
        }
        if ($scheduled) {
            Start-ScheduledTask -TaskName 'OARadar-DailyDone'
        } else {
            Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + (Join-Path $PSScriptRoot 'run-daily-done.ps1') + '"')) -WorkingDirectory $projectRoot -WindowStyle Hidden
        }
        foreach ($server in $webServers) {
            if (Get-Process -Id $server.ProcessId -ErrorAction SilentlyContinue) { & taskkill /PID $server.ProcessId /T /F | Out-Null }
        }
        Start-Process -FilePath $pythonPath -ArgumentList @('-X','utf8','-m','oa_knowledge.cli','web','--config',('"' + $configPath + '"')) -WorkingDirectory $projectRoot -WindowStyle Hidden
        Start-Process -FilePath $pythonPath -ArgumentList @('-X','utf8',('"' + (Join-Path $projectRoot 'tools\start_online_inventory_audit.py') + '"')) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $projectRoot 'runtime\online-inventory-audit.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'runtime\online-inventory-audit.stderr.log')
        Write-Output 'Online audit launched: refresh manifest, open every OA detail, compare inventories, queue missing downloads.'
        Write-Output 'Progress: runtime\state\runtime\online-inventory-audit-progress.json'
    }
} finally { Pop-Location }
