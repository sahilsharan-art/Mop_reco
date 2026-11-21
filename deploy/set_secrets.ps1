# Run ONCE, interactively, on the production server. You type the two passwords here; they are stored encrypted
# (Windows DPAPI, machine scope) - never in clear text, never on a command line, never in a settings file.
# To change a password later, run it again.
. "$PSScriptRoot\common.ps1"
New-Item -ItemType Directory -Force -Path $script:SecretsDir | Out-Null
function Plain($secure) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringAuto($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
$db = Read-Host -AsSecureString 'Database password (user mopfinance_app on 10.0.0.205)'
Protect-Secret (Plain $db) (Join-Path $script:SecretsDir 'db.sec')
$nas = Read-Host -AsSecureString 'NAS password (user ckappuser on 10.0.0.25)'
Protect-Secret (Plain $nas) (Join-Path $script:SecretsDir 'nas.sec')
Protect-SecretsFolder
Write-Host 'Saved (encrypted for this server; the folder is limited to Administrators). Next: deploy\health_check.ps1'
