# MOP Reconciliation (Citykart) - the complete application guide

**Purpose of this file:** to let another AI (or a new engineer) understand the whole application - what it is for, the business rules it enforces, how it is built, every screen and
interface, every data source, how it is deployed and how it is verified - without access to the code. Written 24 Nov 2026 from the live project.

**What is deliberately NOT in this file:** server addresses, database/account names, passwords or keys, and any store-level or company financial figures. Where the real system
uses a specific internal address or login, this guide says "the application server", "the database server", "a shared read-only database login".

---

## 1. In one paragraph

Citykart is an Indian retail chain (about 190 stores). Customers pay at the till by cash or by digital means (cards on Paytm / Axis terminals, UPI QR, Bajaj Pay). **MOP Reconciliation**
("MOP" = mode of payment) is an internal web application that **traces every rupee a store collects on a business date to where the money finally rests**: was every digital bill really
paid, did the gateway settle the right amount on time, did the bank receive it, and does the day's cash movement explain the cash drawer and the cash handed to the cash-pickup agency?
It does this with **nil tolerance** (exact match only), never locks a date (sale data is restated 7-15 days later when IT deletes duplicate bills), never writes a difference off, and keeps
every exception, remark and manual decision as an auditable record.

## 2. The four reconciliations

| | Question | Grain | Inputs |
|---|---|---|---|
| **R1** | Was every digital POS bill paid? Any payment with no bill? | bill line <-> gateway transaction | POS bills; Paytm, Axis EDC, Bajaj Pay transaction reports |
| **R2** | Did the gateway money reach the bank, net of correct charges, on time? | transaction -> payout -> bank credit | gateway settlement fields (payout id, UTR, commission, GST, settled amount/date); bank statement (not yet supplied) |
| **R3a** | Does the day's cash movement explain the store's cash balance? | store x date | opening cash, POS cash sales, petty-cash vouchers (PTC), cash-deposit vouchers |
| **R3b** | Did cash handed to the pickup agency reach the bank? | pickup (code + deposit date + slip) | PTC deposit vouchers; the agency's CMS credit report |

Implemented and live: R1 (matching, exceptions, manual matching), R3a (cash book), a CMS reconciliation (R3b in block form), Float tracking, store tasks. R2 (settlement-to-bank) is only
partly built (the bank-statement leg has no data source).

## 3. Non-negotiable business rules

1. **Nil tolerance.** Exact match only; any difference, however small, is an exception.
2. **No locking.** No business date is ever frozen; every date can be re-run at any time (master data too: lookups use the master version current *as at* the business date).
3. **No write-offs.** A difference is carried as an open item with a remark until explained. The system never nets a difference away.
4. **Nothing vanishes silently.** An exception that disappears becomes `auto_resolved` with a reason; remarks are never lost; every state change is an appended event.
5. **Nothing is guessed.** An unmapped store, MID, TID, pickup code or payment mode is reported by name and value, never dropped or inferred.
6. **Business date = the POS bill date.** Always read from a data column, never from a file or sheet name. A missing input is a reported gap, never filled from a neighbouring day.
7. **Raw files are read-only.** All output goes elsewhere.
8. **No hardcoded dates; no fixed row ranges.** Dates are parameters; whole files are read.

## 4. Architecture

### 4.1 Technology
Python 3.14; pandas, SQLAlchemy; **Streamlit** (web UI) with Altair; **PostgreSQL** in production (SQLite is the development default - same code, connection string in an environment
variable); Parquet files (pyarrow) as a "warehouse" cache; `pyxlsb`/`openpyxl`/`fastexcel` to read Excel; `xlsxwriter` for Excel exports; `psycopg` 3 for PostgreSQL. All dependencies are pinned
(`==`) in `requirements.txt`. About 1,290 automated tests.

### 4.2 Layers

