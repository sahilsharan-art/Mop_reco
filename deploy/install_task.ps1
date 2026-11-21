# Make the application start by itself when the server starts, and restart if it stops: a Windows Scheduled Task
# (no extra software installed). Run once, as Administrator. It first gives the service account its folder permissions,
# then registers the task to run as that account. You type that account's Windows password; it is used only to register
# the task (Windows stores it) and is never written anywhere by this script.
param([string]$Account = '')
. "$PSScriptRoot\common.ps1"
$s = Get-Settings
if (-not $Account) { $Account = $s['RUN_AS_ACCOUNT'] }

& "$PSScriptRoot\grant_service_account.ps1" -Account $Account

$pw = Read-Host -AsSecureString "Windows password of $Account (used only to register the task)"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f (Join-Path $PSScriptRoot 'start_app.ps1')) `
        -WorkingDirectory $script:Root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User $Account -Password $plain -RunLevel Limited -Force | Out-Null
} finally { $plain = $null; [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
Write-Host "Registered scheduled task '$($script:TaskName)' (starts at boot as $Account, restarts up to 5 times)."
Write-Host "Start now : Start-ScheduledTask -TaskName $($script:TaskName)"
Write-Host "Then check: powershell -File deploy\health_check.ps1   (the app logs its NAS test at start: logs\streamlit_<date>.log)"
Write-Host "Stop      : powershell -File deploy\stop_app.ps1"
