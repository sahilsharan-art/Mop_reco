# Give the account the application runs as exactly the access it needs, and no more. Run as Administrator.
#   read + run   : the whole application folder (code, venv)
#   change       : logs, warehouse, db, exports, uploads_archive, uploaded_sources, uploads_staging, and the source-data folders (uploads and runs write there)
#   read only    : secrets (the encrypted database password)
# It cannot change the code, the settings or the secrets.
param([string]$Account = '', [string]$Root = '')
. "$PSScriptRoot\common.ps1"
if (-not $Account) { $Account = (Get-Settings)['RUN_AS_ACCOUNT'] }
if (-not $Root) { $Root = $script:Root }
if (-not $Account) { throw 'no account given and RUN_AS_ACCOUNT is not set in deploy\production.env' }
# icacls does not understand the dot-backslash-name form. Resolve the account to its SID once and use that everywhere.
$Resolved = Resolve-ServiceAccount $Account

function Grant([string]$Path, [string]$Rights) {
    $out = & icacls $Path /grant ("*{0}:(OI)(CI){1}" -f $Resolved.Sid, $Rights) 2>&1
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on ${Path}: $out" }
    "  {0,-8} {1}" -f $Rights, $Path
}

"Account: $Account ($($Resolved.Name), $($Resolved.Sid))    Application folder: $Root"
Grant $Root 'RX'
$changeFolders = @('logs', 'warehouse', 'db', 'exports', 'uploads_archive', 'uploaded_sources', 'uploads_staging',
                   '1. POS Sales', '2. Settlement reports', '3. CMS received', '4. Petty cash ledger', 'Customer Refund')
foreach ($name in $changeFolders) {
    $p = Join-Path $Root $name
    New-Item -ItemType Directory -Force -Path $p | Out-Null          # created now so files copied in later inherit the right
    Grant $p 'M'
}
foreach ($sub in 'Paytm', 'Axis', 'Bajaj Pay') {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "2. Settlement reports\$sub") | Out-Null
}
$secrets = Join-Path $Root 'secrets'
if (Test-Path $secrets) {
    $acl = Get-Acl $secrets
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($id in @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM')) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($id, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($Resolved.Sid)), 'Read', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    Set-Acl $secrets $acl
    "  Read     $secrets"
} else { "  (secrets folder not created yet - run set_secrets.ps1 or store_secrets_from_stdin.ps1, then run this again)" }
"Done."