```
Browser (office PC / tablet / phone)  --HTTP-->  Streamlit application (one process, on the application server)
   |  sign-in gate (every page)                       Pages (app/pages/*), shared chrome (app/dashboard_common.py), UI-free engines (app/mop_reco/*)
   |                                                   |
   |                    reads precomputed rows only    |  writes only when a RUN is started (Upload page / script)
   v                                                   v
 PostgreSQL (database server): runs, R1 bill lines, aggregates, exceptions + remarks, manual matches, refunds, store tasks, masters (versioned), users/sessions/audit
 Warehouse folder (application server): per-source, per-business-date Parquet partitions (a fast cache of normalised source data)
 Source folders / ERP views: the raw inputs (Excel, CSV, zip) or, per source, the ERP's read-only views
 File server (NAS): reachable and tested; not yet used for storage
```

### 4.3 Two-phase execution (a core design rule)
* **Phase 1 - an explicit run.** Started deliberately (Upload page "Run reconciliation" or `scripts/run_range.py`), never by browsing. It reads each source once, normalises it, writes per-date
  Parquet, runs R1, persists results/exceptions/aggregates to the database, creates store tasks and CMS inputs. A full April-to-mid-November run takes tens of minutes.
* **Phase 2 - the dashboard reads precomputed rows only.** No page opens a raw file or holds a month in memory. Aggregates are precomputed per store x date; detail tables are paged in the
  query (50 rows per page); exports are a separate full-population path in chunks.

### 4.4 Code layout
```
app/
  Summary.py                     entrypoint and landing page
  pages/                         1_Float, 2_Cash_position (CMS Reco), 5_Masters, 6_Upload (+Run), 7_Store_Task, 8_Exceptions, 9_Users (admins only)
  dashboard_common.py            shared chrome: theme injection, nav bar, sidebar filters, tiles, export buttons, operator identity
  auth_gate.py                   sign-in gate, account menu, forced password change
  masters_editor.py, run_panel.py, match_strip.py, store_detail_view.py, inline_match.py, selfcheck.py
  assets/theme.css               the ONE stylesheet; assets/citykart-logo.png; assets/task_summary_template.xlsx
  config.yaml                    every rule that could change (MOP classes, sources, thresholds, SLAs, auth limits, ERP window)
  mop_reco/                      ~45 UI-free engine modules (DataFrames in, DataFrames out; no Streamlit imports)
scripts/                         run_range, manage_users, erp_probe, erp_shadow, erp_compare_pos, compare_two_runs, build_package, backfills ...
deploy/                          PowerShell: install, start/stop, scheduled task, health check, secrets, service-account permissions
tests/                           ~1,290 tests, one file per area
```
Key engine modules: `ingest` (source readers + shared normalisers), `r1_bill_txn`/`r1_chunked` (matching), `runs` (the orchestrated run), `exceptions`/`lifecycle`/`exception_pairing`/`exception_tables`,
`manual_match`/`bulk_accept`/`refunds`/`table_upload`, `store_sheet`/`store_sheet_source`/`store_sheet_export` (the daily sheet), `mop_matrix` (Summary particulars), `r3a_store_cash`, `cms_blocks`,
`float_tracker`, `store_tasks`/`store_task_summary`, `masters`/`masters_db`/`master_gaps`, `uploads`, `queries`, `export`, `formatting`, `auth`, `erp`/`erp_source`/`erp_ingest`/`erp_shadow`.

### 4.5 Deployment (generic)
Three machines: an **application server** (runs the Streamlit app as a Windows scheduled task under a dedicated service account, starts at boot, restarts up to 5 times; has its own Python and virtual
environment), a **database server** (PostgreSQL, database owned by an application user) and a **file server** (NAS). Releases are a zip package with a SHA-256 manifest of every file
(`scripts/build_package.py`); `scripts/verify_manifest.py` proves the server folder is byte-identical to the build. Secrets (database password, ERP password) are stored machine-encrypted (Windows DPAPI,
machine scope) and reach the app only as environment variables of its own process; they are never in a file, command line, log or chat. A health check (`deploy/health_check.ps1`) verifies the app,
the scheduled task, the database, the NAS and disk space. The app refuses to start against a local SQLite file in production (`MOP_REQUIRE_POSTGRES=1`).

## 5. Data sources

