# Run any application script with the production environment (database address + password, require-postgres) set:
#   powershell -File deploy\run_in_app_env.ps1 scripts\db_check.py
#   powershell -File deploy\run_in_app_env.ps1 scripts\import_masters_snapshot.py --snapshot snapshots\masters_snapshot.json --by "Name"
. "$PSScriptRoot\common.ps1"
Set-Location $script:Root
# common.ps1 sets 'Stop'. When this script's output is redirected to a file, PowerShell 5.1 turns a native program's first
# stderr line (Python logs there) into a terminating error and kills the run - so a native run must never stop on stderr.
$ErrorActionPreference = 'Continue'
Set-AppEnvironment (Get-Settings)
& $script:Python @args
exit $LASTEXITCODE
