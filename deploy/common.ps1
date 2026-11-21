# Shared helpers for the deployment scripts. Dot-source it:  . "$PSScriptRoot\common.ps1"
# Windows PowerShell 5.1 compatible.

$ErrorActionPreference = 'Stop'
$script:Root = Split-Path -Parent $PSScriptRoot            # the application folder (contains app\, deploy\, venv\)
$script:SecretsDir = Join-Path $script:Root 'secrets'
$script:LogsDir = Join-Path $script:Root 'logs'
$script:PidFile = Join-Path $script:Root 'logs\streamlit.pid'
$script:Python = Join-Path $script:Root 'venv\Scripts\python.exe'
$script:TaskName = 'MOP-Reconciliation-App'

function Read-EnvFile([string]$Path) {
    if (-not (Test-Path $Path)) { throw "Settings file not found: $Path (copy deploy\production.env.example to deploy\production.env and edit it)" }
    $h = @{}
    foreach ($line in Get-Content $Path) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $h[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
    }
    return $h
}

function Get-Settings { return Read-EnvFile (Join-Path $PSScriptRoot 'production.env') }

function Unprotect-Secret([string]$File) {
    if (-not (Test-Path $File)) { throw "Secret file missing: $File - run deploy\set_secrets.ps1 first (as the SAME Windows account that runs the app)." }
    $secure = Get-Content $File | ConvertTo-SecureString          # DPAPI: readable only by the account that wrote it
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Set-AppEnvironment($s) {
    # the database address and password reach the app only as environment variables of its own process
    $env:MOP_DATABASE_URL = "postgresql+psycopg://$($s['DB_USER'])@$($s['DB_HOST']):$($s['DB_PORT'])/$($s['DB_NAME'])"
    $env:MOPFINANCE_DB_PASSWORD = Unprotect-Secret (Join-Path $script:SecretsDir 'db.sec')
    $env:MOP_REQUIRE_POSTGRES = '1'                              # never fall back to a local SQLite file
    $env:PYTHONUNBUFFERED = '1'
}

function Connect-Nas($s) {
    # a UNC-only mapping with the NAS account (no drive letter, so it works for a background task)
    $pw = Unprotect-Secret (Join-Path $script:SecretsDir 'nas.sec')
    $existing = Get-SmbMapping -RemotePath $s['NAS_SHARE'] -ErrorAction SilentlyContinue
    if ($existing) { Remove-SmbMapping -RemotePath $s['NAS_SHARE'] -Force -UpdateProfile -ErrorAction SilentlyContinue }
    New-SmbMapping -RemotePath $s['NAS_SHARE'] -UserName $s['NAS_USER'] -Password $pw | Out-Null
    if (-not (Test-Path $s['NAS_FOLDER'])) { throw "NAS folder not reachable: $($s['NAS_FOLDER'])" }
}
