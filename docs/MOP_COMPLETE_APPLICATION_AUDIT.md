# MOP Reconciliation - complete application audit (24 Nov 2026)

Independent audit, quality assurance and remediation of the MOP Reconciliation application, in nine phases. This is the full report; the management summary is `MOP_FINAL_SIGNOFF_REPORT.md`. Server names, addresses and credentials are not written in any audit document.

## 1. Scope and method

**Inspected:** 203 files (45 engine modules, 9 application modules, 7 pages, 64 scripts of which 12 are the audit's own tools, 78 test files), about 52,900 lines; the production database (read-only); the application server's capacity, release, scheduled task, logs and network exposure (read-only); six source systems' loaded data (POS, Paytm, Axis EDC, Bajaj, petty cash, CMS) and the ERP's finance views.

**Method:** independent re-calculation (whole paise or `Decimal`, plain loops or SQL) compared with the application; an oracle written from the rules for matching; hostile-input tests for every security finding; acceptance scenarios run in the real application on a copy of the full data; read-only measurement on production. A finding is recorded only with evidence, and a fix is accepted only with a regression test that fails on the old code where that could be shown.

**Rules kept:** a backup point was set (git tag `pre-audit-20261124`); no production data was changed by any test (tests wrote only to a throw-away copy; read-only queries against production, plus one deliberate change at your request, the administrator password reset); no history or audit trail was deleted; no financial rule or column meaning was changed; every fix is in the development copy and **none is deployed**.

## 2. Result in numbers

| | |
|---|---|
| Defects found and fixed in the code | **17** (2 at P1, 7 at P2, 8 at P3) |
| P0 (stops the business) | **0** |
| Closed on the live system | **0** (a defect is closed only when its fix is deployed and verified live) |
| Open findings that need a person or another team | 15 observations and decisions, plus 2 data matters (opening differences, duplicate-reference lines) |
| Automated tests | **1,968 passed, 0 failed** in the final full run; six package tests added after it started pass separately (1,974 in all) |
| New tests written by the audit | 674 in files the audit created (matching oracle, Float, CMS blocks, sheet and cash book, atomicity, concurrency, injection, upload paths, contrast, matrix cascade, package, upload validation), plus a few added to existing files |
| Acceptance scenarios (UAT) | 83: 82 pass, 0 fail, 1 not run |
| Module status | 37 verified, 29 working with limitations, 2 unable to verify, the rest (mostly operating scripts) not examined |

## 3. What the audit found, by phase

**Phase 1, discovery.** Inventory of every file, static analysis (0 undefined names), risk classification. Nine candidates were opened: eight are closed (fixed, disproved or logged) and one, code hygiene (38 unused imports), was left as cosmetic by choice.

**Phase 2, financial.** Independent re-performance of R1, Float, CMS blocks, the daily sheet and the cash book (details: `MOP_FINANCIAL_VALIDATION_REPORT.md`). **D-001 (P1):** a collection and its later adjustment in two monthly CMS files collided on a key that left out the row type, losing one of them: 5 store-days, net Rs 7.2 lakh of cash received. D-002 (money noise in stored columns) and D-003 (clear error for un-deduplicated Paytm data).

**Phase 3, code and database.** All 25 silent error handlers read: six hid real failures and now log or fail loudly (D-004, D-005). The suspected SQL-injection route was disproved by a hostile-input test. Two operators racing for one transaction: exactly one wins. The production database passed every structural check (unique keys, no orphans, valid dates and statuses, all formula identities on 45,580 store-days); the only finding was D-002's float noise on 86 rows.

**Phase 4, interface.** Seven pages and the sign-in at 375, 768, 1366 and 1920 px: no overflow, no error. Five fixes: **D-006**, the Summary "Data notes" and Float sections re-read every CMS file and rewrote the warehouse on each view (seen live in the server log), D-007 (Float on the slow path), D-008 (a negative closing balance shown as positive), D-009 (text contrast), D-010 (touch targets).

**Phase 5, security and performance.** **S-5 (P1):** an uploaded file's name decided where it was saved (a test wrote a file into the system folder). Also S-3 (markup injected through a file name), S-4 (a typed remark becoming an Excel formula), S-1 (username timing), S-6 (zip size). Secrets: none in the repository or its history. Performance measured on production (section 7 of the readiness checklist).

**Phase 6, fixing.** All of the above fixed with tests; OB-2 (five small cash-book differences) traced to the ERP's two views disagreeing, not to MOP; bulk operations proven all-or-nothing under forced failure.

**Phase 7, acceptance testing.** 83 scenarios on a copy of the full run. **D-011 (P2)** found only here: after a bulk accept, the Summary matrix still counted matched bills as receivable while the store sheet did not. Also D-012 (dates on the Masters page).

**Phase 8, production readiness.** The application on the server is the 23 Nov build and was found **not running**; deployment, backup and recovery, monitoring and rollback reviewed (`MOP_PRODUCTION_READINESS_CHECKLIST.md`). No schema change since the deployed build, so a rollback needs no data change.

**Phase 9, sign-off.** This report; independent check of the gateways' settlement arithmetic; the module, defect and UAT workbooks.

## 4. The nine non-negotiable rules of the brief

| # | Rule | Assessment | Evidence |
|---|---|---|---|
| 1 | Nil tolerance: any difference is an exception | **Holds.** Amounts are compared exactly; a one-paisa difference is not a match | R1 oracle, UAT E06 |
| 2 | No locking; every date re-runnable | **Holds.** Runs are always whole-range; master data is versioned by date | Tracker, CLAUDE.md 6.12 |
| 3 | No write-offs | **Holds.** A difference stays an open item; a refund of a different amount is held, not netted | refunds tests, UAT E11 |
| 4 | Nothing vanishes silently | **Holds**, with one fix: six hidden failures now log (D-004, D-005). Closed items stay listed; reversal keeps the record | UAT E08, E15 |
| 5 | Nothing is guessed | **Holds with one documented default.** Unmapped identifiers are named. An open bill counts as UPI until matched (the operator's own rule, shown on the page); D-011's fix applies the same default to a matched bill whose mode was never recorded, rather than leaving it receivable | UAT Z04, S19 |
| 6 | Business date = POS bill date, never filled from a neighbour | **Holds** | sheet and float re-performance |
| 7 | Raw files never modified | **Holds**; uploads add files, delete moves them to an archive | upload tests |
| 8 | No hardcoded dates | **One breach:** control (g) of the daily sheet hard-codes 20 July 2026 and its figure (O-1); needs your approval to change | register O-1 |
| 9 | No fixed row ranges | **Holds**; the Excel row-limit cut-off of the November file is detected and announced | pos_completeness |

## 5. Business rules: coverage

| Area (CLAUDE.md section) | Verified how |
|---|---|
| 6.1 MOP classes | Every MOP found in the data is classified; an unknown one is reported and excluded, not guessed |
| 6.2 to 6.3 References and matching tiers | Oracle (316) and live |
| 6.4 to 6.5 Settlement routing, due dates | **Not implemented** (R2); calendar rules for Float verified |
| 6.6 R3a cash book | Re-performed (section 4 of the financial report) |
| 6.7 R3b | Not implemented as specified; CMS Reco verified instead |
| 6.8 to 6.9 Duplicate bills, exception keys | Key determinism and lifecycle by test; remarks keep history |
| 6.9b Manual matching | Concurrency, atomicity, validation cases a/b/c, reversal, shared payment, bulk accept, UAT |
| 6.9c to 6.9d Summary matrix and daily sheet | Identities on 45,580 store-days; matrix equals sheet (after D-011) |
| 6.10 to 6.12 Dates, pre-flight, master versioning | Tests; pre-flight banners seen in the running app |
| 6.13 Sale universe | Rows without a bill number excluded; the sheet test |
| 6.14 Two-phase execution | A page must not read raw sources: D-006 fixed, with a test that fails if any page does it again |
| 6.15 Float | Verified |
| 6.16 to 6.27 Performance, task, tabs, pairing, uploads | UAT and performance measurement |
| 6.28 Parked lines (a gateway file not yet arrived) | **Not exercised**: Axis data is present, so nothing is parked |
| 6.29 to 6.42 Matrix layout, store codes, shared payment, refunds, masters in the database, deployment, sign-in, ERP link | Tests and UAT; ERP link read-only in three layers; sign-in proven |

## 6. What this audit did not examine

* R2 as a reconciliation and anything depending on bank credits (not built; no data).
* Operating scripts (the 52 files in `scripts/` other than the audit's own tools), six small engine modules (CMS reco, exception remarks, parking, config, package marker, an unused inline-match helper) and the Run section's screen.
* `queries.py` (1,941 lines), `ingest.py` and `store_tasks.py` and `table_upload.py` were exercised by tests and acceptance cases but not read line by line.
* Production: nothing was deployed, run or changed there (apart from the password reset you asked for). The production database was read, never written.
* People: every acceptance case was run by the audit team; no finance user has tested the application.
* Real file picking, screen readers, keyboard-only use, more than 4 simultaneous users, HTTPS (there is none).

## 7. Documents

`MOP_FINAL_SIGNOFF_REPORT.md` (management), `MOP_FINANCIAL_VALIDATION_REPORT.md`, `MOP_SECURITY_AUDIT.md`, `MOP_PERFORMANCE_REPORT.md`, `MOP_PRODUCTION_READINESS_CHECKLIST.md`, `MOP_CHANGELOG.md`, `MOP_MODULE_WISE_AUDIT.xlsx`, `MOP_DEFECT_REGISTER.xlsx` (generated from `audit/MOP_DEFECT_REGISTER.md`), `MOP_UAT_TEST_CASES.xlsx`, and the running record `audit/MOP_AUDIT_TRACKER.md`. Spreadsheets are regenerated by `scripts/audit_build_*.py` (git does not track `.xlsx` files).
