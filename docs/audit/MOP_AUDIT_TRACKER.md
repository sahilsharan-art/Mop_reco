# MOP Reconciliation - audit tracker (persistent; update after every phase)

Engagement: independent audit, QA, remediation and production-readiness of the MOP Reconciliation application, per the master instruction of 24 Nov 2026.
This file is the single record of **where the audit stands**. A session that ends does not end the audit: the next session starts here.
Rules in force (instruction section 16): backup/working copy first; no production data changed by testing; no deletion of history or audit trails; no change of financial business rules or
column meanings without approval; no secrets or internal addresses in any deliverable; no claim of "verified" without a test; no hidden failures.

## 1. Phase status

| Phase | Scope | Status | Notes |
|---|---|---|---|
| 1 | Discovery: inventory, architecture, baseline | **Complete (24 Nov 2026)** | inventory, static analysis, test discovery, risk classification, first candidate findings |
| 2 | Functional and financial audit (R1, R2, R3a, R3b, float, sheet A-N) | **In progress (24 Nov 2026)** | R1, Float, CMS blocks, sheet A-N and the R3a cash book re-performed independently; 3 defects found and fixed locally (section 9); R2 / R3b and boundary runs still to do |
| 3 | Code and database audit | **Substantially complete (24 Nov 2026)** | 25 silent handlers read and classified; schema, index and constraint review done; live database integrity audit passed except D-002 noise; concurrency proved; C-002/C-004/C-005/C-007/C-008 closed; see section 10 |
| 4 | UI/UX audit (every page, 375/768/1366/1920) | **Complete for the pages and widths (24 Nov 2026)** | seven pages and the sign-in page at four widths; five defects found and fixed locally (D-006 to D-010); see section 11 |
| 5 | Security and performance audit | **Substantially complete (24 Nov 2026)** | six security defects found and fixed locally (S-1, S-3 to S-6; S-5 is P1); six infrastructure / decision items recorded (S-2, S-7 to S-12); performance measured on production; see section 12 |
| 6 | Bug fixing (P0, P1, P2, P3) | Started | D-001..D-003 fixed locally and committed (01e1e8b); NOT yet deployed to the application server; see `MOP_DEFECT_REGISTER.md` |
| 7 | UAT | Not started | |
| 8 | Production readiness | Not started | the ten open risks are classified in section 6 |
| 9 | Final sign-off | Not started | classification today would be **D - Audit incomplete** |

## 2. Baseline (what was true when the audit started)

* **Code baseline:** git tag `pre-audit-20261124` (commit `3d36eb7`, the last committed state). Uncommitted at the time, from the preceding ERP work: `erp_ingest.py`, the `ingest.py` / `runs.py` dispatch, config, deploy script
  wiring, `compare_two_runs.py`, documentation. Rolling back = check out the tag.
* **Deployed on the application server:** commit `108bbad` (verified file by file at deployment). The ERP source mode is NOT deployed and NOT switched on; both sources read the files.
* **Tests - actual, not stated:** **1,299 tests discovered** (the guide says "about 1,290"). Last complete run before the ERP-ingest change: 1,287 passed, 0 failed, 20 minutes. A full run after the change was in progress when Phase 1 closed.
* **Size:** 171 Python files (45 engine modules, 9 app modules, 7 pages, 52 scripts, 58 test files), 49,176 lines. Inventory: `docs/audit/MOP_MODULE_INVENTORY.md` (regenerate with `scripts/audit_inventory.py`).
* **Static analysis** (tools installed in an isolated temporary folder; results kept in `docs/audit/static_pyflakes.txt`): pyflakes 90 warnings, **0 undefined names**; ruff blind-except 9; bandit: 2 "weak hash" and 14 string-built SQL flags (classified below).
* **Environments:** development = SQLite default and local PostgreSQL for tests; production = PostgreSQL mandatory (`MOP_REQUIRE_POSTGRES=1`, the app refuses SQLite). Tests run against throw-away databases.

## 3. Candidate findings (UNCONFIRMED until read, reproduced and classified)

IDs here are candidates. A candidate becomes a defect in the register only after Phase 2/3 evidence.

