# MOP Reconciliation - production readiness checklist (24 Nov 2026)

Prepared by the audit. Nothing here is deployed yet. Addresses, passwords and account names of the servers are deliberately not written in this document.

## 1. The verdict, in one paragraph

The application's **logic is sound and independently verified** (every reconciliation re-calculated against a separate calculation, 83 acceptance scenarios run on a copy of the full April-November data, 1,900+ automated tests). Seventeen defects were found and fixed in the code, including two that matter for money (a missing CMS adjustment in 5 store-days worth Rs 7.2 lakh, and a Summary table that ignored bulk-accepted matches) and one that was a real security hole (an uploaded file's name could write outside its folder). **All of those fixes are in the development copy only. The application on the server is still the build from 23 Nov and is not running.** It is ready to deploy once the gates in section 3 are passed, but it is **not yet ready to be called production-ready as a whole**: business-user sign-off, backup and recovery, HTTPS and the firewall are open and need other people (section 2).

## 2. The ten open risks from the start of the audit, and what they need

| # | Risk | Where it stands now | Needs | Who |
|---|---|---|---|---|
| 1 | Passwords and sessions cross the network unencrypted (HTTP) | Unchanged. The server already runs a web proxy that could serve MOP over HTTPS; a certificate is needed | **Infrastructure support** | IT |
| 2 | Only two roles (admin, user): anyone signed in can upload, run, edit masters and match | Unchanged | **Formal scope decision** | Finance + IT |
| 3 | No database backup or recovery procedure | A restore rehearsal on the development copy is in section 6; the production database has no backup arrangement known to the audit, and the application server has no backup tools | **Infrastructure support** | DBA |
| 4 | Uploaded source files are not kept on the file server as write-once evidence | Unchanged | **Scope decision** (retention, write-once) | Finance + IT |
| 5 | R2 bank-statement leg (transaction to settlement to bank credit) | Unchanged. R2 must not be shown as complete: the bank postings in the ERP carry no reference number | **External data source** | Finance / bank |
| 6 | No holiday list | Unchanged | **External data source** | Finance / HR |
| 7 | CMS credit cycle (T+0 or T+1) per store not supplied | Unchanged | **Business approval** and data | Finance |
| 8 | Opening-balance differences against the ERP | Narrowed: 21 stores differ on 1 April by Rs 40,460.75 in total, Rs 40,000 of it one store (GPB). The five small daily differences were traced to the ERP's own two views disagreeing, not to MOP | **Business approval** (which opening is right) | Finance |
| 9 | The shared ERP login can read more than MOP needs | Unchanged. MOP is held to the one agreed schema by three layers in its own code | **Infrastructure support** | DBA |
| 10 | One foreign-currency payment line | Unchanged | **Business approval** | Finance |

New items from the audit that need a person (full list: `docs/audit/MOP_DEFECT_REGISTER.md`): lock-out policy and a second administrator (S-2); the firewall on the application server is off (S-7); control (g) of the daily sheet hard-codes 20 July (O-1); the 82 duplicate-reference gateway lines (F-001); the 21 opening differences (OB-1).

## 3. Go / no-go gates for deploying the audited build

A gate is passed only when the evidence in the right-hand column exists. **None of the production gates has been run.**

| Gate | Evidence required | Status |
|---|---|---|
| G1 Full automated test suite on the build to be shipped | Counted run, zero failures | In progress at time of writing; result recorded in the tracker |
| G2 Build is reproducible and complete | Package built from committed code, checksum recorded, file manifest verified after unpacking (section 5) | Not yet built |
| G3 The packaged build starts | Import smoke test of the unpacked package | Not yet run |
| G4 A backup of the production database exists and a restore has been proven | Backup file + restore rehearsal | Rehearsed on the development copy only (section 6); production backup not made |
| G5 The previous package is still on the server for rollback | The 23 Nov package present in the server's package folder (it was, 8 MB) | Present at last check |
| G6 Nobody is mid-run | No run in progress; app stopped | The app is stopped |
| G7 Business owner agrees to the change | Written OK | **Needed** |
| G8 A named person will check the first screens after the restart | Section 5 step 9 | **Needed** |

## 4. What changes for users when this is deployed

* The cash-received column (J) changes for **5 store-days** after the next run (net Rs 7,22,748 more received; cash receivable lower by the same).
* The Summary matrix now agrees with the store-wise sheet after manual and bulk matches.
* The Cash Position card shows a minus sign for a negative closing balance.
* Pages open faster; two sections (Summary "Data notes" CMS check, Float "Credits exceeding deposits") now load when you press a button.
* An uploaded file with an unsafe name is stored safely; exports no longer turn a typed remark starting with `=` into a formula.
* No database change: nothing to migrate, nothing to undo.

## 5. Deployment runbook (for when the gates are passed and the owner says go)

