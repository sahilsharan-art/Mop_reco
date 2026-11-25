param([switch]$Quiet)
# Stop the MOP V2 screens (API, React host, proxy) - only processes started from THIS application folder, never other applications'.
$here = Split-Path -Parent $PSScriptRoot
$stopped = 0
$pidFile = Join-Path $here 'logs\v2.pids'
if (Test-Path $pidFile) {
    try {
        $ids = Get-Content $pidFile -Raw | ConvertFrom-Json
        foreach ($name in 'api', 'web', 'proxy') {
            $id = $ids.$name
            if ($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; $stopped++ }
        }
    } catch { }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}
# anything else of ours still running, matched by this folder's own executables
$py = Join-Path $here 'venv\Scripts\python.exe'
$bun = Join-Path $here 'tools\bun\bun.exe'
$caddy = Join-Path $here 'tools\caddy\caddy.exe'
Get-CimInstance Win32_Process | Where-Object {
    # the venv launcher and the interpreter it starts both carry the command line; both live under this folder
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($here, [StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match 'api_v2') -or
    ($_.ExecutablePath -eq $bun) -or ($_.ExecutablePath -eq $caddy)
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $stopped++ }
if (-not $Quiet) { Write-Host "Stopped $stopped V2 process(es)." }
