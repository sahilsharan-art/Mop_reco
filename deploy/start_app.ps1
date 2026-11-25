# Start the dashboard in the foreground (the scheduled task runs this hidden at start-up; run it by hand to see output).
. "$PSScriptRoot\common.ps1"
$s = Get-Settings
New-Item -ItemType Directory -Force -Path $script:LogsDir | Out-Null
$log = Join-Path $script:LogsDir ("streamlit_{0:yyyyMMdd}.log" -f (Get-Date))

# one dashboard at a time: an old process left running would serve old code (CLAUDE.md 9a)
& "$PSScriptRoot\stop_app.ps1" -Quiet

Set-AppEnvironment $s

# the NAS login is the account this task runs as; prove it works and say so in the log (the app still starts if it does not:
# the NAS is not on the reconciliation's critical path today, but the failure must be visible)
$nas = Test-NasAccess $s['NAS_FOLDER']
("{0:s} NAS {1} as {2}: {3}" -f (Get-Date), $(if ($nas.ok) { 'OK' } else { 'FAILED' }), $nas.account, $nas.message) | Add-Content $log

Set-Location $script:Root
"{0:s} starting on port {1}; database {2}; NAS folder {3}; account {4}" -f (Get-Date), $s['APP_PORT'], $s['DB_HOST'], $s['NAS_FOLDER'], $nas.account | Add-Content $log

$argList = @('-m', 'streamlit', 'run', 'app\Summary.py', '--server.port', $s['APP_PORT'], '--server.address', '0.0.0.0',
          '--server.headless', 'true')
$p = Start-Process -FilePath $script:Python -ArgumentList $argList -WorkingDirectory $script:Root -NoNewWindow -PassThru `
        -RedirectStandardOutput ($log + '.out') -RedirectStandardError ($log + '.err')
$p.Id | Set-Content $script:PidFile
# the V2 screens (API, React host, proxy) start beside the dashboard when production.env says V2_ENABLE=1; a failure there is logged and never stops the dashboard
if ($s['V2_ENABLE'] -eq '1') {
    try { & "$PSScriptRoot\start_v2.ps1" | ForEach-Object { ("{0:s} {1}" -f (Get-Date), $_) | Add-Content $log } }
    catch { ("{0:s} V2 FAILED to start: {1}" -f (Get-Date), $_.Exception.Message) | Add-Content $log }
}
$p.WaitForExit()                                            # keeps the task "running" while the app runs
"{0:s} app exited with code {1}" -f (Get-Date), $p.ExitCode | Add-Content $log
exit $p.ExitCode
