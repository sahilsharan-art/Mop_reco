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

# Secrets are encrypted with Windows DPAPI in MACHINE scope: the file can be decrypted only on this server (a copy taken
# elsewhere is useless), it works from a scheduled task AND from a remote session, and the secrets folder is ACL'd to
# Administrators only. (User-scope DPAPI fails in a key-authenticated SSH session, which has no loaded user profile.)
Add-Type -AssemblyName System.Security
$script:SecretEntropy = [Text.Encoding]::UTF8.GetBytes('MOP-Reconciliation-secret-v1')

function Protect-Secret([string]$Plain, [string]$File) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Plain)
    $enc = [Security.Cryptography.ProtectedData]::Protect($bytes, $script:SecretEntropy, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    [Convert]::ToBase64String($enc) | Set-Content -Path $File -Encoding ASCII
}

function Unprotect-Secret([string]$File) {
    if (-not (Test-Path $File)) { throw "Secret file missing: $File - run deploy\set_secrets.ps1 first." }
    $enc = [Convert]::FromBase64String((Get-Content $File -Raw).Trim())
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect($enc, $script:SecretEntropy, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    return [Text.Encoding]::UTF8.GetString($bytes)
}

function Protect-SecretsFolder {
    $acl = Get-Acl $script:SecretsDir
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($id in @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM')) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($id, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    Set-Acl $script:SecretsDir $acl
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
