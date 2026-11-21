param([switch]$Quiet)
# Stop the dashboard: the process recorded in logs\streamlit.pid, then any other streamlit started from this folder.
$here = Split-Path -Parent $PSScriptRoot
$stopped = 0
$pidFile = Join-Path $here 'logs\streamlit.pid'
if (Test-Path $pidFile) {
    $id = [int](Get-Content $pidFile)
    $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($proc) { Stop-Process -Id $id -Force; $stopped++ }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}
# anything else running THIS application's streamlit (matched by this folder's own python), never other apps' processes
$mine = Join-Path $here 'venv\Scripts\python.exe'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -eq $mine -and $_.CommandLine -match 'streamlit' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $stopped++ }
if (-not $Quiet) { Write-Host "Stopped $stopped process(es)." }
