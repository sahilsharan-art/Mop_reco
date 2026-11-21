# Run ONCE, interactively, on the production server. You type the database password here; it is stored encrypted
# (Windows DPAPI, machine scope) - never in clear text, never on a command line, never in a settings file.
# To change the password later, run it again. (There is no NAS password: the NAS login is the account the app runs as.)
param([string]$ServiceAccount = '')
. "$PSScriptRoot\common.ps1"
if (-not $ServiceAccount) { $ServiceAccount = (Get-Settings)['RUN_AS_ACCOUNT'] }
New-Item -ItemType Directory -Force -Path $script:SecretsDir | Out-Null
$db = Read-Host -AsSecureString 'Database password (user mopfinance_app on 10.0.0.205)'
$b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($db)
try { Protect-Secret ([Runtime.InteropServices.Marshal]::PtrToStringAuto($b)) (Join-Path $script:SecretsDir 'db.sec') }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
Protect-SecretsFolder $ServiceAccount
Write-Host "Saved (encrypted for this server; readable by Administrators and $ServiceAccount only). Next: deploy\install_task.ps1"
