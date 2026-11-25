# Install what the MOP V2 screens need, inside THIS application folder only (nothing global, no other application touched):
#   1. the Python API packages (requirements-v2.txt, exact versions) into the application's own venv
#   2. Bun (pinned, checksum verified) into tools\bun - builds and serves the React screens
#   3. a copy of the Caddy program into tools\caddy - MOP's own proxy (the proxies of other applications are not touched)
#   4. the React app: bun install --frozen-lockfile, then bun run build
# Run as Administrator on the server, after the package is unpacked:  powershell -File deploy\install_v2.ps1
# Safe to run again. Starting the screens is a separate step (deploy\start_v2.ps1 / the scheduled task).
param([string]$CaddySource = 'D:\Citykart_Tools\caddy\caddy.exe')
. "$PSScriptRoot\common.ps1"
Set-Location $script:Root

$BunVersion = '1.4.4'
$BunSha256 = 'cd2c0fccc78f3ede1f50ca827a15f063eda13d4d150a3d0674f4f6e323d864a5'     # bun-windows-x64.zip of that release (published SHASUMS256.txt)
$BunUrl = "https://github.com/oven-sh/bun/releases/download/bun-v$BunVersion/bun-windows-x64.zip"
$tools = Join-Path $script:Root 'tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null

function Step($t) { Write-Host ""; Write-Host "== $t" }

Step '1. API packages (own venv)'
& $script:Python -m pip install --disable-pip-version-check -q -r (Join-Path $script:Root 'requirements-v2.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
$env:PYTHONPATH = Join-Path $script:Root 'app'
& $script:Python -c "import api_v2, fastapi, uvicorn; print('api_v2 imports; fastapi', fastapi.__version__, 'uvicorn', uvicorn.__version__)"
if ($LASTEXITCODE -ne 0) { throw 'the API does not import' }

Step "2. Bun $BunVersion"
$bunExe = Join-Path $tools 'bun\bun.exe'
$have = if (Test-Path $bunExe) { (& $bunExe --version) } else { '' }
if ($have -eq $BunVersion) { Write-Host "already installed ($have)" } else {
    $zip = Join-Path $tools 'bun.zip'
    Invoke-WebRequest -Uri $BunUrl -OutFile $zip -UseBasicParsing
    $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $BunSha256) { Remove-Item $zip -Force; throw "Bun download checksum mismatch (got $got) - not installed." }
    $tmp = Join-Path $tools 'bun_unpack'
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $tools 'bun') | Out-Null
    Copy-Item (Join-Path $tmp 'bun-windows-x64\bun.exe') $bunExe -Force
    Remove-Item $tmp -Recurse -Force; Remove-Item $zip -Force
    Write-Host ("installed " + (& $bunExe --version))
}

Step '3. MOP proxy (own copy of the Caddy program)'
$caddyExe = Join-Path $tools 'caddy\caddy.exe'
if (Test-Path $caddyExe) { Write-Host 'already present' } else {
    if (-not (Test-Path $CaddySource)) { throw "Caddy program not found at $CaddySource - pass -CaddySource <path to caddy.exe>." }
    New-Item -ItemType Directory -Force -Path (Join-Path $tools 'caddy') | Out-Null
    Copy-Item $CaddySource $caddyExe        # a copy: the other applications' own program is read, never changed
}
& $caddyExe version

Step '4. React app (frozen lock file, then build)'
$env:BUN_INSTALL_CACHE_DIR = Join-Path $tools 'bun-cache'
Push-Location (Join-Path $script:Root 'frontend')
try {
    & $bunExe install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw 'bun install failed' }
    & $bunExe run build
    if ($LASTEXITCODE -ne 0) { throw 'bun run build failed' }
} finally { Pop-Location }
if (-not (Test-Path (Join-Path $script:Root 'frontend\dist\server\server.js'))) { throw 'the build produced no frontend\dist\server\server.js' }

Write-Host ""
Write-Host 'V2 installed. Next: set V2_ENABLE=1, V2_ORIGIN=<address users type> in deploy\production.env, then start it (deploy\start_v2.ps1 -Detached, or restart the scheduled task).'
