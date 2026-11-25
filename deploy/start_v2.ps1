param([switch]$Detached)
# Start the MOP V2 screens beside the existing dashboard: the Python API (127.0.0.1:8092), the React host (127.0.0.1:8093) and
# MOP's own proxy (V2_PORT, default 8091), which is the only address users open. Only when V2_ENABLE=1 in production.env.
# The old dashboard on APP_PORT is untouched and stays as the rollback.
#
# -Detached: start through Windows itself instead of as a child of this session. Use it from a remote (ssh) session, where
# anything started normally is killed when the session closes. The scheduled task needs no switch (start_app.ps1 calls this).
. "$PSScriptRoot\common.ps1"
$s = Get-Settings
if ($s['V2_ENABLE'] -ne '1') { Write-Host 'V2_ENABLE is not 1 in production.env - nothing to start.'; exit 0 }

if ($Detached) {
    $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $script:Root }
    Write-Host ("started detached: return code {0}, pid {1}" -f $r.ReturnValue, $r.ProcessId)
    exit $r.ReturnValue
}

$origin = $s['V2_ORIGIN']
if (-not $origin -or $origin -notmatch '^https?://[^/]+$') { throw 'V2_ORIGIN in production.env must be the exact address users type, e.g. http://host:8091 (no trailing slash, no path).' }
$port = if ($s['V2_PORT']) { $s['V2_PORT'] } else { '8091' }
$bun = Join-Path $script:Root 'tools\bun\bun.exe'
$caddy = Join-Path $script:Root 'tools\caddy\caddy.exe'
$front = Join-Path $script:Root 'frontend'
foreach ($need in @($bun, $caddy, (Join-Path $front 'dist\server\server.js'))) {
    if (-not (Test-Path $need)) { throw "Missing $need - run deploy\install_v2.ps1 first." }
}

& "$PSScriptRoot\stop_v2.ps1" -Quiet           # one set at a time: an old process would serve old code
Set-AppEnvironment $s
$env:MOP_V2_ORIGIN = $origin
$env:MOP_V2_PORT = $port
$env:PYTHONPATH = Join-Path $script:Root 'app'
$env:PORT = '8093'
$env:MOP_CADDY_DATA = Join-Path $script:Root 'tools\caddy\data'
New-Item -ItemType Directory -Force -Path $env:MOP_CADDY_DATA | Out-Null
New-Item -ItemType Directory -Force -Path $script:LogsDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd'
$pids = @{}

# 1. the API (one worker only: reconciliation runs started from the screens use one in-process worker)
$log = Join-Path $script:LogsDir "v2_api_$stamp"
$p = Start-Process -FilePath $script:Python -WorkingDirectory $script:Root -NoNewWindow -PassThru `
        -ArgumentList @('-m', 'uvicorn', 'api_v2:app', '--app-dir', 'app', '--host', '127.0.0.1', '--port', '8092', '--workers', '1') `
        -RedirectStandardOutput ($log + '.out') -RedirectStandardError ($log + '.err')
$pids['api'] = $p.Id
# 2. the React host
$log = Join-Path $script:LogsDir "v2_web_$stamp"
$p = Start-Process -FilePath $bun -WorkingDirectory $front -NoNewWindow -PassThru -ArgumentList @('serve.ts') `
        -RedirectStandardOutput ($log + '.out') -RedirectStandardError ($log + '.err')
$pids['web'] = $p.Id
# 3. MOP's own proxy
$log = Join-Path $script:LogsDir "v2_proxy_$stamp"
$p = Start-Process -FilePath $caddy -WorkingDirectory $script:Root -NoNewWindow -PassThru `
        -ArgumentList @('run', '--config', (Join-Path $PSScriptRoot 'Caddyfile.mop_v2'), '--adapter', 'caddyfile') `
        -RedirectStandardOutput ($log + '.out') -RedirectStandardError ($log + '.err')
$pids['proxy'] = $p.Id
($pids | ConvertTo-Json) | Set-Content (Join-Path $script:LogsDir 'v2.pids') -Encoding ASCII
"{0:s} MOP V2 started: api {1}, web {2}, proxy {3}; users open {4}" -f (Get-Date), $pids['api'], $pids['web'], $pids['proxy'], $origin |
    Add-Content (Join-Path $script:LogsDir "v2_$stamp.log")
Write-Host ("MOP V2 started: api {0}, web {1}, proxy {2}; open {3}" -f $pids['api'], $pids['web'], $pids['proxy'], $origin)