| ID | Candidate | Where | Evidence so far | First look | Next |
|---|---|---|---|---|---|
| C-001 | Silent broad exception handlers (no log, no message, no re-raise): 25 | listed in `MOP_MODULE_INVENTORY.md` | AST scan | money-adjacent first: `manual_match.py:229,266`, `store_tasks.py:237`, `run_coverage.py:93,110,361`, `refunds.py:143`, `runs.py:558`, `masters_db.py:157` | read each; log or raise where a failure would be hidden |
| C-002 | `refunds.py:143` turns a failed store-master read into an empty set, so a refund is rejected as "store not in master" instead of showing the real error | refunds.py:137-143 | read | misleading error, no financial misstatement (it fails closed) | P3: log it |
| C-003 | Money is `float64` throughout the engines (423 `float(` conversions; pandas sums) with a paise tolerance of Rs 0.005 in comparisons | engines, `queries` | static count; brief section 8 permits "rounded float" | fail-open risk is below Rs 0.005 per comparison | Phase 2: recompute totals independently with `Decimal` and show the maximum divergence |
| C-004 | bandit SHA-1 "weak hash" at `ingest.py:420` and `db.py:988` | | read: cache file name and a test-schema name, not security | false positive for security | P3: `usedforsecurity=False` |
| C-005 | bandit B608 string-built SQL: `upload_prefetch.py:78,106` | | read: placeholders are bound parameters built by `_in()` | likely false positive | Phase 5: confirm `_in()` can emit only `:name` tokens; add a test with a hostile reference |
| C-006 | pyflakes: 68 unused imports, 6 unused locals, 3 f-strings without placeholders, 8 redefinitions | mostly `Summary.py`, `dashboard_common.py`, tests | pyflakes | hygiene only | P3 clean-up with the regression suite |
| C-007 | Hardcoded ISO-date literals in app code: 10 tokens | `float_tracker.py:331`, `manual_match.py:1205`, `refunds.py:52`, `store_sheet.py:673`, others docstrings | tokenize | look like upload-template example rows and docstrings, not logic | Phase 3: confirm each is only an example |
| C-008 | `masters.store_aliases()` falls back silently to "no aliases" if config cannot be read | masters.py ~697 | read | a mis-read config would be caught earlier by `load_config()`, low risk | Phase 3: log a warning |
| C-009 | Till-session, bank-posting and settlement views have no tests of their own (read directly in analysis only) | gold_mop views | by absence | not used by the app yet | none until used |

## 4. Verified so far (evidence, not assertion)

* ERP read-only layer (`erp.py`, `erp_source.py`, `erp_shadow.py`): unit tests and real-data checks - see `module_status.json`.
* POS and petty cash from the ERP against the Excel-derived warehouse, 1 Apr - 31 Oct 2026: identical (214 dates; 150,947 vouchers). Recorded in CLAUDE.md 6.41 / 6.42. **Not yet independently re-performed by this audit** - the audit's own recomputation is a Phase 2 task.
* The ERP's store cash book confirms the computed cash book: cash sale, deposits, other petty cash 100%; opening 88% (open item OB-1 below).

## 5. Security items already known (Phase 5 will test them)

1. The application is served over plain HTTP. Passwords and session cookies cross the internal network unencrypted.
2. The host firewall on the application server is OFF; the sign-in is the main protection.
3. The shared ERP login can technically read unrelated tables, including HR and service-desk data; MOP is restricted by its own code and by agreement, not by the database. A narrower login is requested.
4. Only two roles exist (admin, user): any signed-in person can upload, run, edit masters and match.
5. Session cookie is script-set and cannot be HttpOnly (documented).

## 6. Open production risks - classification (instruction section 14; none is marked complete)

