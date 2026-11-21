# Run any application script with the production environment (database address + password, require-postgres) set:
#   powershell -File deploy\run_in_app_env.ps1 scripts\db_check.py
#   powershell -File deploy\run_in_app_env.ps1 scripts\import_masters_snapshot.py --snapshot snapshots\masters_snapshot.json --by "Name"
. "$PSScriptRoot\common.ps1"
Set-Location $script:Root
Set-AppEnvironment (Get-Settings)
& $script:Python @args
exit $LASTEXITCODE
