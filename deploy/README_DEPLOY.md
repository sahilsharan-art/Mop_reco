# MOP Reconciliation - production runbook (server 10.0.1.98)

Layout: **app server** 10.0.1.98 (`D:\Citykart_Applications\Citykart_MOP_FINANCE_APP`) - **database** PostgreSQL on 10.0.0.205
(`MOPfinanceapp`, user `mopfinance_app`) - **files** NAS 10.0.0.25 (`\\10.0.0.25\CKAPPLICATIONS_DATA\CKMOPFINANCEAPP_DATA\`).
Everything the application needs lives inside its own folder. Nothing else on either server is touched or reconfigured.

## What is in the package (`mop_reco_<date>_<commit>.zip`)
`app\` (code, theme) - `scripts\` (run, import, checks) - `deploy\` (this folder) - `requirements.txt` (exact versions) -
`.streamlit\config.toml` - `VERSION.json` (commit, build time) - `MANIFEST.json` (checksum of every file).
**Not** in it: business data, databases, the warehouse cache, exports, logs, secrets, tests.

## First install (each step waits for an OK; nothing is done to production without it)
1. **Unpack** the zip into the application folder. Prove it is intact:
   `venv\Scripts\python.exe scripts\verify_manifest.py` -> "all files identical to the package".
2. **Install packages** (inside the application's own `venv` only): `powershell -File deploy\install.ps1`.
3. **Settings:** edit `deploy\production.env` (database host/port/name/user, NAS paths). No passwords in it.
4. **Passwords, once, typed by you at the server:** `powershell -File deploy\set_secrets.ps1`
   (stored encrypted for this server (Windows DPAPI, machine scope); the secrets folder is limited to Administrators).
5. **Masters into the production database, once:** copy `masters_snapshot.json` (sent separately - it is business data)
   to `snapshots\` and run (with the same environment `start_app.ps1` sets):
   `venv\Scripts\python.exe scripts\import_masters_snapshot.py --snapshot snapshots\masters_snapshot.json --by "<your name>"`
   It verifies every master by reading it back. After this no master workbook is ever read; masters are edited on the Masters page.
6. **Health check:** `powershell -File deploy\health_check.ps1` - app, database, masters, NAS write/read, free disk. Exit 0 = healthy.
7. **Start by itself at boot and restart if it stops:** `powershell -File deploy\install_task.ps1` (a Windows Scheduled Task; no extra software),
   then `Start-ScheduledTask -TaskName MOP-Reconciliation-App`.
8. **Open the port** (default 8501) on the server's firewall for the office network only; test from a PC and a phone.

## First reconciliation on production
Copy the source folders (`1. POS Sales`, `2. Settlement reports`, `3. CMS received`, `4. Petty cash ledger`, ...) into the application folder, then
use the **Upload** tab's Run section, or `venv\Scripts\python.exe scripts\run_range.py --from 2026-04-01 --to 2026-11-14 --by "<name>"`.
Then compare with the development result (`scripts` comparison: 0 differences expected - it matched exactly on the development PC, see CLAUDE.md 6.38).

## Updating to a new version
1. `deploy\stop_app.ps1`  2. keep the old folder renamed `..._previous`  3. unpack the new zip over the application folder (code only - data,
`warehouse\`, `logs\`, `secrets\`, `production.env` are not in the zip, so they are untouched)  4. `scripts\verify_manifest.py`  5. `deploy\install.ps1`
(only if `requirements.txt` changed)  6. `Start-ScheduledTask -TaskName MOP-Reconciliation-App`  7. `deploy\health_check.ps1`.
The database upgrades itself on start (adds columns/indexes, never drops).
**Rollback:** `deploy\stop_app.ps1`, put `..._previous` back, start. A database change that added a column does not need undoing for the old code to run.

## Daily operation
- Start / stop: `Start-ScheduledTask -TaskName MOP-Reconciliation-App` / `deploy\stop_app.ps1`.
- Logs: `logs\streamlit_<date>.log` (and `.err`); a run's own log is on the Upload page's run history.
- A reconciliation never runs from browsing; only from the Upload/Run action. Do not start one while another is running (the app blocks it).
- Keep the server free while a run is going (a full run needs about 8 GB of memory and 40-75 minutes).

## Backup and restore (decision pending)
The database lives on 10.0.0.205: either its administrator backs it up on their schedule (recommended), or the application is given a nightly
`pg_dump` to the NAS. Until one is chosen **there is no automatic backup**. Restoring = restore the database, start the application, run `health_check.ps1`.

## Troubleshooting
| Symptom | Check |
|---|---|
| App will not start, log says `MOP_REQUIRE_POSTGRES is set but MOP_DATABASE_URL is not` | `deploy\production.env` missing or wrong; run `health_check.ps1` |
| `password authentication failed` | re-run `set_secrets.ps1` as the same account; confirm the database user's password with the DBA |
| `could not connect to server` | firewall / `pg_hba.conf` on 10.0.0.205 must allow 10.0.1.98 for `MOPfinanceapp` |
| `NAS folder not reachable` | the share name, the `ckappuser` password (`set_secrets.ps1`), or the NAS being offline |
| A page shows an old behaviour after an update | the process was not restarted: `stop_app.ps1`, then start (Streamlit keeps modules in memory - CLAUDE.md 9a) |

## Not in this release (known, agreed to build next)
- **Login / access control.** There is none yet: anyone who can reach the URL can see and change data. Restrict by network until it is added.
- **NAS evidence archive for uploaded files** (write-once copy + checksum + "withdrawn" flag). The NAS is connected and tested by `health_check.ps1`;
  uploads are still kept in the application's own folders for now.
- **Data-lake daily refresh.** Files are supplied by folder or the Upload page until the lake details are known.