| # | Risk | Classification | What would close it |
|---|---|---|---|
| 1 | HTTPS | **Requires infrastructure support** | a certificate and a TLS terminator (the existing reverse proxy, or a certificate on the app server); app-side change is small once supplied |
| 2 | Finer role permissions | **Requires a formal scope decision** | who may upload / run / edit masters / match / close; then buildable and testable |
| 3 | Database backup and recovery | **Requires infrastructure support** (and a decision on retention) | a scheduled dump to the file server or the DBA's backup, plus a restore test; no client tools are installed on the application server today |
| 4 | File-server evidence archive | **Requires a formal scope decision** | retention and write-once rules; the file server is connected and tested |
| 5 | R2 bank-statement leg | **Requires an external data source** | a bank statement file or feed with UTR; the ERP bank postings carry no UTR. Until then R2 must NOT be shown as complete |
| 6 | Holiday list | **Requires an external data source** | a list from Finance / HR; loads once |
| 7 | CMS credit cycle per store | **Requires business approval** (and data) | T+0 / T+1 per store |
| 8 | Opening-balance offsets (OB-1) | **Requires business approval** | Finance rules which opening is right (the import or the ERP's own daily opening) for the ~12% of stores with a constant offset; the investigation itself can start now |
| 9 | ERP login restricted to the MOP schema | **Requires infrastructure support** | the DBA creates the narrower login; the app only needs its host/user/password settings changed |
| 10 | Foreign-currency MOP (one foreign-note payment line) | **Requires business approval** | a ruling; the ERP types it as cash |

## 7. In-flight operations (not part of the audit, recorded so nothing is lost)

* ERP-source run: the code is built and unit-tested, a full-suite re-run was in progress, deployment and the first ERP-sourced run were approved by the operator and are pending that result. They are production operations, kept apart from audit testing.

## 8. Phase 2 plan (next)

1. Build an **independent expected-result harness**: synthetic POS / gateway / PTC / CMS data with hand-computed answers in `Decimal`, run through R1, R3a, CMS blocks, float and the A-N sheet; compare to the engines cell by cell; report maximum divergence.
2. R1 boundary tests: duplicate references, one transaction two bills (and the approved shared-payment rule), malformed JSON, missing gateway file (parking), date differences, refund / chargeback linkage, EDC OTHER ambiguity, manual-match overlay never altering engine rows, reversal.
3. Re-perform on the live data (read-only): daily-sheet column identities (C=D+E, G=D-F, K=I-J, M=H+E-I+L, N=H-I), store totals = date totals = grand total, matrix = sheet, exports = screen.
4. R3a against the ERP store cash book: classify the opening-balance offsets per store (constant offset from a start date vs a mid-period break).
5. Record every confirmed defect in the register with root cause and a regression test before any fix.

## 9. Phase 2 results so far (24 Nov 2026)

Everything below was run on the development PC against synthetic data, except the items marked LIVE, which were read-only checks against run #2 on the application server (nothing was written).

| Area | Method | Result |
|---|---|---|
| R1 matching | Independent oracle written from the spec (plain loops, Decimal) vs `run_r1`: 16 hand-asserted scenarios + 300 random collision scenarios (`tests/test_audit_r1_oracle.py`) | **Agrees on all 316.** Covers exact and one-paisa amounts, FAILED/REFUND rows, case-insensitive status, one transaction one bill, gateway order, 7-day-late match (recorded), EDC OTHER with 1/2/0 candidates, refund linkage, every terminal-reply shape |
| R1 LIVE | Independent re-performance on run #2 (`scripts/audit_live_r1_check.py`) | Every check holds |
| Float | Independent recomputation (Decimal, own streak logic, calendar worked arithmetically): 81 tests incl. threshold boundary (10,000.00 not high, 10,000.01 high), negative float kept, per-store override, streak reset, no leak between stores, 4 years of auto-remarks (`tests/test_audit_float.py`) | **All pass** |
| CMS blocks | Whole-paise reference (opening + deposit - same-day - next-day = closing) vs `build_model`: 88 tests incl. day+2 / before-deposit / no-date credits, ADJ and ZPO outside the grid, pseudo-store order, window clipping, 30-day cent chain (`tests/test_audit_cms_blocks.py`) | **All pass** |
| Sheet A-N and R3a | Whole-paise reference for C=D+E, G=D-F, K=I-J, M=H+E-I+L, N=H-I, carry-forward, store start, component builders by hand: 89 tests (`tests/test_audit_store_sheet.py`) | **All pass** |
| Sheet LIVE | Column identities, totals and independent recomputation on run #2 (`scripts/audit_live_sheet_check.py`) | Hold, **except D-001** (5 store-days, net Rs 7,22,748 of cash received understated) - fixed |
| R3a vs ERP cash book LIVE (OB-1) | Run #2 `store_day_agg` vs `gold_mop.store_settlement_lines`: 44,337 store-days, 227 stores, 1 Apr - 14 Nov | see below |

**OB-1 classified.** Opening and closing agree to the paisa on 82.5% / 82.1% of store-days over the whole range. The differences are of three kinds:
1. **November (3,142 of 3,147 break days, net -Rs 10.29 Cr): our November cash sale comes from the truncated POS Excel file (CLAUDE.md 6.37); the ERP is complete.** Explained, not a cash-book error. It should disappear when the run reads POS from the ERP (run #3) - to be proved by repeating this comparison after that run.
2. **Opening seed on 1 April: 21 stores differ, gross Rs 40,460.75.** One store is 99% of it: GPB (ours Rs 2,934.00, ERP -Rs 37,066.00, difference Rs 40,000). The rest are small (Rs 0.01 to Rs 200; for example UNO -200, CTR -100, GWT -76). Constant afterwards. Needs a Finance ruling on which opening is right.
3. **Five single-day differences in April-October, net -Rs 209:** JGB 04/04 -300, SKP 02/05 +100, MCR 14/07 +50, MDB 11/07 -34, MBR 19/04 -25. Unexplained. Petty-cash vouchers are identical (150,947), so the difference is elsewhere in that day's movement. Open item OB-2, below materiality but not dismissed.

Update to risk 8 in section 6: the earlier "about 12% of stores with a constant offset" is superseded - the constant opening difference is 21 stores and Rs 40,460.75 gross; the larger gap in the earlier reading was November.

## 10. Phase 3 results (24 Nov 2026)

**Silent handlers (C-001): all 25 read.** 6 needed a change (logged or now failing loudly: refunds 143, manual_match 229 and 266, masters_db 97 and 157, store_tasks 237 - register D-004 and D-005). 19 are acceptable: they report the error to the operator (uploads x5, the Upload page, masters.py 279, masters_db 397, run_coverage 110), fail closed (auth_gate x2 return "not signed in"), or are documented fallbacks (selfcheck x2, masters.py 603, r1_chunked memory default, run_coverage 93 and 361, runs 558 fresh-session record of a failure, ingest 2173 whose `None` raises a named IngestError).

**Candidates closed:** C-002 confirmed and fixed (D-004). C-004 marked `usedforsecurity=False` (digest unchanged, tested). **C-005 proved a false positive** by a hostile-input test (`tests/test_audit_security_sql.py`): the placeholders can only be `:name` tokens and every value is bound. C-007: the ISO dates in code are template examples and docstrings, except control (g) in the sheet (observation O-1, needs a decision). C-008 logged. C-003 (float money) stands until the Decimal divergence measurement in Phase 2 closes: every independent recomputation so far used whole paise and matched exactly, and the live check found only the D-002 noise (largest 1e-10).

**Schema and constraints reviewed.** Exact `NUMERIC` money on PostgreSQL, ISO text dates everywhere, unique `exception_key`, partial unique indexes enforcing one transaction per active manual match, one active match per bill, one refund per transaction, per-run uniqueness of every aggregate table. Gaps recorded as observations O-2 (no unique key on bill lines), O-3 (redundant indexes), O-4 (table growth).

**Live database integrity (read-only, production database, run #2, `scripts/audit_db_integrity.py`):** keys unique, no orphan references (exceptions, remarks, manual-match events, task events, bill lines), all dates ISO, all statuses in their domains, closed exceptions carry a reason or closer, matched lines name a transaction and unmatched ones do not, C=D+E, K=I-J, M=H+E-I+L, N=H-I on all 45,580 store-days, sheet digital sale = sum of bill-line amounts, carry-forward unbroken, no stuck run, no duplicate indexes, every documented index present. **One finding: D-002 confirmed on live data (86 rows, error 1e-10).**

**Concurrency:** two operators validating the same transaction for two bills and recording at the same moment: exactly one wins, the other is refused, no half-written row is left (6 repeats stable); the database refuses a second active match even with every check bypassed (`tests/test_audit_concurrency.py`).

**Still to do in Phase 3:** transaction-boundary review of the bulk-apply and run persistence paths under a forced mid-way failure; reading `lifecycle.sync_closures`, `store_tasks` and `table_upload` line by line. Not started: these are the largest unread money-adjacent modules.

## 11. Phase 4 results (24 Nov 2026)

**Method.** The app was run on the development PC against the local throw-away PostgreSQL copy of the full April-November run (never against production), at 375, 768, 1366 and 1920 px. An in-page probe checked every page for: horizontal overflow, elements outside the viewport, error panels and tracebacks, stray "nan / None / NaT" text, unlabelled inputs, touch-target size, and hidden raw ligature text. Contrast was computed from the theme tokens. The sign-in page was checked on a phone width, including that nothing is visible before sign-in and a wrong password gives the one generic message.

**Results.** Summary, Store Task, Cash position, Float, Exceptions, Upload and Masters: **no horizontal overflow, no error panel, no traceback, no "nan" text at any of the four widths** (Summary at 768 was reached by direct load and is covered by the 1366 and 375 runs). The phone sidebar opens as an overlay with readable labels and disabled months dimmed. The sign-in page is clean on a phone; Enter submits; the password field is marked `current-password`.

**Defects found and fixed locally:** D-006 (pages re-read all CMS files and rewrote the warehouse on every view - found because the page froze the browser), D-007 (Float on the slow path), D-008 (negative closing balance shown positive), D-009 (text contrast), D-010 (touch targets). Two observations: O-5, O-6.

**Not done and why.** No screen-reader or keyboard-only walkthrough (needs a person); no visual-regression snapshots; the sign-in success path was not repeated in this phase (verified when it was built, and again in the audit's earlier work). Heavy tables were checked for layout but not scrolled to the end.

## 12. Phase 5 results (24 Nov 2026)

**Security - what was tested and how.** Secrets: the repository and all 39 commits scanned for passwords, tokens and connection strings - none. Dependencies: `pip-audit` on the server's exact 63 installed packages. Authentication: read in full, and response time measured. Injection: every one of the 36 places that put text into raw HTML was listed and classified; the export path was tested with hostile remarks; file upload was tested with 12 hostile file names against the real storing function; zip handling was read. Network: ports and firewall on the application server were read (nothing changed).

**Fixed locally (all tested, none deployed):** S-5 (P1, file name decides where the file is written - proven writing outside the folder), S-1 (username timing), S-3 (unescaped text in HTML - proven markup injection), S-4 (typed remark becomes a live Excel formula - proven), S-6 (zip size limit). 34 new tests.

**Confirmed sound:** passwords are salted scrypt (2^15), compared in constant time; session tokens are 256-bit, stored only as hashes, expire after 120 idle minutes or 12 hours; a wrong username and a wrong password give the same message; nothing is shown before sign-in; the production switch `MOP_REQUIRE_POSTGRES` makes the "auth off" development switch inert; raw SQL is parameterised (C-005 disproved by a hostile-input test); zips are never extracted to disk; the ERP login is read-only in three layers.

**Needs a decision or IT:** S-2 lock-out denial of service (the app has a single administrator), S-7 firewall off and plain HTTP, S-8 Pillow CVEs (blocked by Streamlit), S-9 reproducible install (lock file added), S-10 error details, S-11 repository visibility, S-12 the wide ERP login.

**The application is not running on the server.** Port 8501 has nothing listening; the scheduled task `MOP-Reconciliation-App` exists but is idle. I did not start it. (Whether this was deliberate is not known.)

**Performance, measured on the production database with the real run (5,584,483 bill lines, 8 months):**

| | Time |
|---|---|
| Summary data, whole range (Apr - Nov) | 3.5 s |
| Summary data, one month | 0.47 s |
| Summary data, one day | 0.04 s |
| Particulars matrix, whole range | 0.15 - 0.19 s |
| Store cash card | 0.04 s |
| Cash position data (load + model, 225 stores x 228 days) | about 1 s (the page then takes about 5 s to draw the very wide table in the browser) |
| 4 people loading a month at once | 1.7 s each (0.55 s alone); roughly linear beyond two users because one process shares one interpreter |

Float: 82 s on the old path, about 3.6 s warm now (D-007). Cold page loads on the development PC were 5 - 9 s for Store Task, Float and Cash position and 1.1 - 1.9 s for the rest (the browser timing overlaps neighbouring pages, so these are approximate).

**Database observations for the DBA:** PostgreSQL is on near-default memory settings (`shared_buffers` 128 MB, `work_mem` 4 MB, `random_page_cost` 4) while the largest table is 3.1 GB and 52 of 100 connections are used by other applications. Tables are vacuumed and the visibility map is 100% (so index-only scans are effective). The bill-lines table grows about 0.65 million rows a month.

**Not done:** no penetration test of the live server's other services (out of scope: they belong to other applications); no load test beyond 4 users; no TLS test (there is no TLS); the lock-out and session-expiry timings were verified by unit tests, not on the server.
