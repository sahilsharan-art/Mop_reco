# Start the dashboard in the foreground (the scheduled task runs this hidden at start-up; run it by hand to see output).
. "$PSScriptRoot\common.ps1"
$s = Get-Settings
New-Item -ItemType Directory -Force -Path $script:LogsDir | Out-Null

# one dashboard at a time: an old process left running would serve old code (CLAUDE.md 9a)
& "$PSScriptRoot\stop_app.ps1" -Quiet

Set-AppEnvironment $s
Connect-Nas $s

$log = Join-Path $script:LogsDir ("streamlit_{0:yyyyMMdd}.log" -f (Get-Date))
Set-Location $script:Root
"{0:s} starting on port {1}; database {2}; NAS {3}" -f (Get-Date), $s['APP_PORT'], $s['DB_HOST'], $s['NAS_FOLDER'] | Add-Content $log

$argList = @('-m', 'streamlit', 'run', 'app\Summary.py', '--server.port', $s['APP_PORT'], '--server.address', '0.0.0.0',
          '--server.headless', 'true')
$p = Start-Process -FilePath $script:Python -ArgumentList $argList -WorkingDirectory $script:Root -NoNewWindow -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError ($log + '.err')
$p.Id | Set-Content $script:PidFile
$p.WaitForExit()                                            # keeps the task "running" while the app runs
"{0:s} app exited with code {1}" -f (Get-Date), $p.ExitCode | Add-Content $log
exit $p.ExitCode
