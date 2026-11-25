# One command that says whether the installation is healthy: app, database, masters, NAS, task, disk. Exit code 0 = all good.
. "$PSScriptRoot\common.ps1"
$s = Get-Settings
$bad = 0
function Report($name, $ok, $detail) {
    $mark = if ($ok) { 'OK  ' } else { 'FAIL' }
    Write-Host ("[{0}] {1} - {2}" -f $mark, $name, $detail)
    if (-not $ok) { $script:bad++ }
}

# 1. the app answers
try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri ("http://localhost:{0}/_stcore/health" -f $s['APP_PORT']) -TimeoutSec 10
    Report 'app' ($r.StatusCode -eq 200) ("port {0} answers '{1}'" -f $s['APP_PORT'], $r.Content.Trim())
} catch { Report 'app' $false "not answering on port $($s['APP_PORT']): $($_.Exception.Message)" }

# 2. the startup task, and the account it runs as
try {
    $t = Get-ScheduledTask -TaskName $script:TaskName -ErrorAction Stop
    $info = Get-ScheduledTaskInfo -TaskName $script:TaskName
    Report 'task' ($t.Principal.UserId -match [regex]::Escape(($s['RUN_AS_ACCOUNT'] -replace '^\.\\', ''))) `
        ("'{0}' state {1}, runs as {2}, last result {3}" -f $script:TaskName, $t.State, $t.Principal.UserId, $info.LastTaskResult)
} catch { Report 'task' $false "scheduled task '$($script:TaskName)' not registered (run deploy\install_task.ps1)" }

# 3. database + masters (uses the app's own code and its own connection settings)
try {
    Set-AppEnvironment $s
    $out = & $script:Python (Join-Path $script:Root 'scripts\db_check.py') 2>&1
    Report 'database' ($LASTEXITCODE -eq 0) (($out | Where-Object { $_ -notmatch '^INFO' } | Select-Object -Last 6) -join ' | ')
} catch { Report 'database' $false $_.Exception.Message }

# 4. the NAS, as the account the app runs as. If this check is run by another account (an administrator), it reports what the
#    app itself found when it last started, instead of trying with the wrong identity.
$runAs = ($s['RUN_AS_ACCOUNT'] -replace '^\.\\', '')
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($me -match [regex]::Escape($runAs)) {
    $n = Test-NasAccess $s['NAS_FOLDER']
    Report 'NAS' $n.ok ("as {0}: {1}" -f $n.account, $n.message)
} elseif (Test-Path $script:NasProbeFile) {
    $n = Get-Content $script:NasProbeFile -Raw | ConvertFrom-Json
    Report 'NAS' ([bool]$n.ok) ("as the app account at its last start ({0}, {1}): {2}" -f $n.account, $n.checked_at, $n.message)
} else { Report 'NAS' $false "no NAS probe recorded yet - start the app (it probes at start) or run this check as $runAs" }

# 4b. the V2 screens, when enabled: the proxy answers, the API behind it answers, and the React host serves the sign-in page
if ($s['V2_ENABLE'] -eq '1') {
    $v2port = if ($s['V2_PORT']) { $s['V2_PORT'] } else { '8091' }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri ("http://localhost:{0}/api/mop/health" -f $v2port) -TimeoutSec 15
        Report 'v2 api' ($r.StatusCode -eq 200) ("through the proxy on port {0}: {1}" -f $v2port, ($r.Content.Trim() -replace '\s+', ' '))
    } catch { Report 'v2 api' $false "not answering through port ${v2port}: $($_.Exception.Message)" }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri ("http://localhost:{0}/login" -f $v2port) -TimeoutSec 20
        Report 'v2 screens' ($r.StatusCode -eq 200 -and $r.Content.Length -gt 500) ("sign-in page served ({0} bytes)" -f $r.Content.Length)
    } catch { Report 'v2 screens' $false "sign-in page not served on port ${v2port}: $($_.Exception.Message)" }
}

# 5. free disk on the application drive
$drive = Get-PSDrive -Name ($script:Root.Substring(0, 1))
$freeGb = [math]::Round($drive.Free / 1GB, 1)
Report 'disk' ($freeGb -ge 20) "$freeGb GB free on $($drive.Name):"

if ($bad -gt 0) { Write-Host "$bad check(s) FAILED"; exit 1 }
Write-Host 'All checks passed.'
