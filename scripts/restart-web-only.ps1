$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.yaml'
# Only reload this project's web UI. Leave browser and collection workers running.
$processes = @(Get-CimInstance Win32_Process -Filter "name = 'python.exe'")
$webProcesses = @($processes | Where-Object {
    $_.CommandLine -match '-m\s+oa_knowledge\.cli\s+web\s' -and
    $_.CommandLine.Replace('/', '\').IndexOf($projectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0
})
foreach ($process in $webProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
Start-Process -FilePath $pythonPath -ArgumentList @('-X','utf8','-m','oa_knowledge.cli','web','--config',('"' + $configPath + '"')) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $projectRoot 'runtime\web-refresh.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'runtime\web-refresh.stderr.log')
