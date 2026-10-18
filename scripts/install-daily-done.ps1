param([string]$UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$runner = Join-Path $PSScriptRoot 'run-daily-done.ps1'
$config = Join-Path $projectRoot 'config.yaml'
& (Join-Path $projectRoot '.venv\Scripts\python.exe') -X utf8 -m oa_knowledge.cli daily-done --enable --config $config
if ($LASTEXITCODE -ne 0) { throw 'Could not enable daily Done updates.' }
$arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $runner + '" -Config "' + $config + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $projectRoot
$daily = New-ScheduledTaskTrigger -Daily -At '02:00'
$logon = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $action -Trigger @($daily,$logon) -Principal $principal -Settings $settings -Description 'Read-only OA Done inventory, screened attachment download, local Markdown conversion and classification.'
Register-ScheduledTask -TaskName 'OARadar-DailyDone' -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName 'OARadar-DailyDone'
Get-ScheduledTask -TaskName 'OARadar-DailyDone' | Select-Object TaskName,State
