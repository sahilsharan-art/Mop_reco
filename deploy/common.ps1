# Shared helpers for the deployment scripts. Dot-source it:  . "$PSScriptRoot\common.ps1"
# Windows PowerShell 5.1 compatible.

$ErrorActionPreference = 'Stop'
$script:Root = Split-Path -Parent $PSScriptRoot            # the application folder (contains app\, deploy\, venv\)
$script:SecretsDir = Join-Path $script:Root 'secrets'
$script:LogsDir = Join-Path $script:Root 'logs'
$script:PidFile = Join-Path $script:Root 'logs\streamlit.pid'
$script:NasProbeFile = Join-Path $script:Root 'logs\nas_probe.json'
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

# The database password is encrypted with Windows DPAPI in MACHINE scope: the file can be decrypted only on this server
# (a copy taken elsewhere is useless), it works from a scheduled task AND from a remote session, and the secrets folder
# is ACL'd to Administrators and the one service account. (User-scope DPAPI fails in a key-authenticated SSH session.)
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

# The account may be written ".\name" (local account). icacls and some Windows APIs do not understand that form, so resolve it to
# its real name (COMPUTER\name) and SID. Throws a clear message if the account does not exist.
function Resolve-ServiceAccount([string]$Account) {
    $full = if ($Account.StartsWith('.\')) { $env:COMPUTERNAME + '\' + $Account.Substring(2) } else { $Account }
    try {
        $sid = (New-Object Security.Principal.NTAccount($full)).Translate([Security.Principal.SecurityIdentifier])
    } catch {
        throw "The account '$Account' (looked up as '$full') does not exist on this computer."
    }
    return [pscustomobject]@{ Name = $full; Sid = $sid.Value }
}

function Protect-SecretsFolder([string]$ServiceAccount = '') {
    $acl = Get-Acl $script:SecretsDir
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($id in @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM')) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($id, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    if ($ServiceAccount) {      # the account the app runs as may READ (never change) the secrets
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((Resolve-ServiceAccount $ServiceAccount).Name, 'Read', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
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

function Test-NasAccess([string]$Folder) {
    # The NAS login is the Windows account this process runs as (the platform convention: other apps on this server run
    # as .\ckappuser and the NAS recognises that account). Nothing is mapped and no NAS password is stored. Proves the
    # folder can be written, read back and cleaned up, and records the outcome in logs\nas_probe.json.
    $result = [ordered]@{ checked_at = (Get-Date).ToString('s'); account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
                          folder = $Folder; ok = $false; message = '' }
    try {
        if (-not (Test-Path $Folder)) { throw "folder not reachable by this account: $Folder" }
        $dir = Join-Path $Folder '_healthcheck'
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $f = Join-Path $dir ("probe_{0:yyyyMMddHHmmss}.txt" -f (Get-Date))
        $token = [guid]::NewGuid().ToString()
        Set-Content -Path $f -Value $token
        $back = (Get-Content -Path $f).Trim()
        Remove-Item $f -Force
        if ($back -ne $token) { throw 'read-back did not match what was written' }
        $result.ok = $true
        $result.message = 'wrote, read back and deleted a probe file'
    } catch { $result.message = $_.Exception.Message }
    New-Item -ItemType Directory -Force -Path $script:LogsDir -ErrorAction SilentlyContinue | Out-Null
    try { ($result | ConvertTo-Json) | Set-Content -Path $script:NasProbeFile -Encoding UTF8 } catch { }
    return [pscustomobject]$result
}
