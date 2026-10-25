param([string]$UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$runner = Join-Path $PSScriptRoot 'watch-oa-processes.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $runner + '"') -WorkingDirectory $projectRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $action -Trigger @($logon,$repeat) -Principal $principal -Settings $settings -Description 'Keep local read-only OA workers available and refresh durable audit progress; preserve paused tasks.'
Register-ScheduledTask -TaskName 'OARadar-ProcessWatchdog' -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName 'OARadar-ProcessWatchdog'
Get-ScheduledTask -TaskName 'OARadar-ProcessWatchdog' | Select-Object TaskName,State
