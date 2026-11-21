# Run ONCE, interactively, on the production server, as the SAME Windows account that will run the app.
# You type the two passwords here; they are stored encrypted (Windows DPAPI) for this account only.
# They are never written in clear text, never passed on a command line, never put in a settings file.
. "$PSScriptRoot\common.ps1"
New-Item -ItemType Directory -Force -Path $script:SecretsDir | Out-Null

$db = Read-Host -AsSecureString 'Database password (user mopfinance_app on 10.0.0.205)'
$db | ConvertFrom-SecureString | Set-Content (Join-Path $script:SecretsDir 'db.sec')
$nas = Read-Host -AsSecureString 'NAS password (user ckappuser on 10.0.0.25)'
$nas | ConvertFrom-SecureString | Set-Content (Join-Path $script:SecretsDir 'nas.sec')

# only this account (and administrators) may read the secrets folder
$acl = Get-Acl $script:SecretsDir
$acl.SetAccessRuleProtection($true, $false)
foreach ($id in @([Security.Principal.WindowsIdentity]::GetCurrent().Name, 'BUILTIN\Administrators')) {
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($id, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
}
Set-Acl $script:SecretsDir $acl
Write-Host "Saved. Account: $([Security.Principal.WindowsIdentity]::GetCurrent().Name). Next: deploy\health_check.ps1"