Time needed: about 30 minutes plus the run. The application is stopped throughout; nobody can use it.

1. **Confirm gates G1 to G8.** Stop if any is red.
2. **Back up** the production database (section 6) and note the file name and size.
3. **Build the package** from the committed code: `python scripts/build_package.py` (creates a zip, its checksum, a version stamp and a file manifest). Record the commit and checksum in the tracker.
4. **Copy** the zip and checksum to the server's package folder (the old package stays there).
5. **Install**: unpack over the application folder, then run `scripts/verify_manifest.py` on the server. Every file must match; one mismatch means stop and roll back. Passwords and secrets are not touched (they are stored encrypted on the server and are not in the package).
6. **Smoke test** the database link and masters read-only (`scripts/db_check.py`).
7. **Start** the application (the scheduled task) and check the health script: the application answers, the database and masters read, free disk is sufficient.
8. **Sign in** with the administrator account and open Summary, Float, Cash position, Exceptions, Store Task, Upload, Masters. Expect: no error panel; the same red banner about the cut-off November POS file as today; Summary matrix Total sale Rs 999.74 Cr and the cash card figures as before the run.
9. **Run** the reconciliation for 01/04 to 14/11 as one run (a run is always the whole range). Watch the heartbeat. Expect about 40 minutes. Keep the machine otherwise idle.
10. **Compare** the new run with run #2: `scripts/compare_two_runs.py`. Expected differences, and only these: the 5 store-days of cash received (D-001); stored money without float noise (D-002). Anything else is investigated before anyone relies on the new run.
11. **Switch POS and petty cash to the ERP** only as a separate, later decision (it fixes the cut-off November file); it is a settings change and a restart, with its own comparison.
12. **Record** the result in the tracker and tell users.

## 6. Backup and recovery

* **Today:** no backup arrangement for the application database is known to the audit. The application server has no database backup tools installed. The database server is shared with other applications.
* **What is needed:** a scheduled backup of the application database to the file server (or the DBA's own backup), kept for a period Finance chooses, and a restore test at least once. The database holds the exception register, remarks, manual matches, refunds, store tasks, users and audit trail: **these are the records that cannot be recreated from the source files**, so losing them loses people's work. The reconciliation results themselves can be rebuilt by a run.
* **Rehearsal (24 Nov 2026, development PostgreSQL, 5.3 GB copy with 9.9 million bill lines, twice the production size):** compressed dump **135 seconds** (0.46 GB file), restore with 4 parallel jobs **351 seconds**, **about 8 minutes end to end**. Afterwards every one of the 25 tables had the same row count, and a checksum over every row of the tables that hold people's work (manual matches, exceptions, remarks, store tasks, users, master rows, store-day figures) was **identical**. Production should take roughly half as long. Script: `scripts/audit_backup_rehearsal.py`. This proves the method and the time; it is **not** a production backup, and no production backup exists yet.

## 7. Rollback

Because there is no database change, rolling back is: stop the application, unpack the previous package (23 Nov build, commit `108bbad`) over the application folder, verify the manifest, start. No data is lost. A run made by the new build stays in the database and is simply the latest run; run #2 is still there.

## 8. Monitoring and support

| Need | Today | Gap |
|---|---|---|
| Is the application up? | Health script, run by hand | **No alert.** The application was found stopped at the audit and nobody was told. Recommend a scheduled check that messages an owner |
| Did a run fail or stall? | The Run page shows a heartbeat; failed runs are recorded | Nothing pushes it to a person |
| Errors | `logs/` on the server | Not watched; one web-layer error was in the log at the last check |
| Disk | 376 GB free on the data drive, the warehouse is 1.2 GB | None now; check yearly |
| Who did what | Sign-in trail, match and remark history, upload history | Good |
| Support | The administrator account is a single person | Add a second administrator |

## 9. Observations about the server found during this phase

* Windows Server 2022, 27 GB memory (23 GB free), data drive 376 GB free, time synchronised.
* The scheduled task starts the application at boot and runs as a service account; the last start was 07:19 today. The logs stop at about 12:00 and the application was not running at the check; the log does not say why (the last lines are a web-layer error that normally means a browser disconnected). Cause unknown.
* The deployed build is the 23 Nov one (before the ERP work and before every audit fix).
* The application was re-reading the CMS files and rewriting its warehouse files on page views (defect D-006), visible in the server log at 11:57 today.
* A temporary password file for the administrator is still in the server's `secrets` folder and should be deleted after sign-in.

## 10. Sign-off (to be completed by people, not by the audit)

| Role | Name | Decision | Date |
|---|---|---|---|
| Finance owner (acceptance of the UAT and of the changed figures) | | | |
| IT owner of the application server | | | |
| Database administrator (backup, ERP login) | | | |
| Information security | | | |