| Source | Format | Key fields MOP reads | How it arrives now |
|---|---|---|---|
| **POS bill lines** (one row per bill x payment line; ~1.2-1.5 million rows a month) | monthly `.xlsb`/`.xlsx`, multiple sheets | bill_no (globally unique `SB58/024800/0726`), site_ref, store, bill date, bill time, MOP name, amount (= tendered - change), **terminal reply JSON** (holds the gateway reference), till operator | Excel files **or** the ERP view (see 5.1) |
| **Petty-cash vouchers (PTC)** | CSV (cp1252, header on row 3) | store, date, voucher no, mode (PAYMENT negative / RECEIVE positive), head, ledger, amount, remarks; a **deposit** voucher = head "Cash/CMS Deposit" **or** ledger "CMS-RECD." | CSV files **or** the ERP view |
| **Paytm settlement** | zip parts of CSV (100 columns, ragged rows), ~640k rows/month | transaction_id, date+time, status, type (ACQUIRING / REFUND / CHARGEBACK / ...), original MID, amount, commission, GST, payout id, UTR, settled amount/date, payment mode (UPI/DEBIT/CREDIT) | files only (not in the ERP) |
| **Axis EDC settlement** | xlsx (card transactions only) | RRN, date, MID, TERM_ID, card type, gross, MDR, GST, net, UTR, process date | files only |
| **Bajaj Pay settlement** | xlsx (81 columns) | Transaction Id, TID, date+time, amount, status (mixed case), MID, net, MDR | files only |
| **Axis CMS (cash-pickup credits)** | zip of CSV (67 columns, duplicate names, ragged) | TYPE (COL / ADJ.CR. / ADJ.DB. / ZPO), DEPDATE, CRDATE, slip no, slip amount, adjustments, location, pickup code | files only |
| **Opening cash balance** | a Tally "Cash Drawer" cost-centre export, as at a date in the file | store (full name) and opening | held in the database now (a one-time import), editable on screen |
| **Customer refunds** | finance team workbook | transaction no, ticket id, customer, store, dates, amount | files (a Customer Refund folder) |
| **Masters** | originally Excel | store list; Paytm MID->store; Axis MID->store; pickup code->store; pickup location split; opening balances | **in the database**, versioned, edited on screen only |

Every loader uses an **alias map** (acceptable header names per logical field, so layouts that differ by month still load); unknown extra columns are ignored, unknown required ones raise a clear error
naming file and field. A source folder may hold one file per month; the loaders read **every** file and resolve overlaps (the same business date or natural key) to the most recently modified file.

