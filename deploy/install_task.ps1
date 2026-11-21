# Make the application start by itself when the server starts, and restart if it stops: a Windows Scheduled Task
# (no extra software installed). Run once, as Administrator, and give the password of the account that ran set_secrets.ps1.
param([string]$Account = ([Security.Principal.WindowsIdentity]::GetCurrent().Name))
. "$PSScriptRoot\common.ps1"
$pw = Read-Host -AsSecureString "Windows password of $Account (used only to register the task)"
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f (Join-Path $PSScriptRoot 'start_app.ps1')) `
    -WorkingDirectory $script:Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $Account -Password $plain -RunLevel Highest -Force | Out-Null
$plain = $null
Write-Host "Registered scheduled task '$($script:TaskName)' (starts at boot as $Account, restarts up to 5 times)."
Write-Host "Start now: Start-ScheduledTask -TaskName $($script:TaskName)    Stop: deploy\stop_app.ps1"
