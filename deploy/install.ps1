# Install or refresh the Python packages for the application, inside THIS application's own folder only.
# Touches nothing outside the application folder. Safe to run again after an update.
. "$PSScriptRoot\common.ps1"
Set-Location $script:Root          # relative paths below (app\, requirements.txt) always mean THIS application folder
if (-not (Test-Path $script:Python)) {
    throw "The application's own Python environment is missing: $script:Python. Create it first (python -m venv venv) using the application's own Python 3.14 (CLAUDE.md section 3)."
}
foreach ($d in 'logs', 'secrets', 'warehouse', 'db', 'exports', 'uploads_archive') {
    New-Item -ItemType Directory -Force -Path (Join-Path $script:Root $d) | Out-Null
}
# Prefer the lock file (the exact 63 packages proven on the server, audit S-9); fall back to the 17 direct pins.
$req = Join-Path $script:Root 'requirements.lock.txt'
if (-not (Test-Path $req)) { $req = Join-Path $script:Root 'requirements.txt' }
Write-Host "Installing packages from $(Split-Path $req -Leaf)"
& $script:Python -m pip install --disable-pip-version-check -r $req
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

# smoke test: every pinned package imports, and the application's own modules load
& $script:Python -c "import pandas, sqlalchemy, streamlit, psycopg, pyarrow, polars, fastexcel, openpyxl, xlsxwriter, pyxlsb; import sys; sys.path.insert(0, 'app'); import mop_reco.db, mop_reco.masters_db, mop_reco.runs; print('packages and application modules import OK; streamlit', streamlit.__version__)"
if ($LASTEXITCODE -ne 0) { throw 'smoke test failed' }
if (-not (Test-Path (Join-Path $PSScriptRoot 'production.env'))) {
    Copy-Item (Join-Path $PSScriptRoot 'production.env.example') (Join-Path $PSScriptRoot 'production.env')
    Write-Host 'Created deploy\production.env from the example - check the values, then run deploy\set_secrets.ps1'
}