### 5.1 The ERP link (read-only), added November 2026
Citykart's data platform (a PostgreSQL "data lake") now exposes MOP-specific read-only views in a schema `gold_mop`, read through a **shared read-only database login** (limits: 120 s per statement, 30
connections shared with another application; MOP uses 2-4, tagged `application_name = mop_reco`):
`pos_bill_lines`, `pos_bill_header`, `void_payment_lines`, `ptc_vouchers`, `store_master`, `calendar` (holiday flag empty), `tid_master`, `payment_heads`, `payment_head_store`,
`store_settlement_day`, `store_settlement_lines` (the ERP's own daily store cash book), `bank_postings` (no UTR), `till_sessions` (no cash amounts). **MOP reads only `gold_mop` and one freshness view.**

* **Three layers of read-only** (`mop_reco/erp.py`): the session opens with `default_transaction_read_only=on`; the code then asks the server and refuses to continue unless the answer is `on`; and
  `run_select()` accepts only one plain SELECT/WITH/SHOW/EXPLAIN (data-modifying words are refused before reaching the server). The password is never logged or printed.
* **Same code path as the files.** `erp_source` renames the ERP columns to the headers the Excel files carry and hands them to the SAME normalisers the file readers use, so a source can be switched
  per environment (`MOP_SOURCE_POS` / `MOP_SOURCE_PTC` = `file` (default) | `erp`) with no code change; `erp_ingest` pulls POS one day per query, caches pulled days as warehouse partitions, and re-pulls
  the last 65 days every run (the platform re-reads 60 days from Oracle daily so deleted duplicates disappear).
* **Proof before switching** (`erp_shadow`, `scripts/erp_shadow.py`): April-October 2026, 214 dates: every bill x payment-mode combination agrees (millions), amounts identical to the paisa, and the
  payment references parsed from the ERP's terminal reply equal those parsed from the Excel files with zero differences. Petty cash agrees voucher by voucher. The ERP is *better* where the Excel files
  were wrong (a November export cut off at Excel's row limit; voided bills the ERP correctly drops; ledgers assigned later). The ERP's store cash book confirms MOP's computed cash book 100% on cash sale,
  deposits and other petty cash, and about 88% on opening balance (the rest differ by a constant per-store starting offset).
* **What the ERP does not have** (these stay as files): Paytm/Axis/Bajaj settlements, Axis CMS credits, bank credits with UTR, Paytm/Axis MIDs, pickup codes, customer refunds, public holidays, CMS T+0/T+1 cycle.

## 6. Business logic in detail

### 6.1 Payment-mode (MOP) classification (config, not code)
`Cash` = cash. `PAYTM EDC`, `PaytmDQR`, `PAYTM DQR MOREFUN`, `Bajajpay UPI/Card` = digital. `EDC OTHER`, `EDC OTHER AXIS` = digital with **no reference** (matched by a special rule).
`Credit Note Issued/Received`, `Gift Voucher` = non-settling (excluded from R1/R2, shown for information). An unknown MOP is tagged `other`, excluded from every figure and reported by name.
For the Summary matrix there is a separate **bucket** per MOP, and **Card vs UPI is decided per matched transaction by the gateway data** (Axis = card; Paytm uses the transaction's own payment mode; Bajaj uses its two
distinct MOPs), never by the POS MOP name (one Paytm EDC terminal takes both card and UPI). **An open (unmatched) digital bill is provisionally counted as UPI** until matched, with a footnote.

### 6.2 R1 - reference extraction and matching
A bill's terminal reply (JSON) yields a reference: Paytm `acquirementId` (EDC) or `txnId` (QR), Axis `retrievalReferenceNo` (always, whether or not a card is indicated), Bajaj flat `rrn`.
* **Governing rule: a bill needs ONE successful gateway match**, not one per reference it carries; an unresolved extra reference is information, not an exception.
* **Tier 1 - reference match**: reference + exact amount + status SUCCESS + type ACQUIRING, searched across the **entire** loaded transaction table with no date restriction (a gateway reference is globally
  unique); a date difference is recorded (`date_diff_days`, a note), never an exception. A matched transaction is consumed once.
* **Tier 2 - EDC OTHER only** (no reference): candidates in the same store, exact amount, same date; exactly one = match, more than one = "ambiguous" (operator decides; time only orders candidates).
* **Non-ACQUIRING rows** (refund, chargeback, reversal) link to their original sale; original absent = an exception.
* **Reverse direction**: every successful transaction no bill consumed is an **unmapped transaction** (money with no bill).
* Execution is chunked (a few days at a time) against a disk-resident index of the whole pool; results commit per business date.
* **Parked lines**: a digital bill whose only possible gateway (Axis) has no data yet for that date is *parked*, not opened as an exception, and announced on every page.
* **Duplicate bills**: an unmatched bill sharing store + date + amount with another matched bill is flagged "suspected duplicate" and auto-resolves when IT removes it.

### 6.3 Exceptions
Keys are deterministic so remarks survive re-runs (bill: date|store|bill_no|mop; unmapped transaction: gateway|transaction_id; ...). Lifecycle `open` -> `under_investigation` -> `closed`, or
`auto_resolved`. Two **risk figures are never added together**: *amount not received* (bills with no payment found - the exposure) and *received, not billed* (a payment with no bill - an accounting gap). A reason
breakdown separates "reference present, transaction not found", "ambiguous", "suspected duplicate", "no reference at the till".

### 6.4 Manual matching, refunds, bulk accept (human overlays, never edits)
* A **manual match** is an overlay row keyed by the bill; the engine's own rows are never edited. **One transaction can back at most one bill** (checked by validation, by the code and by a database unique
  index), with **one recorded exception**: a customer paying two bills with one swipe (a shared payment, amounts must add up exactly). Validation cases: (a) clean - accepted on the paste alone; (b) a difference
  in amount/store/status or already used - needs a written reason and a ticked confirmation; (c) transaction not found - only recorded as "asserted, unverified" and the bill stays open.
* Manual and automatic matches are **never blended**: match rates are reported as matched by system / matched manually / asserted unverified.
* **Customer refunds** close an unmapped transaction without being a bill match or revenue; a Zoho ticket may cover several payments of one customer; a transaction is refunded once; a refund can coexist
  with a bill match on one transaction (bill + refund = transaction exactly). A folder reader ingests the finance workbook (held lines are listed with reasons, never guessed).
* **Bulk accept** of the system's own suggestions: an EXACT rule (one candidate, exact amount, same store) and a SPLIT rule (two transactions whose amounts sum exactly to the bill); preview -> confirm -> apply,
  each row re-validated at apply time; applied in one database transaction as a reversible batch.
* **Candidate suggestions** are a *global assignment*, not a per-bill lookup: a transaction is suggested to at most one bill; ambiguous clusters are left blank and offered to a resolver.
* Everything is **reversible with a reason**; reversal keeps the record (append-only events).

### 6.5 The daily store sheet (columns A..N) and the Summary matrix
One row per store per date: **A** Date, **B** Store, **C** Total sale (= D+E), **D** Digital sale, **E** Cash sale (bills with a number only), **F** Digital received (matched part of D), **G** Digital receivable (= D-F),
**H** Opening balance, **I** Cash deposit (positive), **J** Cash received (CMS credits on the *deposit* date: COL + ADJ.CR. - ADJ.DB.; ZPO ignored), **K** Cash receivable (= I-J), **L** Cash expense/income (net PTC
excluding the deposit head), **M** Closing (= H+E-I+L), **N** Float (= H-I). Eight lettered controls plus a per-date-equals-sum-of-stores check are asserted and shown (silent when clean, a red banner when not).
The **Summary particulars matrix** (Sale / % to total / Collected / Receivable for Total sale, UPI, Card, Total digital, Total cash, with Gift Voucher and Credit Note lines behind a +/- toggle) is in Rs Cr.

### 6.6 R3a cash book
`closing(D) = opening(D) + cash sale(D) + net petty cash(D) - deposits(D)`; `opening(D) = closing(D-1)`; the first opening comes from the opening-balance master. POS rows with no bill number are cash in/out that mirror the
PTC ledger and are **excluded** (otherwise every movement would count twice). The cash book has internal-consistency checks (no negative closing, a closing above a threshold, runs of days with sales but no
deposit - working-day aware, the estate identity, the carry-forward chain). **An independent confirmation now exists** from the ERP's own store cash book (section 5.1).

### 6.7 CMS reco (R3b in block form, the "Cash position" page)
One block per **deposit date**, chained per store: OPENING + Cash/CMS Deposit - CMS Report (1) (credited the same day) - CMS Report (2) (credited the next day) = CLOSING. A credit two or more days later sits
in neither column and stays in CLOSING, listed under "Credited later than day+1". Pickup codes resolve to stores through a location-aware master (one code can serve two stores). Per-block overrides
(applied to the bank's figure, revertible), remarks per store+period, a bulk override upload, and an Excel workbook with live formulas.

### 6.8 Float
**Float = opening balance minus the cash deposited that day** (not the closing balance). A configurable threshold (with optional per-store overrides) flags a *high float*; a per-store streak counts consecutive
high days; weekends (Sundays, 2nd/4th Saturdays) and listed holidays get a computed, never-stored auto-remark; operator remarks are keyed by store+date with append-only history; negative float is shown, never floored.

### 6.9 Store Task
Tasks are **generated from data**, close themselves when the data clears them, and a store's reply never closes one. Types: *open bill* (an unmatched bill), *high float* (float above threshold, not weekend-explained),
*CMS not done* (no deposit when due, or a deposit not credited by T+1 working day). Each has raised-on, due-by (SLA working days), a store reply (via a bulk-upload template), HO status (Open -> Pending (replied) -> WIP ->
Closed), history, and a per-store summary.

### 6.10 Masters
Six masters held in the database: store (full name -> short code), Axis MID -> store, Paytm MID -> store, pickup code -> store, pickup location split, opening balance. **Every change is a new version** (who/why/when/kind);
old versions are never rewritten; a lookup for a past business date uses the version current as at that date, and an identifier absent from that version falls back to the earliest later version that holds it (so a store
opened after the last master load still resolves). Edited **only on screen** (grid editor with a diff preview, required reason and operator name, history, one-click restore as a new version). Store code replacements
(old -> new, config) apply everywhere a code is read. A pre-flight checks masters at the start of every run.

## 7. Database (main tables)
`runs`, `run_files`, `r1_bill_lines`, `reconciliation_results`, `exceptions`, `remarks`, `manual_match_batches`, `manual_matches`, `manual_match_events`, `transaction_refunds`, `refund_events`, `float_remarks`,
`float_remark_events`, `store_tasks`, `store_task_events`, `cms_slips`, `cms_ptc_deposits`, `cms_reco_remarks(+events)`, `cms_overrides(+events)`, `master_gaps`, aggregates (`store_day_agg`, `date_total_agg`,
`mop_day_agg`, `digital_mode_agg`, `txn_day_agg`, `parked_bill_lines`), `master_versions`/`master_rows`, `uploaded_files`, `app_settings`, and the sign-in tables `app_users`, `app_sessions`, `auth_events`.
Schema upgrades are applied automatically at start (`create_all` plus lightweight added columns/indexes); important composite indexes exist for the paged views and must not be dropped.

## 8. User interface

### 8.1 Global chrome (every page)
* **Sign-in gate first**: nothing is drawn or queried until a person signs in (see 9). The first thing on screen otherwise is a centred card with the logo, username, password and "Sign in".
* **Brand bar** (dark blue gradient) with the supplied **CityKart MOP Reco logo on a white plate** at the left and "MOP Reconciliation" beside it.
* **Navigation bar** (white, sticky under the brand bar): *Summary | Store Task | Cash position | Float | Exceptions | Upload | Masters* (+ *Users* for administrators). The open page is a glossy gradient pill; at the right an
  **account menu** (the signed-in name -> Sign out, Change my password). It scrolls sideways on narrow screens and never wraps.
* **Page title** (small) with one purpose line, then the **Live pill**: "Live - run #N - date range - store filter" (its tooltip carries the build stamp).
* **Sidebar filters, global on every page**: *From*, *To* (DD/MM/YYYY, empty = every loaded date, the resolved range is written in the pill), *Year* and *Month* quick-pick buttons built from the loaded data (a year is
  offered only if it has data, plus the next calendar year), *Store*, *Clear filters*, *Refresh* (re-reads the database; it does **not** re-run the reconciliation). Page-specific filters stay on their page.
* **Coverage banners**: what the run covers and what is not reconciled (never zeros for missing data), provisional periods, parked lines awaiting a gateway file, a POS-completeness warning when an export looks cut off.
* **Tiles** show rupee amounts (never truncated; the exact figure is in the tooltip), at most four per row; **sections** have +/- toggles whose header shows the count even when collapsed, and a collapsed
  section does not do its expensive work. Tables are Streamlit grids (canvas-drawn), with status columns tinted *and* labelled in words; plain hairline HTML tables are used only for the fixed Summary matrix and cash card.
* Money: Indian digit grouping everywhere; dates DD/MM/YYYY; reference ids are always text (a Paytm id is 35 digits and Excel would corrupt it as a number).

### 8.2 Pages
* **Summary** (landing): (1) the **particulars matrix** - Sale / % to total / Collected / Receivable across Total sale, UPI, Card, Total digital, Total cash, with a +/- for Gift Voucher and Credit Notes; (2) the **store cash card** - an equation
  *Opening + Cash sale - Cash deposit + Expense/income = Closing at store*, and below it *Cash deposit - Cash received = Cash receivable* (never negative on screen; a negative is a data defect listed on Float), each with mismatch
  checks; (3) **Store-wise detail** (collapsible): one row per business date (all stores consolidated, columns A..N) with a TOTAL row; clicking a date opens an expander with that date's store rows, and clicking a store row
  opens the drill-down (digital bills, cash bills, PTC rows, CMS credits); Expand all/Collapse all; the **Excel export** ("Build the Excel sheet": five tabs - by date, by store and date, by store, excluded CMS types, controls - with
  live formulas); (4) the **"POS sale bills and settlements transactions" strip**: Bills (Closed/Open/Total) and Transactions (Auto / Manual / Refunded / Unmapped / Total) with asserted two-sided controls (MISMATCH in red); (5) a collapsed
  **Data notes** area with the full controls table, run notes, unresolved pickup codes and provisional UPI.
* **Exceptions**: the work queue and the whole manual-matching surface. *Tiles/controls* (one overview shared with Summary: open + auto + manual = total, auto + manual + refunded + unmapped = total); *Show* filter (Open / Under
  investigation / Closed); **Exception bills** table (date, time, store, bill no, MOP, amount, till operator, status, **Txn 1 / Amount 1 / Txn 2 / Amount 2** candidate transactions, "+N more", a **Match** button, remark) and
  **Exception transactions** table (gateway, transaction id, date, time, store, amount, payment mode, status, matched bill, customer, ticket id, refund date, remark), 50 rows per page with Export-all; inline edits
  (paste a transaction id, type a remark) are *validated first*, listed as "pending - validated, not yet saved", then saved in one action; **Upload** a filled export (preview -> confirm -> one database transaction);
  **Bulk accept** buttons (Exact / Split) with preview and reversible batches; an ambiguous-cluster resolver; "Second transaction still open"; a collapsed "Awaiting Axis - not an exception" list; a customer-refund
  folder loader; *Manual match bills / transactions* and *Customer refunds* tables with reversal; a register of every manual match and refund ever recorded; and a collapsed **Matched** section (matched bills with the transaction id in full and the day difference).
* **Store Task**: tiles *Open / Pending / WIP / Closed*; one filter row (Store code, Search, Type, Status); the task table; open a task by id (ST-000123); reply / mark under review / close with a reason; history; "Check tasks against the data
  now"; **Upload store replies** (template, per-row validation, preview, confirm); per-store summary; Export-all.
* **Cash position** (CMS reco): period selector; per-store summary of date blocks; per-store+period remarks with history; "Credited later than day+1" (T+10 flagged); unresolved pickup codes; rows not in the grid; the date blocks; overrides
  (apply / revert / bulk upload); export of the wide workbook; a bridge explaining any difference to store-sheet column J.
* **Float**: tiles (stores above threshold on the last date, total float above threshold, high-float store-days without a remark, longest streak); the float grid (opening, cash sale, deposit, float, threshold, high?, days high, remark);
  filters (All / High only / Unremarked / Weekend-explained); per-row remark with history; a **Data quality** area (negative float; credits exceeding deposits); bulk remark upload; export.
* **Upload**: *Supply a file* (several files at once, e.g. the parts of one Paytm export; each checked on its own - header, rows, date range, wrong month - with a preview before anything is stored); *Upload history* with a **Delete** per
  file (moves it to an archive, keeps the row, names the run that used it); **Run reconciliation** (date range, a readiness message per month - complete / incomplete / provisional - the Run button is disabled with a red warning if a required source
  is missing, run history, heartbeat and progress).
* **Masters**: the master editor (grid, diff preview, reason, history, restore); version history per master; health checks (a MID mapped to two stores, etc.); a working-day due-date calculator; "Unmapped - awaiting Axis"; the configuration in force.
* **Users** (administrators only): the people list with status; *Add a person* (a generated temporary password shown once); *Manage a person* (unlock, deactivate/reactivate, change role, reset password); the latest 200 sign-in/user events.

### 8.3 Design system (one stylesheet)
`theme.css` holds every colour, font and spacing as CSS custom properties: canvas `#F3F5F8`, navy `#1A4D8F` (glow `#2E6FE6`), hairline `#E3E9F0`, semantic green/amber/red, Manrope + JetBrains Mono, compact heading scale, a 3px
top accent bar on tiles (colour only for two named "attention" tiles and genuine breaks), glossy gradient primary buttons with white text, card-style expanders. Streamlit's own theme file carries the same primary/text colours
(the grid's canvas colours cannot be set by CSS). Responsive for phones (375), tablets (768), laptops (1,366) and desktops: no horizontal page overflow on any page (measured), the nav and wide tables scroll sideways inside their cards,
inputs are 16 px on phones, tap targets are finger-sized. Red is reserved for critical state. The Material-icon font does not load offline, so native icons are replaced by plain characters in CSS.

## 9. Security and access
* Local users in the database; passwords stored only as salted **scrypt** hashes; a wrong username and a wrong password give the same message; **5 wrong passwords lock the account for 15 minutes** (an administrator can unlock);
  a browser holds a random token in a SameSite=Strict cookie (only its SHA-256 is stored); sessions end after 120 idle minutes or 12 hours, on sign-out, on deactivation, on a password change or reset; a temporary password forces
  the person to choose their own at first sign-in; the last active administrator cannot be deactivated or demoted; every sign-in, failure, lock-out, sign-out and user change is appended to an audit table. Two roles today: **admin** (also manages
  users) and **user**. The signed-in person's unique full name is stamped on every remark, match, refund, upload and run (no "who are you?" prompts).
* **Known limits**: the app is served over plain HTTP on the internal network (HTTPS is the recommended next step); finer permissions (read-only users, admin-only Upload/Run/Masters) are not built; the host firewall on the application server is off,
  so the login is the main protection.

## 10. Exports
Every export covers **every row matching the current filters** (not the page), the button reads "Export all N matching rows", and the filtered count and exported count are asserted equal. Up to 100,000 rows -> `.xlsx`, above -> `.csv` (the UI says which and why).
Every export opens with a header block (run, timestamp, date range, filters, row count, total value, export time). Reference ids are written as text. The exception tables, the upload template and the export share ONE column definition so they cannot drift.
Exports are downloaded by the browser; the server stores none.

## 11. Performance design
Summary reads precomputed per-store-per-date rows (about 1.7 s against about 33 s before); the store filter and date range are pushed into the SQL; the manual-match overlay is applied live to the aggregates so a match moves every figure
without a re-run; exception tables are paged in the query (a collapsed section reads only its count); R1 is chunked against an on-disk index; multi-file POS is cached per file; set-based validation for uploads; named composite indexes serve the paged detail and the headline aggregates.

## 12. Quality, verification and working discipline
* About 1,290 automated tests (engines, readers, matching equivalence against a frozen reference matcher, manual-match rules, exports, chrome, sign-in, ERP readers), run on a throw-away database, never the live one.
* A self-check runs on every page load: every function a page calls must exist in the modules *as loaded*, and a process older than the code on disk is named rather than allowed to crash (Streamlit keeps modules in memory).
* UI changes are verified in the running application, not only by tests; a passing suite is not treated as proof that the app works.
* Results are compared across installations by per-date fingerprints, and across data sources by the shadow comparison; acceptance numbers recorded in the project brief must match exactly.
* Scope narrowing is always stated up front; dates and sources are parameters; every master/remark/match/refund change keeps its history.

## 13. Current status and open items (24 Nov 2026)
* **Live**: sign-in, the full UI above, R1, cash book, CMS reco, float, store tasks, manual matching/refunds/bulk accept, masters in the database, the ERP read-only link and readers (POS, petty cash) proven against the Excel
  files for April-October; the ERP source mode is built and tested, and its first live run is being prepared (POS and petty cash from the ERP; Paytm/Axis/Bajaj/CMS stay as files).
* **Open**: HTTPS and finer roles; database backup; the NAS evidence archive for uploaded files; the bank-statement leg of R2 and the remaining external feeds (a "landing place" for the outside-company files was offered by the platform team);
  the holiday list and CMS cycle per store; a Finance ruling on per-store opening-balance offsets (the ERP's cash book vs the opening import); a narrower read-only ERP login limited to MOP's schema; a ruling on one foreign-currency MOP.

## 14. Glossary
**MOP** mode of payment. **EDC** card swipe terminal. **DQR** dynamic QR. **MID/TID** merchant/terminal id. **RRN** retrieval reference number. **UTR** bank transfer reference. **MDR** merchant discount rate (card charge). **PTC** petty-cash voucher.
**CMS** cash management service of the bank (the cash-pickup agency's credit report). **COL/ADJ.CR./ADJ.DB./ZPO** CMS row types (collection / adjustment credit / adjustment debit / pseudo-code, ignored). **Float** opening cash less the day's deposit.
**Defect B** a CMS credit larger than its deposit (a credit posted against the wrong date or a shared pickup code). **Parked** a bill that cannot be judged because its gateway has not delivered. **Shadow run** reading a new source beside the old and proving they agree.
**Gold / silver / bronze** layers of the data platform (MOP reads only the `gold_mop` schema).
