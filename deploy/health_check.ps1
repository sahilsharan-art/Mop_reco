# One command that says whether the installation is healthy: app, database, masters, NAS. Exit code 0 = all good.
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

# 2. database + masters (uses the app's own code and its own connection settings)
try {
    Set-AppEnvironment $s
    $out = & $script:Python (Join-Path $script:Root 'scripts\db_check.py') 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    Report 'database' $ok (($out | Select-Object -Last 6) -join ' | ')
} catch { Report 'database' $false $_.Exception.Message }

# 3. the NAS: write, read back, delete
try {
    Connect-Nas $s
    $dir = Join-Path $s['NAS_FOLDER'] '_healthcheck'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $f = Join-Path $dir ("probe_{0:yyyyMMddHHmmss}.txt" -f (Get-Date))
    $token = [guid]::NewGuid().ToString()
    Set-Content -Path $f -Value $token
    $back = (Get-Content -Path $f).Trim()
    Remove-Item $f -Force
    Report 'NAS' ($back -eq $token) "write, read back and delete on $($s['NAS_FOLDER'])"
} catch { Report 'NAS' $false $_.Exception.Message }

# 4. free disk on the application drive
$drive = Get-PSDrive -Name ($script:Root.Substring(0, 1))
$freeGb = [math]::Round($drive.Free / 1GB, 1)
Report 'disk' ($freeGb -ge 20) "$freeGb GB free on $($drive.Name):"

if ($bad -gt 0) { Write-Host "$bad check(s) FAILED"; exit 1 }
Write-Host 'All checks passed.'
