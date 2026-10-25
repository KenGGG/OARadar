$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.yaml'
Push-Location $projectRoot
try {
    # Inspect both venv launchers and their Python children; never kill an active job.
    $processes = @(Get-CimInstance Win32_Process -Filter "name = 'python.exe'" -ErrorAction Stop)
    foreach ($role in @('desktop_collection', 'worker', 'daily-done')) {
        $pattern = switch ($role) {
            'desktop_collection' { '-m\s+oa_knowledge\.desktop_collection\s' }
            'worker' { '-m\s+oa_knowledge\.cli\s+worker\s' }
            'daily-done' { '-m\s+oa_knowledge\.cli\s+daily-done\s' }
        }
        $existing = @($processes | Where-Object {
            $_.CommandLine -match $pattern -and
            $_.CommandLine.Replace('/', '\').IndexOf($projectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0
        })
        if ($existing.Count -eq 0) {
            $arguments = if ($role -eq 'desktop_collection') {
                @('-X','utf8','-m','oa_knowledge.desktop_collection', ('"' + $configPath + '"'))
            } elseif ($role -eq 'daily-done') {
                @('-X','utf8','-m','oa_knowledge.cli','daily-done','--watch','--config', ('"' + $configPath + '"'))
            } else {
                @('-X','utf8','-m','oa_knowledge.cli','worker','--config', ('"' + $configPath + '"'))
            }
            Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden
        }
    }
    & $pythonPath -X utf8 'tools\refresh_online_audit_progress.py'
    if ($LASTEXITCODE -ne 0) { throw 'Could not refresh audit progress.' }
} finally { Pop-Location }
