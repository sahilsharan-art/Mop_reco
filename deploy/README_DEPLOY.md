# MOP Reconciliation - production runbook (server 10.0.1.98)

Layout: **app server** 10.0.1.98 (`D:\Citykart_Applications\Citykart_MOP_FINANCE_APP`) - **database** PostgreSQL on 10.0.0.205
(`MOPfinanceapp`, user `mopfinance_app`) - **files** NAS 10.0.0.25 (`\\10.0.0.25\CKAPPLICATIONS_DATA\CKMOPFINANCEAPP_DATA\`).
Everything the application needs lives inside its own folder. Nothing else on either server is touched or reconfigured.

## How the application reaches the NAS (the platform convention)
The application runs as the local Windows account **`.\ckappuser`** - the same account CkDesk's services use on this server. The NAS
recognises that account by name, so **nothing is mapped and no NAS password is stored anywhere**. `start_app.ps1` proves the access at every
start (writes, reads back and deletes a probe file in `_healthcheck`) and records the outcome in `logs\nas_probe.json` and the log.
`grant_service_account.ps1` gives that account only what it needs: read + run on the application folder, change on logs / warehouse / db /
exports / uploads_archive / the source-data folders, and **read-only** on the encrypted database password.

## What is in the package (`mop_reco_<date>_<commit>.zip`)
`app\` (code, theme) - `scripts\` (run, import, checks) - `deploy\` (this folder) - `requirements.txt` (exact versions) -
`.streamlit\config.toml` - `VERSION.json` (commit, build time) - `MANIFEST.json` (checksum of every file).
**Not** in it: business data, databases, the warehouse cache, exports, logs, secrets, tests.

## First install (each step waits for an OK; nothing is done to production without it)
1. **Unpack** the zip into the application folder. Prove it is intact:
   `venv\Scripts\python.exe scripts\verify_manifest.py` -> "all files identical to the package".
2. **Install packages** (inside the application's own `venv` only): `powershell -File deploy\install.ps1`.
3. **Settings:** `deploy\production.env` (database host/port/name/user, NAS folder, the run-as account). No passwords in it.
4. **Database password, once:** typed at the server with `powershell -File deploy\set_secrets.ps1`, or supplied over a remote session with
   `deploy\store_secrets_from_stdin.ps1` (one line on standard input). Stored encrypted for this server (Windows DPAPI, machine scope); the
   secrets folder is readable only by Administrators and the run-as account.
5. **Masters into the production database, once:** `powershell -File deploy\run_in_app_env.ps1 scripts\import_masters_snapshot.py --snapshot snapshots\masters_snapshot.json --by "<name>"`
   (the snapshot file is business data and is sent separately). It verifies every master by reading it back. After this no master workbook is ever
   read; masters are edited on the Masters page.
6. **Start by itself at boot and restart if it stops, as `ckappuser`:** as Administrator, `powershell -File deploy\install_task.ps1`. It runs
   `grant_service_account.ps1`, then asks for **`ckappuser`'s Windows password** (used only to register the task; never stored by these scripts).
   Then `Start-ScheduledTask -TaskName MOP-Reconciliation-App`.
7. **Health check:** `powershell -File deploy\health_check.ps1` - app, startup task and its account, database + masters, NAS (as the app found it at start),
   free disk. Exit 0 = healthy.
8. **Open the port** (default 8501) on the server's firewall for the office network only; test from a PC and a phone.

## First reconciliation on production
Copy the source folders (`1. POS Sales`, `2. Settlement reports`, `3. CMS received`, `4. Petty cash ledger`, ...) into the application folder (the
empty folders already exist with the right permissions), then use the **Upload** tab's Run section, or
`powershell -File deploy\run_in_app_env.ps1 scripts\run_range.py --from 2026-04-01 --to 2026-11-14 --by "<name>"`.
Then compare with the development result (0 differences expected - it matched exactly on the development PC, see CLAUDE.md 6.38).

## Updating to a new version
1. `deploy\stop_app.ps1`  2. keep the old folder renamed `..._previous`  3. unpack the new zip over the application folder (code only - data,
`warehouse\`, `logs\`, `secrets\`, `production.env` are not in the zip, so they are untouched)  4. `scripts\verify_manifest.py`  5. `deploy\install.ps1`
(only if `requirements.txt` changed)  6. `Start-ScheduledTask -TaskName MOP-Reconciliation-App`  7. `deploy\health_check.ps1`.
The database upgrades itself on start (adds columns/indexes, never drops).
**Rollback:** `deploy\stop_app.ps1`, put `..._previous` back, start. A database change that added a column does not need undoing for the old code to run.

## Daily operation
- Start / stop: `Start-ScheduledTask -TaskName MOP-Reconciliation-App` / `deploy\stop_app.ps1`.
- Logs: `logs\streamlit_<date>.log` (start-up, NAS probe), `.out` and `.err`; a run's own log is on the Upload page's run history.
- A reconciliation never runs from browsing; only from the Upload/Run action. Do not start one while another is running (the app blocks it).
- Keep the server free while a run is going (a full run needs about 8 GB of memory and 40-75 minutes).

## Backup and restore (decision pending)
The database lives on 10.0.0.205: either its administrator backs it up on their schedule (recommended), or the application is given a nightly
`pg_dump` to the NAS. Until one is chosen **there is no automatic backup**. Restoring = restore the database, start the application, run `health_check.ps1`.

## Troubleshooting
| Symptom | Check |
|---|---|
| App will not start, log says `MOP_REQUIRE_POSTGRES is set but MOP_DATABASE_URL is not` | `deploy\production.env` missing or wrong; run `health_check.ps1` |
| `password authentication failed` | re-run `set_secrets.ps1`; confirm the database user's password with the DBA |
| `could not connect to server` | firewall / `pg_hba.conf` on 10.0.0.205 must allow 10.0.1.98 for `MOPfinanceapp` |
| Health check: `NAS ... FAILED` / log says `folder not reachable by this account` | the app is not running as `.\ckappuser` (check `task` line), or that account lost its NAS access (CkDesk would fail too) |
| Task shows result `0x8007052E` or the app never starts | wrong Windows password for `ckappuser` was given to `install_task.ps1`: run it again. Do NOT retry many times: 10 failures lock the account for 10 minutes and CkDesk's services use it |
| A page shows an old behaviour after an update | the process was not restarted: `stop_app.ps1`, then start (Streamlit keeps modules in memory - CLAUDE.md 9a) |

## Not in this release (known, agreed to build next)
- **Login / access control.** There is none yet: anyone who can reach the URL can see and change data. Restrict by network until it is added.
- **NAS evidence archive for uploaded files** (write-once copy + checksum + "withdrawn" flag). NAS access is built and probed at every start;
  uploads are still kept in the application's own folders for now.
- **Data-lake daily refresh.** Files are supplied by folder or the Upload page until the lake details are known.

## The V2 screens (React) - installed beside the dashboard (24-25 Nov 2026)
Built by another developer (`frontend/`, `app/api_v2.py`, `app/api_v2_workflows.py`); they call the unchanged engines in `app\mop_reco`. The dashboard on
APP_PORT stays running and is the rollback. Three private pieces, one public port:

| Piece | Where | Started by |
|---|---|---|
| Python API (`uvicorn api_v2:app`, ONE worker) | 127.0.0.1:8092 | `start_v2.ps1` |
| React host (`bun serve.ts`) | 127.0.0.1:8093 | `start_v2.ps1` |
| MOP's own proxy (a copy of Caddy, `Caddyfile.mop_v2`, admin API off) | **V2_PORT (8091)** - the address users open | `start_v2.ps1` |

1. **Install (Administrator, after unpacking the package; own folder only):** `powershell -File deploy\install_v2.ps1` - API packages into the venv, Bun 1.4.4 into
   `tools\bun` (checksum verified), a copy of Caddy into `tools\caddy`, then `bun install --frozen-lockfile` and `bun run build`. Safe to repeat.
2. **Settings (`deploy\production.env`):** `V2_ENABLE=1`, `V2_PORT=8091`, `V2_ORIGIN=<exactly what users type>` e.g. `http://<server>:8091` - no trailing slash. The API
   refuses any request whose Origin differs, so an IP address and a host name are different origins: choose one. Use `https://` once the infrastructure team puts a
   certificate in front (the session cookie then becomes Secure by itself).
3. **Start:** restart the scheduled task (`start_app.ps1` starts V2 when V2_ENABLE=1; a V2 failure is logged and never stops the dashboard), or from a remote session
   `powershell -File deploy\start_v2.ps1 -Detached` (a plain start from ssh dies with the session). Stop: `stop_v2.ps1`, or `stop_app.ps1` stops both.
4. **Check:** `health_check.ps1` adds `v2 api` and `v2 screens`. Sign in with an existing account (the same users and passwords as the dashboard).
5. **Rollback:** `stop_v2.ps1`, set `V2_ENABLE=0`. Nothing else changes; V2 has no schema of its own.
Logs: `logs2_api_<date>.*`, `v2_web_<date>.*`, `v2_proxy_<date>.*`, `v2_<date>.log`. Firewall: open V2_PORT (not 8092 / 8093) for the office network only.
Limits: one API worker only (a reconciliation started from the screens uses one in-process worker, protected by the engine's own running-run check); the developer's
own notes say production acceptance is open and some older drill-downs are not recreated - keep the dashboard until a finance user has accepted V2.
