# Citykart MOP & Cash Reconciliation — project brief

Read this file at the start of every session. It is the single source of truth for
business rules, column names and acceptance numbers. If anything in a task prompt
contradicts this file, stop and ask.

---

## 1. What this system does

**The application is named "MOP Reconciliation", not "MOP & Cash
Reconciliation"** (2026-09-10). R3a and R3b are both blocked, so cash
reconciliation is not running and the name must not claim it — in the page
title, the browser tab, the launcher or the export header block. "Cash
reconciliation to follow" appears as a small note under the title, never as
part of the name. When R3a and R3b go live, rename it back.

Traces every rupee a store collects on a business date through to where the money
finally rests — cash in the drawer or the bank, digital collections credited by
Paytm, Axis or Bajaj Pay.

Three independent reconciliations, joined only for display:

| | Reconciliation | Grain | Question |
|---|---|---|---|
| **R1** | POS bill ↔ gateway transaction | Bill line ↔ transaction | Was every digital bill paid? Any payment with no bill? |
| **R2** | Transaction ↔ settlement ↔ bank | Transaction → payout → bank credit | Did the money reach the bank, net of correct charges, on time? |
| **R3a** | Store cash book | Store × date | Does the day's cash movement explain the drawer? |
| **R3b** | CMS deposit ↔ bank credit | Pickup | Did the cash handed to the agency reach the bank? |

---

## 2. Non-negotiable rules

1. **Nil tolerance.** Exact match only. Any difference, however small, is an exception.
2. **No locking.** No business date is ever frozen. Sale data changes 7–15 days later
   when IT deletes duplicate bills. Every date must be re-runnable at any time. This
   applies to master data too — see §6.12.
3. **No write-offs.** A difference is carried as an open item with a remark until
   explained. The system never nets a difference away.
4. **Nothing vanishes silently.** An exception that disappears is marked
   `auto_resolved` with a reason. It is never deleted. Remarks are never lost.
5. **Nothing is guessed.** An unmapped store, MID, TID, pickup code or MOP is
   reported by name and value. Never dropped, never inferred.
6. **Business date = POS bill date.** Every column in an output row belongs to the
   same business date. A missing input is reported as a gap, never filled from a
   neighbouring day. Business dates are always read from data columns — see §6.10.
7. **Raw files are never modified.** Read-only. All output goes elsewhere.
8. **No hardcoded dates anywhere.** Dates are parameters.
9. **No fixed row ranges.** Read whole files. Never assume a row count.

---

## 3. Tech stack

- **Python 3.14** (`pythoncore-3.14-64` on the build machine). 3.12 was the original
  target but isn't installed anywhere on this machine (only 3.14 and an unrelated
  3.13 install were found on 2026-08-24) — rather than install a new runtime,
  we're staying on 3.14 and recording it here as what we actually run. Railway
  must be pinned to the same 3.14 to match.
- pandas, SQLAlchemy
- `pyxlsb` for `.xlsb`, `openpyxl` for `.xlsx`, `xlsxwriter` for exports
- Streamlit for the dashboard, Altair for charts
- SQLite while building → Postgres on Railway. Same code, connection string in config.
- `pyarrow` for Parquet, `holidays` + `python-dateutil` for the working-day calendar
- **`requirements.txt` pins exact versions** (`==`, not ranges) so the build is
  reproducible and Railway installs the same versions proven locally.

---

## 4. Folder layout

```
MOP_Reco/
  data/                          raw dumps, READ ONLY
    pos_store_wise/  2026-08-04/  *.xlsb|xlsx
    pos_bill_wise/   2026-08-04/
    ptc/             2026-08-04/
    paytm_txn/       2026-08-04/
    paytm_settlement/2026-08-04/
    axis_edc/        2026-08-04/
    axis_cms/        2026-08-04/
    bajaj/           2026-08-04/
    bank_statement/  2026-08-04/
  masters/                       store, paytm_mid, axis_mid_tid, pickup_code, holidays
  warehouse/                     normalised parquet, one folder per source
  db/                            reco.sqlite — results, exceptions, remarks, runs
  exports/                       Excel outputs
  logs/
  app/
    config.yaml
    mop_reco/
      __init__.py  config.py  io_.py  masters.py  ingest.py
      r1_bill_txn.py  r2_settlement.py  r3a_store_cash.py  r3b_cms.py
      receivable.py  exceptions.py  runs.py  export.py  manual_match.py
      store_sheet.py  store_sheet_export.py  store_sheet_source.py
    Summary.py                   the ENTRYPOINT and landing page
    inline_match.py              the inline paste-to-match cell (imports Streamlit)
    pages/                       1_Float 2_Bill_match 3_Transactions 4_Exceptions
                                 5_Manual_matches 6_Digital_sale 7_Run 8_Masters
  tests/
```

**Current reality (do not restructure):** the real data folders on disk today are
*not* in this layout — they are the six numbered folders as originally handed over
(`1. POS Sales`, `2. Settlement reports\Axis`, `2. Settlement reports\Bajaj Pay`,
`2. Settlement reports\Paytm`, `3. CMS received`, `4. Petty cash ledger`,
`5.CMS DEPOSIT MASTER`, `6.MID WISE STORE CODE MASTER`, and — added 2026-09-10 —
`closing balance at store\Active site vs store code` and
`closing balance at store\July opening balance`). They are never renamed or
moved. Note the folder is `closing balance at store`, not "Cash balance at
store"; the name is a container only and is never interpreted. `app/config.yaml` carries a `sources:` map from each logical source name in
§5 to its real path on disk. The tree above is the target shape for `warehouse/`,
`db/`, `exports/`, `logs/` and the app code only — it does not describe `data/` as
it exists today.

**File discovery rule (confirmed 2026-09-02):** scan the configured source path
**plus its immediate subdirectories — depth 1, never deeper**. Month folders sit
one level below every configured source root (`1. POS Sales/July`,
`2. Settlement reports/Paytm/July'26`, `3. CMS received/July'26`, …), while the
two masters have no subfolder at all. Naming is inconsistent by design — POS
uses `July`, everything else `July'26` — so **the folder name is never
interpreted**: it is a container only, and the business date always comes from a
data column (§6.10). `July`, `July'26`, `Aug'26`, or several month folders side
by side all work with no code change, and a mislabelled folder cannot corrupt a
date.

A non-recursive scan is a silent-wrong-answer bug: before this rule the loaders
saw only the loose single-day files in each source root and would have kept
reconciling 20-July while reporting it as July.

**File selection rule:** where more than one discovered file covers the same
business date, take the **most recently modified**. A re-dump supersedes the
earlier one — so the monthly export supersedes the loose daily file for any
overlapping date.

---

## 5. Column dictionary — real names from the actual files

Headers differ between dumps of the same source. Every loader uses an **alias map**:
a list of acceptable names per logical field. Unknown headers raise a clear error
naming the file and the missing field. Unknown *extra* columns beyond what's listed
here are ignored, not rejected — several sources carry undocumented columns (noted
below) that are harmless.

### POS sale — store wise (`.xlsb`, header on row 2)
`CUBENAME · STORE_NAME · BILL_MONTH · BILLDATE · MOP · START_DATE · END_DATE ·
COLLECTION · FINANCIALYEAR · SITECODE · MOP TYPE`

| Logical | Source |
|---|---|
| store | `STORE_NAME` |
| date | `BILLDATE` |
| mop | `MOP` |
| mop_type | `MOP TYPE` |
| amount | `COLLECTION` |

> **Not supplied as of 2026-08-24** (this is the store-WISE SALE dump, which is
> still absent — not to be confused with the STORE MASTER, which arrived
> 2026-09-10; see "Store master" below). Until it arrives:
> - Derive store-level cash and digital totals from the bill-wise dump instead:
>   sum `BASEAMT` grouped by `STORE_NAME` + `BILLDATE`, filtered by `MOPDESC`.
> - Keep the bill-wise-vs-store-wise pre-flight check in the code (§6.11), but
>   when the store-wise file is absent it must report
>   `"store-wise dump not supplied"` as its own named condition — never pass
>   silently just because there's nothing to compare against.

### POS sale — bill wise (`.xlsb`, header on row 1)
`ADMSITE_CODE · BILLDATE · PSITE_POSBILL_CODE · MOPDESC · MOPSTLMDESC · BASETENDER ·
BASEBALANCE · BASEAMT · TPEDCRESPONSE · TPBILLREFNUMBER · STORE_NAME · BILLNO ·
Column1 · Paytm trnx · Axis trnx · Column2 · Column3`

| Logical | Source | Note |
|---|---|---|
| bill_no | `BILLNO` | e.g. `SB58/024800/0726`. **This is the bill number.** Globally unique |
| site_ref | `PSITE_POSBILL_CODE` | e.g. `SSRKPHB-145937`. Site-wise reference, not the bill number |
| store | `STORE_NAME` | |
| date | `BILLDATE` | |
| mop | `MOPDESC` | |
| amount | `BASEAMT` | |
| edc_response | `TPEDCRESPONSE` | JSON blob |
| bill_time | `BILL_TIME` | An Excel TIME serial — a fraction of a day (`0.7783796296296296` → `18:40:45`), never a datetime or string. Required (§6.11 schema guard): a WARNING is logged, named per file, if a file that should carry it does not — see §6.9d/e's own note on this |
| created_by | `CREATEBY` | The till operator, text as-is (e.g. `CASH2`, `CS15502-Priya kumari`) |

`Paytm trnx` / `Axis trnx` are the user's helper columns — use only as a fallback if
the JSON is unreadable.

> **`BILL_TIME`/`CREATEBY` confirmed present on every real monthly `.xlsb`
> export (April-August 2026), 100% non-null across all 8,593,496 rows
> (2026-09-29).** Absent only on the two permanently-superseded duplicate
> files this source folder also still holds (`MOP SALE DATA.xlsb`, the
> retired 20-Jul daily `.xlsx` sample) — both already lose in file-overlap
> resolution regardless, so their absence is expected, not a defect. Kept
> OUT of the header-detection anchor columns above on purpose (requiring
> them there would make discovery raise on those two harmless, already-
> losing files instead of just picking the winner) — see
> `POS_BILL_WISE_OPTIONAL_COLUMNS` in `ingest.py`.

> **As supplied (20-Jul-2026 sample):** only the 12 core columns above exist — no
> `Column1`, `Paytm trnx`, `Axis trnx`, `Column2`, `Column3`. The loader must not
> require them; use the JSON-extraction rule in §6.2 as the only source of the
> reference, never these helper columns as a primary source.

> **The monthly `.xlsb` export spans MULTIPLE SHEETS** (confirmed 2026-09-02 on
> the July dump: 222 MB, `Sheet1` = 791,166 rows for 01–15 Jul, `Sheet1 (2)` =
> 698,975 rows for 16–31 Jul, **1,490,141 total**). The split exists because the
> month exceeds Excel's 1,048,576-row sheet limit. The loader must **iterate
> every sheet present and concatenate** — never `sheets[0]`, which would
> silently drop 47% of the month — must not hardcode `Sheet1` / `Sheet1 (2)`
> (a larger month adds `Sheet1 (3)`), must assert the header is identical
> across sheets and fail loudly if not, and must report per-sheet row counts so
> a lost sheet shows up as a number rather than a silent shortfall.

> **`BILLDATE` in the `.xlsb` is an Excel serial number, not a date** — e.g.
> `46223.0` is 2026-07-20 (serial 46204 = 2026-07-01, epoch 1899-12-30). Passing
> it to a plain date parser yields 1970-era garbage. The `.xlsx` daily export,
> by contrast, carries real datetimes. Both shapes must be handled.

> **Streaming cost:** ~150s to read all 1.49M rows once via `pyxlsb`
> (uncontended). This is why raw files are read once into Parquet and never
> touched again during a page interaction — see §6.14.

### PTC vouchers — **layout varies by which columns are present, not by month**

Earlier revisions assumed the columns differed between a "July layout" and an
"August layout". That's wrong — the 21-Jul-2026 sample uses the August-style
columns below. **Detect the layout by which columns are present in the file,
never by the file's date.**

| Logical | Layout A | Layout B (seen in both July and August samples) |
|---|---|---|
| store | `STORE_NAME` | `Source Short Name` |
| date | `BILLDATE` | `Billdate` |
| voucher_no | `BILLNO` | `Billno` |
| mode | `PTCMODE` | `Ptcmode` |
| head | `PTCHEAD` | `PTC Head` |
| ledger | `LEDGER` | `Ledger` |
| amount | `POSTED_AMOUNT` | `PTC Bill Amount SUM` |
| remarks | `REMARKS` | `Remarks` |

Layout B also carries `Sub Ledger, PTC Bill Status, Settlement Status, Terminal,
Noofprints, Createdon, Createdby, Lastmodifiedon, Lastmodifiedby` — undocumented
before now, harmless, ignored by the loader.

**Amount rule: always use the PTC bill amount** (`PTC Bill Amount SUM`, or
`POSTED_AMOUNT` where that is the only column). Confirmed identical to
`Posted Amount SUM` on every row seen so far, but the bill amount is the
authority. PAYMENT rows are negative, RECEIVE rows positive.

**Cash deposit — two co-identifiers, not one:** a deposit voucher is
`PTC Head == "Cash/CMS Deposit"` **and** `Ledger == "CMS-RECD."`. Both are
accepted identifiers; on every file seen so far the two sets of rows are
identical (0 disagreements). If a future file ever has one without the other,
treat it as a data-quality exception rather than assuming either identifier
alone is authoritative. Sign-flip with `× −1` to get a positive deposit.

**File mechanics confirmed on the sample:** CSV, encoding **`cp1252`, not UTF-8**
(a UTF-8 read throws `UnicodeDecodeError`). Header is on **row 3** — row 1 is a
`Cube Name:...` title line, row 2 is blank. The header row is **found** by
looking for the mode column under either layout, never assumed to be row 3.

**Full July, measured 2026-09-10:** 21,690 rows across all 31 dates, layout B,
`PAYMENT 19,148 / RECEIVE 2,542`, **4,490** `Cash/CMS Deposit` vouchers
totalling **489,898,591.00**, and **0 co-identifier disagreements** — every
`Cash/CMS Deposit` row also carries Ledger `CMS-RECD.` and vice versa.

### Paytm settlement (documented before as "transaction report" — **as supplied, it already is the settlement report**)

Delivered as `.zip`, each containing a Spark-style export: a `_SUCCESS` marker plus
one `part-00000-*.csv`. Not `.xlsx` as previously assumed.

Real column count: **100**, not 103. Use: `transaction_id · order_id ·
transaction_date · status · transaction_type · original_mid · amount ·
commission · gst · payout_id · utr_no · settled_amount · settled_date ·
payment_mode · settlement_type`

- `transaction_id` matches the bill's `acquirementId` / `txnId`
- `payment_mode` ∈ {`UPI`, `DEBIT_CARD`, `CREDIT_CARD`} — **this decides the
  settlement channel**
- No `Store Code` column — derive via `original_mid` → the Paytm sheet of the MID
  master
- No standalone `time` column — time is embedded inside `transaction_date`
  (e.g. `'20-07-2026 19:21:19'`), parse it out. This is the **transaction**
  time, confirmed distinct from `settled_date` (a pure date, no time — the
  later T+1 settlement date). 100% non-null across all 3,570,117 rows
  checked (2026-09-29). Stored as `transaction_time` and, for an unmapped
  transaction, on `Exception_.transaction_time` (§6.9)
- `settled_amount`, `settled_date`, `payout_id`, `commission`, `gst` are all
  populated on every row supplied — this file already carries everything R1 *and*
  R2 need; there is no separate plain transaction-only feed to load
- Values arrive wrapped in single quotes (`'SUCCESS'`) — strip them

**The monthly export is RAGGED — some rows carry more fields than the header**
(found 2026-09-04 on the July dump). 47,276 of 642,165 rows have **105 fields
against a 100-column header**; never fewer, always exactly 5 extra, always at
the tail. Every named column still lands in its correct position (verified:
`payment_mode` at index 22, `settled_amount` at index 26), so the first 100
fields are the real record and the surplus is a wider export schema. The loader
trims to the header width, counts it, and reports it as
`breakdown.ragged_rows`; a short row is padded and counted separately, since
that would instead suggest a truncated export. Passing ragged rows straight to
pandas raises `100 columns passed, passed data had 105 columns` and takes the
whole run down.

**Delivered as multiple zip parts for the same period — read, concatenate, dedupe.**
The 20-Jul sample arrived as three separate zip files with opaque, undated
filenames. The loader must:
1. Read every zip part present in the source folder for the date, not just one.
2. Concatenate them.
3. Dedupe on `transaction_id`.
4. Log rows read per part, the total row count before dedupe, and the total
   after — so an overlap between parts is visible in the run log, never assumed
   away.

### Axis EDC report (`.xlsx`, 32 columns, not 34)
Use: `RRN · TRAN_DATE · TRAN_TIME · MID · TERM_ID · CARD_TYPE · GROSS_AMT · MDR ·
GST · NET_AMT · UTR · PROCESS_DATE`

- `RRN` matches the bill's `retrievalReferenceNo`
- Card transactions only, by design
- `TERM_ID` is a real column in the file (confirmed on the 20-Jul sample — it's
  the first column). There is also a separate `TI` column (a channel flag, e.g.
  `'POS'`) — do not confuse the two. `TERM_ID` is not needed for store
  resolution (see resolver below); it's read and stored for traceability only.
- No `Store Code` column exists. Store is derived via `MID` alone against the MID
  master — the MID is common across both the Axis and the Paytm reports, and one
  MID resolves to exactly one store.
- **`TRAN_TIME` is NOT a real transaction time — confirmed 2026-09-29 against
  the whole 5-month, 134,887-row dataset: every single row, every month,
  carries the identical literal string `'000000'`, with no exceptions.** It
  is a constant placeholder Axis's own export always fills, never a captured
  time. Axis genuinely has no usable time field in this file — never
  substitute `PROCESS_DATE` or anything else for it. `transaction_time` is
  stored as a real, separate column and is always `None` for Axis rows; the
  raw `TRAN_TIME` field is still read and kept for traceability only, exactly
  as before.

**MID resolver:**
```
if MID resolves to exactly one store in the master:
    store = that store
else:
    store = null, status = "store_ambiguous", surfaced in the
    unmapped / master-data-gap report — never guess, never assign
    either candidate store
```
This is a safety net, not a design requirement — MID→store is expected to be
strictly one-to-one, so the "else" branch should never fire in normal operation.
It exists because the master previously had one conflicting MID
(`037215021610005`, mapped to both `GLA` and `MBA`), corrected on 2026-08-24 to
`GLA` only. The pre-flight check in §6.11 is what catches the next one on the
day it appears, instead of it sitting undetected in the master like this one did.

### Axis CMS settlement (`.xlsx`, 67 columns)
Use: `TYPE (A) · DEPDATE (H) · CLGDATE (J) · CRDATE (K) · SLIPNO. (P) ·
SLIPAMOUNT (W) · RECOVEREDAMOUNT (X) · LOCATIONNAME (E) · PICKUPPOINTCODE (BO)`

- **Credit date = `CRDATE`.** Not CLGDATE, not RECDATE
- **Credit amount = `SLIPAMOUNT`**
- Store comes from `PICKUPPOINTCODE` via the pickup code master
- The raw file has two pairs of duplicate header names (`RECOVEREDAMOUNT` and
  `SUBCUSTOMERNAME` each appear twice, at different positions) — read by column
  letter/position, not by a `dict`-style header lookup that would silently keep
  only the last occurrence.

### Bajaj Pay settlement (`.xlsx`, 81 columns)

Real data arrived 2026-09-02 (July dump: 573 rows). **Bajaj is in R1** —
digital bill = Paytm + Axis + Bajaj Pay. Field mapping, derived and verified
against the data rather than from column names:

| Logical | Source column | Note |
|---|---|---|
| transaction_ref | `Transaction Id` | **the join key** — matches the bill's flat `rrn` 192/192 |
| terminal | `TID` | matches the bill's flat `tid` 192/192 |
| date | `Transaction Date` | `DD-MM-YYYY HH:MM:SS`; the file spans **490 July + 83 August** rows, so filter on this, never on the `July'26` folder name. **The time part is real and varies per row** (confirmed 2026-09-29, e.g. `21:53:01`, `21:49:34`) — unlike Axis's placeholder — stored separately as `transaction_time`, 100% non-null across all 1,071 rows checked |
| amount | `Transaction Amount` | |
| status | `Transaction Status` | **mixed case** — `Success` 532 / `SUCCESS` 37 / `Refunded` 4; compare case-insensitively |
| mid | `MID` | `BFL…` format; does **not** match the bill's `mid` — see §6.2 |
| net | `Net Amount` · charges `Total MDR` | for R2 later |

- `RRN` and `Auth Code` are the literal string `'NA'` on every row — never use them.
- Store for a matched bill comes from the bill's `STORE_NAME`. A Bajaj
  `TID → store` master is needed **only** for Bajaj gateway rows with no bill
  behind them (~39 on July).
- `settlement_cycles.bajaj.cycle_days` is still `null` — no document states it.
  R1 doesn't need it; R2 will. It stays a known gap, never a guess.

### Store master (`.xlsx`, header on row 1) — SUPPLIED 2026-09-10

`SR.No · CURRENTLY ACTICVE · SITES NAMES · Store Code` — 214 rows.

| Logical | Source | Note |
|---|---|---|
| store_name | `SITES NAMES` | the full name, e.g. `CITYKART STORES PVT. LTD. (BALLIA-BIA)` — this is what a Tally Cost Centre carries |
| store | `Store Code` | the short code, e.g. `BIA` — what POS, PTC and every gateway report carry |

**This replaces the provisional list derived from the POS dump.** The earlier
note that "no store master exists" is superseded. 213 distinct codes across
214 names: `MCR` appears twice (`(MADHEPURA-2)` and `(MADHEPURA-2-MCR)`),
which is harmless because the direction we resolve is name → code. Every one
of the 190 POS stores and 189 PTC stores resolves against it; 23 master codes
have no July POS activity.

`CURRENTLY ACTICVE` (the file's own spelling) duplicates `Store Code` on all
214 rows and is not read.

**Name matching is EXACT after normalising** — upper-case, trim, collapse
repeated whitespace — and nothing more. It is **not** fuzzy matching: nothing
is scored, ranked or guessed, and a name that fails to match is reported by
name (rule 5). Normalising is not cosmetic: matching raw strings resolves
**154** of 214 cost centres, normalising resolves **193**, and all 39 gained
differ by case alone. `masters.NORMALISED_KEYS` holds which (master, field)
pairs use it; only the store name does.

Note what normalising deliberately does NOT do: `( BALLIA-BIA)` does not match
`(BALLIA-BIA)`, because a single inner space is not a repeated one. Removing
spaces adjacent to punctuation would be a step toward fuzzy matching.

### Opening cash balance (`.xlsx`, header on **row 5**) — SUPPLIED 2026-09-10

A Tally `#Ledger Summary - Cost Centre#` export, ledger `Cash Drawer`.
`Level 1 · Level 2 · Level 3 · Level 4 · Ledger · Sub-Ledger · Cost Centre ·
Opening · Debit · Credit · Closing`

- Rows 1–4 are a title, the period, and a filter line. **Row 5 is the header.**
- The `Level*` and `Ledger` columns are populated on the first data row only
  and blank thereafter — do not require them per row.
- The file ends with a `Grand Total :` row (in `Sub-Ledger`, with no Cost
  Centre) and two print footers. Requiring a Cost Centre drops all three
  without matching on their text.
- **The as-at date comes from the file's own `Period:` line**, not from a
  hardcoded date and not from the filename (rule 8).
- `Cost Centre` is the store's FULL name, so the store master above is a
  prerequisite for reading this file at all.
- "Balances shown in '()' are credit balances" — the negative convention.

Measured: 214 cost centres, **193 mapped**, **21 unmapped and every one of the
21 has an opening of exactly 0.00**, so the mapped total equals the file's own
Grand Total of **22,808,219.46** with a difference of 0.00. The 21 are dormant
or non-CKSPL sites (three `CITYKART RETAIL PVT. LTD.` entries among them).

### Pickup code master
`PICKCODE → CODE`. **The "must look like `AX#####`" assumption is dropped — it
was wrong.** On the supplied file, 44 of 210 codes are plain 3-letter store
codes instead, and every one of them resolves correctly. **Flag only a code
that fails to resolve against the master — never flag one for merely looking
unusual.**

Effective-dating is not present in the supplied file (it's a flat, undated
list) and is not expected to ever be added at the source — see §6.12, this is
the loader's responsibility, not the file's.

### Pickup location master — the pickup code is not always unique (added 2026-09-12)

**Defect A:** APM and MMR share one pickup code, `AX31918`. The CMS report
credits both under it, distinguishable only by the file's own `LOCAT`
(column D — not read at all before this) and `LOCATIONNAME` (column E)
columns. The `pickup_code` master above stays a strict one-to-one map and is
untouched; a **new** master, `pickup_location`, sourced from
`5a. Pickup location master/PICKUP LOCATION MASTER.xlsx` (`PICKCODE,
LOCATION, CODE`), carries the disambiguation and today holds exactly two
rows: `AX31918+ARRAH → APM`, `AX31918+MADHB → MMR`. That correspondence was
confirmed against the store master's own site names (`ARRAH-APM`,
`Madhubani-2-MMR`), not guessed.

**Deliberately its own folder, not a subfolder of `5.CMS DEPOSIT MASTER`.**
Source discovery is depth-1 from the configured path and merges root +
subfolder files into one candidate list (§4) — nesting the new file under
the existing pickup-master folder would make each master's "most recently
modified" file-selection able to pick up the *other* master's file by
mistake. Two sibling top-level folders keep them from ever colliding.

**`masters.resolve_pickup_store()`** is the resolver `ingest_axis_cms` now
calls, replacing a bare `resolve_store(..., "pickup_code", ...)` call for
this one source: a pickup code NOT in the `pickup_location` master resolves
on the code alone, exactly as before — nothing changes for the other ~209
codes. A code that IS registered there consults `LOCAT`; a blank or
unrecognised location on a registered code is `location_unresolved`, never a
guess at either candidate. The mechanism is generic on purpose — the
operator's own instruction was "if the agency books another store under a
shared code we are not rebuilding this" — so a future case needs only a new
row in the small master, not a code change.

**Pre-flight check, `masters.pickup_codes_needing_location_registration()`**
(CLAUDE.md §6.11): every CMS ingest scans for a pickup code seen against more
than one distinct `LOCAT` value that the `pickup_location` master does not
yet know about, and reports it in the run's notes rather than silently
resolving on the code alone. This is the check that would have caught
defect A a month earlier than one row's arithmetic happening to look odd.

**Full July, re-resolved on pickup + location:** `AX31918` carries 44 COL
rows, ₹48,64,108 total — **19 rows / ₹19,33,458 now resolve to APM** (LOCAT
`ARRAH`, previously credited to MMR) and **25 rows / ₹29,30,650 to MMR**
(LOCAT `MADHB`, down from the previous ₹48,64,108 by exactly the amount that
moved to APM). 4,487 of 4,593 rows resolve — unchanged from before the fix,
since every AX31918 row already resolved to *a* store; only which store
changed for the 19 ARRAH rows. `report.unmapped` is empty and no
pre-flight note fires, since the one code that needs location is the one
already registered.

**A second, DIFFERENT pattern was found and is still open, on BLA/BIA
specifically — not the same defect, and not fixed by the above.** BLA
(`AX28945`) and BIA (`AX28944`) already have separate, correctly-registered
pickup codes; nothing about defect A touches them. But on 8 July dates
(02, 06, 15, 16, 17, 20, 21, 22), **BIA's own code `AX28944` carries TWO
`SLIPAMOUNT` rows instead of the usual one — one BIA's own size, the other
matching what would otherwise be BLA's slip for that date — while BLA's code
`AX28945` carries no row at all that day.** Confirmed still present after
the defect-A fix (BLA/BIA's resolution is completely untouched by it, so
this cannot be an APM/MMR artifact). And critically, **the `LOCAT` value on
both rows is identical** (`BALLP`/`BALLIA`, the same as BLA's and BIA's
usual location) — since BLA and BIA sit in the same town, this case has NO
location variance for the pre-flight check to catch; it can only be seen by
a slip landing under the wrong CODE, not a location mismatch. Do not extend
the `pickup_location` master or change anything for BLA/BIA until this is
understood — a location-based split here would silently misattribute rather
than correct, since location cannot tell the two apart to begin with.

### MID / TID master
Two separate sheets in one file, not one combined master:
`Axis mid wise store` (`MID, Store Code`) and `Paytm Mid wise store code`
(`MID, Store Code`). No `TERM_ID` column in either sheet (not needed — see the
Axis resolver above) and no Bajaj MID sheet at all.

Same versioning responsibility as the pickup code master — see §6.12.

### Tally cash balance (reference only)
`Cost Centre · Store code · Opening · Debit · Credit · <n> Closing`

---

## 6. Business rules

### 6.1 MOP classification

| MOP | Class | Reconciled in |
|---|---|---|
| `Cash` | cash | R3a |
| `PAYTM EDC` | digital | R1, R2 |
| `PaytmDQR` | digital | R1, R2 |
| `PAYTM DQR MOREFUN` | digital | R1, R2 |
| `EDC OTHER` | digital_noref | R1 (special), R2 |
| `EDC OTHER AXIS` | digital_noref | R1 (special), R2 |
| `Bajajpay UPI` / `Bajajpay Card` | digital | R1, R2 |
| `Credit Note Issued` / `Credit Note Received` / `Gift Voucher` | non_settling | neither — excluded, reported for information |

Held in config, not code. An unknown MOP stops that store-day and is reported.

**Control:** classes must sum to total collection for every store-day.

### 6.2 Reference extraction (R1)

Three gateways, three different payload shapes. **Digital bill = Paytm + Axis +
Bajaj Pay.**

```
paytm_ref = TPEDCRESPONSE.body.acquirementId   (EDC)
          | TPEDCRESPONSE.body.txnId           (DQR / QR)

axis_ref  = TPEDCRESPONSE.body.retrievalReferenceNo
            ONLY when the response carries issuerMaskCardNo (card-conditional)

bajaj_ref = TPEDCRESPONSE.rrn                 (FLAT payload — no head/body)
```

Normalise: strip leading apostrophes and trailing `.0`. Treat as **blank**:
`""`, `0`, `nan`, `#N/A`, and `0x2a` (how Excel errors surface from `.xlsb`).

**`axis_ref` is card-conditional (confirmed 2026-08-29).** Extract
`retrievalReferenceNo` only when the response also carries
`issuerMaskCardNo`; otherwise leave `axis_ref` blank. On the 20-Jul file, only
**821** of 12,424 `PAYTM EDC` rows carry a card indicator, yet
`retrievalReferenceNo` is populated on all 12,424 — so the unconditional rule
would attach an Axis reference to 11,603 UPI/wallet-on-EDC transactions that
Axis never processed, and R1 would then hunt for them in a 453-row Axis
report. This is the user's own workbook rule and is the source of truth.

**Bajaj payload is a flat JSON with no `head`/`body` envelope** (confirmed
2026-09-02 on the July dump). The bill-side key is lowercase `rrn`, and it
joins to the Bajaj settlement column **`Transaction Id`** — verified 192/192
on Sheet1's Bajaj bill lines. Traps:
- The Bajaj settlement file's own `RRN` column is the literal string `'NA'` on
  every non-null row (1 distinct value) — never join on it.
- The bill's `mid` (`0050001A0037272`) does **not** match the settlement `MID`
  (`BFL0000006850987`); the working terminal link is `tid` → `TID` (192/192).
- Bajaj `card_scheme` / `txn_type` read `UPI` / `SALE-UPI` even for rows the
  POS calls `Bajajpay UPI`, so the UPI-vs-card split for Bajaj comes from the
  bill's MOP, never from a card indicator.
- Store for a *matched* Bajaj bill comes from the bill's `STORE_NAME`. A Bajaj
  `TID → store` master is needed only for Bajaj gateway rows with no bill
  behind them.

### 6.3 Matching tiers (R1)

**A bill needs ONE successful gateway match — not a match for every reference it
carries. An unresolved reference is not, by itself, an exception.**

This is the governing rule of R1 and must not be weakened. A bill can carry a
Paytm reference *and* an Axis reference (and the payload may carry a stray RRN
even when Axis never touched the transaction — see §6.2). The bill is
**matched** the moment any one of its references resolves to a successful
gateway transaction. Only a bill with **no** resolving reference is an
exception. Reporting one exception per unresolved reference would generate
thousands of false positives — 11,603 on the 20-Jul file alone — and would
also fire the first time a genuine card reference legitimately fails while the
Paytm reference matched fine.

Per-reference resolution results are still recorded per bill for
traceability, and an unresolved reference on an otherwise-matched bill is
reported as information, never as a difference.

**Tier 1 — reference match.** Bill `paytm_ref` → Paytm `transaction_id`; bill
`axis_ref` → Axis `RRN`; bill `bajaj_ref` → Bajaj `Transaction Id`. Amount
exactly equal, `status=SUCCESS` (compare case-insensitively — the Bajaj file
mixes `Success`/`SUCCESS`), `transaction_type=ACQUIRING`. **No business-date
restriction (revised 2026-09-13 — a gateway reference is globally unique):**
the reference is searched across the ENTIRE loaded transaction table, not
matched only against the bill's own date. When the matched transaction's own
date differs from the bill's, the bill line records `date_diff_days` and a
`match_note` (*"reference matched, txn dated N day(s) after bill"*) — never
silently accepted as same-day when it was not, and never an exception either:
the money is received. Revenue recognition still stays on the bill's own
business date (rule 6); only the matching search is date-agnostic. See §6.9b
"The gateway search has NO date restriction at all" for the incident that
forced this (four July bills, one with a 7-day gap, that a same-date-only
and later a ±1-day-window search both missed). A matched transaction is
**consumed** and cannot match again.

**Non-ACQUIRING gateway rows — linked to their original, not to a bill**
(confirmed 2026-09-02). Every `REFUND`, `CHARGEBACK`, `CHARGEBACK_REVERSAL`
and `REVERSAL` row carries `reference_transaction_id` pointing at the original
sale. Link it to that original:
- original present in the loaded range → link it, inherit the original's
  `payment_mode` and store, **no exception**. (All 13 July chargebacks resolve
  this way, which is also why their blank `payment_mode` needs no special
  routing branch.)
- original absent → **exception**, naming the missing original id and its date.
  Expected on July: 8 refunds + 2 chargeback-reversals = 10 exceptions.

**Date-range filtering runs AFTER this linkage check.** Two July
chargeback-reversals carry a `transaction_date` of 21-May and 28-May while
their `transaction_id` encodes July — filtering by date first would drop them
before they could be flagged.

**Tier 2 — EDC OTHER only.** Never applied to a MOP that should carry a reference.

```
candidates = gateway transactions where
    store == bill store
    amount == bill amount        (exact)
    txn date == business date
    NOT consumed by Tier 1
exactly one  -> match, status "Matched (EDC OTHER)"
more than one -> "Ambiguous", rank by |bill time − txn time|, operator decides
none          -> open exception
```

Time **never decides** a match. It only orders candidates for the operator.
This matters: 1,664 combinations of store+date+amount are shared by more than one
bill on a single day.

**Reverse direction:** every unconsumed successful transaction is an unmapped
transaction — money with no bill behind it.

### 6.4 Settlement routing (R2)

`payment_mode == DEBIT_CARD | CREDIT_CARD` → **Axis** settlement.
Everything else → **Paytm** settlement. Bajaj MOPs → Bajaj settlement.

Three levels, each must tie:
1. transaction → settlement line. Expected credit = gross − commission − GST
   (Paytm) or GROSS_AMT − MDR − GST (Axis)
2. settlement lines → payout, grouped by `payout_id` + UTR
3. payout → bank statement credit, matched on UTR then value date + amount

### 6.5 Due dates

Cycle is **T+1** for Paytm and Axis, **T+0 or T+1** for CMS (per store, from the
master). Due date = **first working day after** the transaction or deposit date,
skipping weekends and public holidays. A Saturday CMS deposit credits Monday.

Statuses: `settled` · `settled_late` · `awaiting` (not an exception) · `short` ·
`excess` · `not_settled`.

### 6.6 R3a — store cash

```
closing(D) = opening(D) + cash_sale(D) + net_ptc(D) − cms_deposit(D)
opening(1st) = the opening balance file
opening(D)   = closing(D−1)
```
- `net_ptc` is **one netted column** — no income/expense split in the output.
  The split is reported for information only.
- Cashier short/excess stays **inside** the reconciliation (shortage = expense,
  excess = income) and is also reported separately as a metric
- **Cash received is NOT part of this equation.** It is a Head Office figure — see R3b

**`net_ptc` EXCLUDES the deposit vouchers.** The deposit is subtracted by its
own term, so counting those rows in `net_ptc` as well subtracts every deposit
twice — **Rs 48.99 Cr** on full July. Measured: all PTC rows net
−495,725,237.65; the deposit rows alone net −489,898,591.00; the non-deposit
rows net **−5,826,646.65**, and that last figure is `net_ptc`.

**A store present in the sale or PTC data but absent from the opening file is
seeded at zero and stays visible** — never dropped (rule 5). Three July stores
are in this position: BGP, KGT, RSG, with a combined closing of Rs 20,525.

#### The double-count test — POS no-bill rows ARE the PTC ledger

We exclude POS rows with no `BILLNO` because they are cash in/out, an expense
movement (§6.13). But PTC records expense movements too, so the two
populations must be checked for overlap before any R3a figure is trusted.
`r3a_store_cash.overlap_with_ptc()` is that test.

**They are the same movements.** Measured on the 20-Jul daily export — the only
supplied file whose no-`BILLNO` population is non-empty:

| | POS no-bill rows | PTC rows, same date |
|---|---|---|
| rows | **628** | **628** |
| total | **−31,423,089.00** | **−31,423,089.00** |
| stores whose net agrees | **185 of 185** | |
| matched on (store, exact amount) | **628 of 628** | 618 distinct keys on each side |

The POS export mirrors the PTC ledger. So the §6.13 exclusion is not only
"these are not sales" — **it is what prevents every petty-cash movement and
every deposit being counted twice.** Never re-admit those rows into a cash
figure while PTC is also loaded.

The monthly `.xlsb` carries **zero** such rows, so on full July the test has an
empty population and proves nothing. That is reported as "the population is
empty", never as a clean pass, and the run re-runs the test against the daily
export where it can actually fire.

#### What R3a can and cannot check

Opening cash is supplied **for 1 July only**, and there is no daily declared
cash balance. R3a is therefore a **computed cash book with no independent
confirmation**, and it must say so on screen
(`r3a_store_cash.NO_INDEPENDENT_CONFIRMATION`). **Do not invent a comparison we
do not have.** When a physical cash balance is supplied for a later date, that
becomes the two-sided check.

The checks that ARE available are internal-consistency checks — they can prove
the book is impossible, not that it is right:

| | Check | Why |
|---|---|---|
| a | **negative closing** on any store-day | a store cannot hold negative cash: a missing sale, an overstated deposit, or a mapping error. Always a defect |
| b | **closing above a threshold** | cash accumulating and not being deposited. Configurable — `r3a.high_cash_threshold`, started at Rs 2,00,000 |
| c | **cash sales but no deposit voucher for several consecutive days** | see the working-day caveat below |
| d | **the estate identity**, exactly | sum(opening) + sum(cash sale) + sum(net PTC) − sum(deposits) = sum(closing). Nil tolerance |
| e | **the carry-forward chain unbroken** | opening(D) must equal closing(D−1) for every store-date after the first |

**Check (c) must be working-day aware, or it blames the store for the agency's
cycle.** CMS pickup follows the working-day pattern of §6.5: the three July
Sundays (05, 12, 26) have **zero** estate-wide deposits, Saturdays run at about
40% of a weekday, and Mondays at double. Counting raw calendar days flagged
**88 runs across 80 stores** — mostly every store, every weekend.
`agency_non_collection_dates()` derives the dates on which the agency collected
from **nobody** and excludes them: such a day neither breaks a run nor counts
towards its length. That leaves **11 runs across 5 stores**, which is the real
signal. The dates are derived from the data, not from a weekday rule, because
no holiday calendar has been supplied and a public holiday looks identical.

### 6.7 R3b — CMS deposit

Grain: one row per **pickup** (pickup code + deposit date + slip no).

```
receivable = deposits (PTC "Cash/CMS Deposit") − credits (CMS SLIPAMOUNT)
```

CMS `TYPE` handling:

| TYPE | Treatment |
|---|---|
| `COL` | credit |
| `ADJ.CR.` | credit — agency adjusting an earlier excess, applied to its own DEPDATE/CRDATE |
| `ADJ.DB.` | **negative credit** — agency recovering an earlier excess settlement |
| `ZPO` | excluded — but listed on the run summary so it is a decision, not an omission |

Statuses: `credited` · `credited_late` · `in_transit` · `not_credited` ·
`credit_without_deposit` · `deposit_without_slip`.

**`credit_without_deposit` is higher severity than `not_credited`** — cash the
agency collected that the store never booked.

### 6.8 Duplicate bills

Bill numbers are always unique, but a store sometimes bills the same customer twice.
The duplicate has no gateway transaction, so it surfaces as an unmatched bill.
IT deletion takes 7–15 days.

An unmatched digital bill sharing store + business date + amount with another bill is
flagged `suspected_duplicate_bill` **on the first run**, carries an IT ticket
reference and an age, and auto-resolves when the bill leaves the dump.

### 6.9 Exception keys

Must be deterministic so remarks survive re-runs:

```
bill exception        business_date | store | bill_no | mop
unmapped txn          gateway | transaction_id
settlement exception  gateway | transaction_id
cms pickup            pickup_code | deposit_date | slip_no
cash difference       business_date | store
master data gap       source | identifier
```

Lifecycle: `open` → `under_investigation` → `closed`, or `auto_resolved`.
If the amount changes while the key holds, flag `amount_revised` with both figures.

**Addendum, 2026-08-24 — new statuses from the data inventory:**
- `store_ambiguous` — a MID resolves to more than one store in the master. Key:
  `gateway | transaction_id`. See the Axis resolver in §5.
- `no_source_report` — a bill's MOP class has no source report available at all
  (Bajaj today). Key: same shape as a bill exception —
  `business_date | store | bill_no | mop`.
- `"store-wise dump not supplied"` is reported as its own named pre-flight
  condition (§6.11), not merged into a generic missing-file message.

**Addendum, 2026-09-02 — non-ACQUIRING linkage and the sale universe:**
- `original_txn_not_found` — a `REFUND` / `CHARGEBACK` / `CHARGEBACK_REVERSAL` /
  `REVERSAL` row whose `reference_transaction_id` is not in the loaded range
  (§6.3). Key: `gateway | transaction_id`. Carries the missing original's id
  and date.
- `row_without_bill_no` — reported as its own named load-report line with count
  and total **per MOP** (§6.13), not as an exception row; excluded from the sale
  universe but never invisible.

### 6.9a The two risk figures are never added together

**"Open exceptions, Rs X at risk" over all exception types is a
misstatement.** Added 2026-09-10 after the tile on the landing page reported
850 exceptions and ₹6.41 L "at risk", which summed two situations that point
in opposite directions:

| Group | Types | What it is |
|---|---|---|
| **Amount not received** | `bill_not_matched`, `ambiguous_edc_other`, `no_source_report` | A bill was raised and no payment was found. **Money we did NOT get — the exposure.** |
| **Received, not billed** | `unmapped_gateway_transaction` | A successful payment has no bill behind it. Money we DID get but never recorded — an accounting gap, not a loss. |
| **Neither** | `original_txn_not_found` | A reversal whose original sale is outside the loaded range. Reported on its own; never folded into either. |

Measured on full July, run #1: not received **205 bills / ₹2,29,238** ·
received-not-billed **635 transactions / ₹3,90,651.55** · neither **10 /
₹21,533**. The three sum to the 850 / ₹6,41,422.55 that the single tile used
to show — which is why that total was meaningless.

Only **amount not received** carries a red or amber treatment. Received-not-billed
is neutral. `queries.risk_split()` is the only sanctioned way to read these,
and `verdict_sentence()` must never emit a combined "at risk" figure.

The **Exceptions** page header carried the same misstatement and was corrected
on 2026-09-11: it now reads "N exception(s) · Rs X not received · Rs Y
received, not billed" instead of one "Rs Z at risk" total.

**The Show dropdown was simplified to Open / Under investigation (2026-09-12)
— the page is a working list, it should show work, not history.** It used to
offer "Open only, Everything, Closed, Auto-resolved" — four options where two
were history, not work. Removed **Everything**, **Closed** and
**Auto-resolved** from the dropdown entirely; the two survivors, **Open** and
**Under investigation**, are each their own exact status filter now (the old
"Open only" silently combined both into one bucket). Closed and
auto-resolved items are never made unreachable — they move into a collapsed
**"Closed and auto-resolved"** expander at the very bottom of the page, with
its own count and its own export, because that is the audit record and
somebody will ask for it. Confirmed live: closing bill `SA62/024932/0726`'s
exception via a manual match (above) made it disappear from the "Open"
working list and appear, correctly, inside that bottom expander.

**Amount not received is broken down by reason, one reason per bill so the
rows never overlap** (`queries.unmatched_by_reason()`). Precedence, and why it
is in this order:

1. **the bill carries a gateway reference** → `reference_present_txn_not_found`.
   Genuine exposure; chase the gateway. This must outrank the duplicate test —
   otherwise the 5 Paytm bills whose transaction exists on the following day
   (§6.3 same-date rule) hide inside "probably not a loss".
2. **Tier 2 found more than one candidate** → `ambiguous_multiple_candidates`.
3. **no reference, but an identical bill on the same store+date+amount DID
   match** → `suspected_duplicate_bill` (§6.8). Probably not a loss, IT
   deletion pending.
4. **no reference and no matched twin** → `no_reference_at_pos`. A capture
   failure at the till; needs the store, not the gateway.
5. `amount_mismatch` — only the difference is at risk, not the whole bill.

July figures under this breakdown: reference present, txn not found **17 /
₹15,632** · ambiguous **11 / ₹3,183** · no reference at POS **131 / ₹1,91,469**
· suspected duplicate **46 / ₹18,954** = 205 / ₹2,29,238. A duplicate awaiting
IT deletion and a genuinely missing payment must never sit inside the same
number again.

### 6.9b Manual matching — a human overlay, never an edit (2026-09-11)

**A manual match is an OVERLAY.** It lives in `manual_matches`, keyed by the
bill's deterministic key (§6.9). The engine runs untouched and writes
`r1_bill_lines` exactly as it always did; the overlay is applied afterwards by
`manual_match.apply_overlay()`. That is what makes rule 2 safe here — a re-run
rebuilds the engine's rows from scratch and the overlay survives because
nothing in the engine ever writes to it. **A test asserts that
`r1_bill_lines` reads exactly as the engine left it after a manual match.**

#### The rule that matters most

**One transaction can back at most one bill.** Assigning the same transaction
to two bills double-counts revenue and hides a genuine shortfall. This is
enforced three times over, on purpose:

1. `validate()` reports it, naming the other bill, its store, date, amount and
   who matched it.
2. `record()` refuses without an explicit written override reason.
3. A **partial unique index** `ux_manual_txn_active` on
   (gateway, transaction_ref) WHERE lifecycle='active' refuses it in the
   database, as the backstop if a code path ever forgets to ask.

`ux_manual_bill_active` does the same for one bill holding two active matches.

#### Entry is INLINE, on the Transactions table (2026-09-11)

Matching happens by pasting a transaction id into the **Matched txn** cell of
an unmatched row and pressing enter. Every other column of that table is
read-only — the engine's result is never edited, the paste writes an overlay
row. Where this table lives has moved twice since (the pre-split
"Transactions" page → **Bill match** on 2026-09-12 → **Manual matches** on
2026-09-17, see §6.14's consolidation note) — as of 2026-09-17, **Manual
matches is both the entry point and the audit register**, on the same page,
the entry-side content stacked above the register.

**The gateway is never asked for.** `manual_match.identify_gateway()` searches
Paytm, Axis and Bajaj for the pasted id on that business date and reports
which one holds it; `validate_by_id()` then validates against that gateway and
carries `validation.gateway` so the UI can show what was found. The operator
already knows the id — making them also name the gateway is asking for
something the data can answer.

Three outcomes, and the middle one is not a choice to make:
- found in exactly one gateway → that is the gateway
- found in none → case (c)
- **found in more than one → reported, never resolved.** A Paytm
  `transaction_id` colliding with an Axis `RRN` would mean picking one
  silently, and picking silently is what rule 5 forbids.

A case (c) record stores `gateway = "unknown"` rather than null, so the
register never implies a gateway confirmed something it does not have.

**Case (a) is accepted on the paste alone** — a clean, unused, exact-amount,
same-store SUCCESS transaction is exactly the case where nothing else is
needed. Cases (b) and (c) always stop for a decision, inline under the table:
(b) needs a written reason plus a ticked confirmation, (c) offers only "save
as asserted, unverified".

#### The three validation cases

Every submission is validated **before** anything is written, and the case is
stored rather than recomputed — it records what was true and what the operator
was shown at the moment of the decision.

| Case | Condition | Outcome |
|---|---|---|
| **(a)** | found · amount equal · same store · SUCCESS/ACQUIRING · unused | accepted, status `matched_manual` |
| **(b)** | found but amount, store or status differs, **or the transaction is already used** | **never accepted on a single click** — the problems are listed, an override reason is mandatory, and a confirmation box must be ticked |
| **(c)** | no such transaction in that date's gateway data | recorded only as `asserted_unverified`, a distinct status; the bill **stays an open item** |

#### Never blend manual and automatic

`queries.match_split()` is the only sanctioned reader, and it keeps three
figures apart: **matched by system**, **matched manually**, **asserted
unverified**. Only the system rate is comparable across runs, because it is the
only one no human decision can move.

**An assertion is excluded from every rate.** `effective_matched` is
system-OR-manual and deliberately excludes asserted: an assertion with no
transaction behind it is an open item, not a match. A single blended
"99.99% matched" that counts human assertions is a number that flatters
itself, and `Summary` shows the three separately for exactly that reason.

#### Both sides move together

On acceptance: the bill leaves the unmatched list, the transaction is reported
as consumed by `manual_match.consumed_transactions()` so the engine can never
also consume it, and the bill's exception is closed with reason code
`matched manually`. **The remark history is untouched** (rule 4) — tested.

#### Lifecycle, and the three conflict cases on re-run

`manual_match.reconcile_after_run()` runs as its own stage in every run
(`timings.manual_overlay`) and reports all three outcomes; none is silent:

| Lifecycle | When | What happens |
|---|---|---|
| `active` | in force | the only state that moves a figure |
| `reversed` | a person undid it | the bill returns to unmatched, the transaction is released, **the record is kept** |
| `superseded` | the engine later matched that bill to the **same** transaction | redundant, marked, kept |
| `conflicted` | the engine matched it to a **different** transaction | **both are kept and neither is overwritten** — a person resolves it. Logged as a warning on the run |
| `orphaned` | the bill has left the dump (IT deleted a duplicate) | the transaction is released, the record is kept |

Every state change appends a `manual_match_events` row. **Append-only: a
reversal is a new row, never an update that erases what was there.**

#### The gateway search has NO date restriction at all (2026-09-13 — supersedes the WINDOW below)

**A gateway reference is globally unique.** The ±1-day window below (shipped
2026-09-12) was itself superseded a day later: four July bills
(`SA62/024925`, `/024926`, `/024927`, `SA12/045803/0726`) sat open with
their exact reference, store and amount present in the loaded Paytm data,
rejected only because the transaction's date differed from the bill's — 1
day for the first three, **7 days** for the fourth, which no window would
have caught. Money received is money received regardless of which day the
gateway happened to post it, so both **Tier 1** (`r1_bill_txn.match_tier1`)
and **manual matching** (`manual_match.gateway_pool_for_date`,
`validate_by_id`) now search the ENTIRE loaded transaction table for a
reference — no window, no restriction of any kind. `config.yaml`'s
`manual_match.match_window_days` and `manual_match._match_window_days()`
are gone; there is nothing left to configure.

**Never silent.** Tier 1 records `date_diff_days` (0 when the dates agree)
and, when non-zero, `match_note` — *"reference matched, txn dated N day(s)
after bill"* — on the bill line (`r1_bill_lines.date_diff_days` /
`match_note`, added as nullable columns via a lightweight `ALTER TABLE` in
`db.init_db()` so an existing database picks them up without a rebuild).
The **Bill match** page shows a count and drill-down of bills matched this
way — *"X bills matched to a transaction dated on a different day"* — as
information, never an exception: the money is received: **Revenue
recognition stays on the bill's own business date (rule 6) — only the
SEARCH is date-agnostic, never the date the bill is booked on.**

The manual-matching side of this is described in full below, unchanged in
its shape (still `date_lag_days` / `date_lag_note`, still surfaced inline
and in the bulk-upload preview) — only the search behind it widened from a
window to the whole table, and `gateway_pool_for_date()` now reads every
date-partitioned Parquet file for a gateway rather than a handful around one
date. `business_date` is still accepted by that function (existing callers
pass it) but no longer used to filter anything.

#### The gateway search was, for one day, a WINDOW — not the bill's date alone (2026-09-12, superseded above)

**Defect found on bill `SA62/024932/0726`** (9 July, KGN, PAYTM EDC, Rs
1,132): its transaction `20260710011020000282313546973201838` was rejected
as "not in the Paytm, Axis or Bajaj data loaded for 2026-07-09" — but the id
itself starts `20260710`, because it sits in the **10 July** file. A
late-evening sale is often posted by the gateway the following day, and the
validator was only ever looking at the bill's own date.

Fixed:

- `manual_match.gateway_pool_for_date(business_date, window_days=None)` now
  reads the per-gateway Parquet for **every date in
  `[business_date − window, business_date + window]`**, not one date —
  `window_days` defaults to `config.yaml`'s `manual_match.match_window_days`
  (**1**, either side); `window_days=0` reproduces the old exact-date-only
  behaviour, which is what the tests use to pin down the change.
- `find_transaction()` no longer filters by exact date at all — it trusts the
  pool it is handed is already scoped to the window, and matches on
  gateway + reference only. If the same reference somehow appears on two
  dates within the window, the row closest to the bill's date wins (a
  tiebreak, not a match rule).
- **Found on a different date within the window is still accepted, never
  silently.** `Validation` carries `date_lag_days` and `date_lag_note` (e.g.
  *"Found in the 10 July file; bill dated 09 July. Still accepted as a
  match…"*), populated in `validate()` and kept **out of `problems`** on
  purpose — a same-window date lag must not force case (b)/an override, but
  it must never be invisible either. Both the inline single-paste flow
  (`inline_match._clean_case` / `_discrepant_case`) and the bulk-upload
  preview (`UploadPreview.as_frame()`'s `date_lag_days` column, and the `why`
  list) surface it.
- Only absence from the **whole window** is reported as "not found" —
  `validate_by_id()`'s message now says so explicitly, naming how many days
  either side were checked.
- **A leading apostrophe is stripped before searching.** Pasting a
  transaction id out of Excel carries Excel's own text-forcing apostrophe
  (`'20260710011020000282313546973201838`) — `manual_match._clean_ref()`
  wraps the existing `io_.clean_ref()` (already used elsewhere for the same
  reason) and is applied to every reference id specifically, while `_clean()`
  stays a plain strip for every other field. `record()` also cleans the ref
  before storing it, so a recorded match never carries a stray quote either.

Re-tested against the real bill and the real transaction, read-only
(`validate_by_id()` only, never `record()`, so the test cannot mutate
production data): `case == CASE_CLEAN`, `gateway == "paytm"`,
`date_lag_days == 1`. Confirmed again live in the running app on the **Bill
match** page (below): searching the bill, pasting the id, the operator sees
"✓ Found in PAYTM… Amount and store agree" **and** "📅 Found in the 10 July
file; bill dated 09 July" before recording it.

#### Bulk upload — on Manual matches (moved to Bill match 2026-09-13, back here 2026-09-17 when Bill match was deleted)

**Template simplified 2026-09-13, still current.** Entry — inline paste or
bulk upload — lives on **Manual matches** as of 2026-09-17 (§6.14's
consolidation note); between 2026-09-13 and then it was on the
now-deleted Bill match page. Template columns, exactly: `bill_no,
business_date, store, transaction_id, remark` — **no mop, no reason_code
column any more**. `bill_no` alone identifies the bill (globally unique,
§5), so its mop is looked up from the system's own record rather than
re-typed, and the typed `business_date`/`store` are cross-checked against
that same record — a disagreement is rejected (rule 5: nothing is guessed,
and the operator's typed metadata must never silently override the bill's
actual one). Every accepted row records the fixed reason code
`manual_match.BULK_UPLOAD_REASON_CODE` (`"bulk_upload"`, in `config.yaml`'s
`reason_codes` list) with the operator's own free-text `remark` as its note
— there is no picked-reason column any more; the remark is the reason.
Everything else about it is unchanged: every row is validated first and
**nothing is applied** until the operator chooses; one upload is one
`manual_match_batches` row, so it reverses as a unit.

**Rejected, not warned** — because applying either would corrupt a figure
rather than merely be doubtful:
- the bill is not an unmatched bill in this run
- the typed `business_date`/`store` disagrees with the bill's own
- the transaction is already used
- **the same transaction appears twice in the same file** — no per-row check
  catches this; the double-count is between two rows of the one upload

A case (b) or (c) row is *warned*, and applying warned rows needs the same
written override reason as the single-row flow.

#### One §6.14 judgment worth knowing

The validator must look its id up in **the same data the engine searched**, so
it uses the engine's own pool shape (`gateway / txn_ref / business_date /
store / amount / status / txn_type` — `build_gateway_pool`, not a renamed
copy, or the two can disagree). For the UI,
`manual_match.gateway_pool_for_date()` reads **every loaded date's**
already-normalised Parquet for a gateway (2026-09-13 — no date restriction of
any kind; see "The gateway search has NO date restriction at all" above).
That is the "precomputed rows only" side of §6.14 rather than the raw-file
side; it is bounded by the warehouse's own size (one Parquet file per
gateway per date), not by a window, and it is a single operator action, not
a page load shared by everyone.

### 6.9b-ii Four changes, 2026-09-13 — cascading, Bill match's new pages, and suggested matches

**Item 1 — a manual match now cascades to every page immediately, not only
after a re-run.** Before this, `R1BillLine.matched` (the engine's own
column, never written by the overlay — §6.9b) was read directly, unadjusted,
by the store sheet (digital sale/receivable, D/F/G) and by the Digital sale
page's headline (`queries.verdict`) and by-store cut
(`not_received_by_store`/`not_received_bills`) — so a manually-matched bill
kept showing as unmatched on those three views until the next full run,
even though **Bill match**, **Transactions** and **Exceptions** already read
the overlay live and were correct. Fixed the same way throughout: `bill_no`
is globally unique (§5), so each of these now looks up the small set of
currently-**active** `manual_matches` (never the whole month of bill lines)
and folds in any bill among them that the engine itself still shows
unmatched. `store_sheet_source.build()` does this once, before
`digital_by_store_date()` runs, so **Summary (MOP RECO_Store Wise)**'s G
column and **Digital sale**'s headline and by-store table all move together,
correctly, the instant a match is recorded — confirmed with a unit test on
each of the three read paths (`test_verdict_cascades_a_manual_match_
immediately`, `test_not_received_by_store_cascades_a_manual_match_
immediately`, and the `store_sheet_source.build()` overlay lookup).
Exceptions already cascaded (`manual_match.record()` closes the bill's
exception directly); nothing changed there.

**Item 2 — the two-sided control is asserted and shown, not just implied.**
Both **Bill match** (`open + closed = total`) and **Transactions**
(`mapped + unmapped = total`) now carry an explicit pass/fail line under
their tile ribbon — a green confirmation when it holds, a red banner naming
the actual numbers when it does not, the same "quiet when clean, loud when
not" pattern the Summary page's controls already use.

**Item 3 — bulk upload moved to Bill match** (see above), with a new
"Suggested matches" section above it on the same page:
`manual_match.suggested_matches()` groups every OPEN bill with **no
gateway reference at all** by (store, amount) against unmapped
transactions in the same store and amount within `SUGGESTION_WINDOW_DAYS`
(2) days. **Time never decides a match (§6.3) — this only suggests, never
auto-applies.** When the bill count and candidate count in a group are
EQUAL, it is a **solvable set** (on instruction: "2 bills and 2
transactions... treat it as a solvable set, not ambiguous") — paired
greedily by closest date, with one "Accept all N" button that records every
pairing as its own `manual_match_batches` row (reversible as a unit, same as
any other batch). When the counts differ, every bill is listed against the
full candidate list instead, genuinely unresolved — exactly Tier 2's own
rule for one bill with more than one candidate, generalised to a group.

**Item 4 — the build stamp.** The sidebar now shows "Code loaded: <time>"
under the run stamp — `sys._mop_reco_started_at` (already tracked by
`selfcheck.mark_process_start()`, §9a) formatted for a human, so an operator
can see they are looking at a freshly-restarted process rather than an old
one silently serving cached modules.

### 6.9c The Summary headline row, and the store-wise cut (2026-09-11)

**Every tile in the headline row is a rupee amount.** The match rate used to
be a tile reading "99.99%", sitting beside three money figures it could not be
compared with. The value is now the headline and the rate is the subtitle:

```
MATCHED BY SYSTEM
Rs 54.14 Cr
99.97% - 659,945 of 660,150 lines
```

**A tile never truncates a figure — a cut-off number on a financial screen
is worse than no number (2026-09-11).** The Store sheet page originally put
six tiles in one `st.columns(6)` row using `st.metric()`, which clipped
every value ("Rs 2.9...", "-Rs 1...."). Fixed by: **at most four tiles per
row** — six goes on two rows of three, using the same `dashboard_common.tile()`
helper Summary already used rather than `st.metric()`; `tile()` now also sets
an explicit `min-width` on the value so it cannot be clipped by a narrow
column at any window size (checked at 1366px, not only a full-width
monitor); and every tile carries the **exact rupee figure** (full Indian
grouping, not the short form) both as the sub-line beneath the value and as
a native-title hover tooltip via `tile()`'s `tooltip` parameter — the short
form (`Rs 2.90 Cr`) is never the only figure available.

**Amount not received is cut by STORE, not by reason.** The by-reason table
answered "what kind of problem is this"; it did not answer **"which store do I
ring"**. `queries.not_received_by_store()` gives store · bills · amount ·
oldest bill date · age · action, biggest first. Age is measured from the
oldest unmatched bill, because a three-week-old problem is a different call
from yesterday's.

**The reason survives as a column on the bill rows inside each store**
(`not_received_bills()`), never as its own table — a suspected duplicate and a
genuinely missing payment still need telling apart. Both functions use the
same precedence as §6.9a, sharing `_reason_by_bill_key()`, so the two cuts can
never disagree.

Measured on full July: 205 bills across **86 stores**. KPH is the largest at
25 bills / ₹24,913, oldest 2026-07-02.

### 6.9c-ii Manual matches rebuilt: By bill / By transaction, and customer
refunds (2026-09-20)

**Two blocks removed from the Manual matches page, display only — the
underlying data is untouched.** "Suggested matches" (the solvable-set cards
and "Accept all" buttons) is gone from the page; `manual_match.
suggested_matches()`/`Suggestion` stay in the module, unused by the page, on
the judgment call that the user asked for the UI removed, not the function
purged — a future caller (or this page again) can still use it. The "Open
one" detail panel — the dropdown, the Bill/Amount/Transaction/Status/
Validation-case/Recorded-by block, the Lifecycle line, the Reason-code line,
the Audit-trail expander, and "Reverse this match" — is gone from the page
too. `manual_match_events`, `mm.reverse()` and `mm.reverse_batch()` are
unchanged; there is simply no button on this page that calls them any more.
The register table and its "Export all N manual matches" button are
unchanged and still the audit record.

**The literal string "nan" fix.** That panel was the source of a bare "nan"
line whenever a nullable field (note, override_reason, reason_code) was
read straight into an f-string. `formatting.blank(value)` is now the one
shared null-safety helper — `None`/`NaN`/`NaT` all render as an empty
string, never "nan"/"None"/"NaT" — applied to the register's `reason_code`
and `lifecycle_note` columns and to every nullable text column introduced
by this rework (Payment mode, Matched bill no, Remark, Customer name).

**The page is now a segmented toggle, two buttons, not tabs: By bill /
By transaction.** Entry is validate-then-batch-save, not immediate-apply:
pasting an id validates immediately (`manual_match.validate_by_id()` for
By bill, `manual_match.validate()` directly for By transaction — see below)
and the result — accept/reject and why — is tracked in a plain
`st.session_state` dict (`bybill_pending` / `bytxn_pending`, keyed on the
bill's deterministic key / `gateway|transaction_id`) rather than written
immediately. Nothing is recorded until **Save matches** is pressed, which
applies every currently-accepted pending row in one pass; a rejected row
stays on screen with its reason and is never applied. This is a genuinely
different UX shape from `inline_match.py`'s immediate-apply-on-paste flow
(still used nowhere now — kept in the tree, unused by this page) since the
operator needs to see and reconsider several pending decisions before
committing, not just one.

**By transaction's reverse-direction validator is the SAME `manual_match.
validate()` the forward direction already uses** — `manual_match.
find_bill_for_match(session, run_id, bill_no)` (new) looks up a bill by its
globally-unique `bill_no` (§5) with no date/store scope, reports if it is
already closed (naming the engine match or the active manual match), and
the page then calls `validate()` with that bill, the transaction's own
already-known gateway, and its id — literally the same two-sided check
(amount, store, status, already-used) the forward paste runs, just started
from the opposite side. No new validation logic was written for the
reverse direction on purpose — a second, subtly different implementation of
"is this a clean match" would be a second thing to keep in sync.

**"Resolution" is a status display plus a "Show" filter, not a
free-standing editable dropdown — a judgment call, stated here rather than
silently guessed.** The prompt described it as a dropdown (Matched to bill
/ Customer refund / Open) but also said "see section 3" for how a refund is
set, and section 3 requires more than a single dropdown value can carry — a
Zoho number and a customer name, validated against the store master and
the transaction's own store. Implementing "Customer refund" as a bare
dropdown option would either have to silently accept a refund with no Zoho
number (against rule 5) or pop a second form out of a dropdown selection,
which is not what "dropdown" ordinarily means. So: the **By transaction**
table shows the transaction's actual current resolution as a read-only
column, and a **Show** selectbox (Unmapped (open) / Matched to bill /
Refunded to customer / All) filters the table by it — "Matched bill no" is
only editable when viewing Unmapped (open); setting a refund goes through
the Refund upload below, per the prompt's own cross-reference.

**Customer refunds — a NEW `transaction_refunds` table** (`mop_reco.db.
TransactionRefund`), one row per transaction, natural-keyed on
(gateway, transaction_ref) via a unique index (`ux_refund_txn`) — refunds
survive every re-run because a gateway transaction's own identity never
changes between runs, so there is no reconcile-after-run case the way
`ManualMatch`'s lifecycle machinery needs one. `zoho_no` carries its own
unique index (`ux_refund_zoho`) so the same Zoho ticket can never be
attached to two transactions. Fields: `business_date`, `store`, `amount`
(captured from the transaction at upload time, not re-derived later),
`zoho_no`, `customer_name`, `refund_date`, `refund_status` (`"pending"` —
refundable, not yet paid — or `"released"` — paid back; the upload's own
`status` column maps `paid`/`released`/`refunded`/`completed` all to
`"released"`, `pending` to `"pending"`, and rejects anything else rather
than guessing), `uploaded_by`, `uploaded_at`.

**`mop_reco/refunds.py`** is the validation/apply module, the same
validate-everything-then-apply-all-or-nothing shape as `manual_match.py`'s
own bulk upload, enforcing every rule from the prompt verbatim: the
transaction must exist in the loaded gateway data (searched with no date
restriction, via `manual_match.identify_gateway`/`gateway_pool_for_date` —
the same global search a manual match uses); it must be currently unmapped
(refused, naming the bill, if the engine or an active manual match already
closed one against it); `store_code` must resolve in the store master
(`masters.get_rows_asof(session, "store", now)` — today's version, since an
upload is a today's-date action, not a re-run of a past business date);
`store_code` must agree with the transaction's own store, and a mismatch
shows **both**, never silently picked either way (rule 5); `zoho_no` must
be present and not already used, either by a prior refund or by another row
in the same file. **Applied as one all-or-nothing batch** — unlike the
manual-match bulk upload, which applies row-by-row and reports individual
failures, a refund batch either commits every accepted row or none, on the
prompt's explicit instruction ("applies — all or nothing, never
partially").

**A refund is never a bill match and is never counted as revenue.**
`gateway_txns.with_refunds()` (called after `with_bill_mapping()`) attaches
`resolution` — `"mapped"` / `"refunded"` / `"unmapped"` — and the refund's
own fields, joined on (gateway, txn_ref); a refunded transaction's `mapped`
column (bill-side) is untouched by this and stays whatever the bill-side
join already said (in practice always `False`, since only an unmapped
transaction can be refunded). `queries.transaction_resolution_summary()` is
the sanctioned reader for the new three-line TRANSACTIONS block (Mapped to
bill / Refunded to customer / Unmapped (open) / Total, shown on both
Summary's "Bills and transactions" strip and Manual matches' own tile
ribbon) — asserted to sum to the independently-queried total, MISMATCH
shown in red on disagreement, the same pattern every other two-sided
control in this app already follows. **Refund pending vs refund released
are two sub-counts inside the "Refunded" line, always shown together, never
merged into one figure** — `refund_pending_n`/`refund_released_n` split on
`refund_status`.

**Where a refunded amount sits in columns A-N: nowhere, and that is
already correct.** D (digital sale) and F (digital received) are both
built from `r1_bill_lines` (§6.9d) — bill-side aggregates. A transaction
eligible for a refund is, by definition, unmapped: no bill was ever raised
against it, so it was never counted in D to begin with, and F is the
matched subset of D. Cash columns H-N are a completely separate domain
(opening balance, PTC, CMS) that never reads a gateway transaction at all.
So a refund does not need to be moved OUT of digital received or a
receivable column — it was never IN one. The only place gateway-transaction
money surfaces on this app at all is the Bills-and-transactions
TRANSACTIONS strip, which the new three-line design accounts for
correctly. This was verified by reading `store_sheet_source.py`/
`store_sheet.py`'s own column formulas, not assumed.

**The Manual match bill report** (`queries.manual_match_bill_report()`) is
a new, plain, newest-first reading of every `ManualMatch` ever recorded
(every lifecycle state, since this is an audit report, not a work queue) —
Date/Store/Bill no/MOP/Amount/Gateway/Transaction id/Txn date/Lag/Matched
by/Matched at/Reason/Status, with its own export. `lag`/`txn_date` are read
back from `validation_detail`'s own `date_lag_days`, the identical
technique `_matched_bill_records()` already uses for the engine's own
Matched-bills table (§6.9c's 2026-09-19 revision) — never assumed to be 0.

**The Refund report** (`refunds.refund_records()`) is the refund-side
mirror: Refund date/Store/Gateway/Transaction id/Amount/Zoho no/Customer
name/Status/Uploaded by/Uploaded at, newest upload first, its own export,
transaction id as text.

### 6.9d The daily store-wise sale and cash sheet (2026-09-11)

The operator's own format, one row per store per business date, columns in
this exact order. `mop_reco.store_sheet` is the engine, `store_sheet_export`
the Excel, `store_sheet_source` the assembly and drill-down, and the dashboard
page reads the same engine so screen and file cannot disagree.

| | Column | Source |
|---|---|---|
| A | Date | |
| B | Store (short code) | |
| C | Total sale | **= D + E** |
| D | Digital sale | `r1_bill_lines` |
| E | Cash sale | POS bill-wise, MOP `Cash`, rows with a BILLNO only (§6.13) |
| F | Digital received | `r1_bill_lines` where matched |
| G | Digital receivable | **= D − F** |
| H | Opening balance | the balance file on the first date, then closing(D−1) |
| I | Cash deposit | PTC `Cash/CMS Deposit`, carried **POSITIVE** (corrected 2026-09-17 — see below) |
| J | Cash received | CMS credits on the **DEPOSIT** date, from **COL + ADJ.CR. + ADJ.DB.**, each via its own column (see rule 2 below); **ZPO ignored** |
| K | Cash receivable | **= I − J** — nil once a deposit is credited |
| L | Cash expense/income | net PTC **excluding** the deposit head |
| M | Closing balance | **= H + E − I + L** |
| N | Float amount | **= H − I** |

**Column I is stored POSITIVE, corrected 2026-09-17 — a deposit is a
positive amount of money.** It was carried negative before that date, with
K/M/N reading `I+J`, `H+E+I+L`, `H+I` to match; the operator's own
instruction was explicit that changing I's sign alone, without restating
those three formulas in the same change, would corrupt every figure on the
sheet, so all four moved together. `ptc_by_store_date()` now negates the
PTC deposit rows — which still arrive negative from the raw file, a PAYMENT
row leaving the till — into a positive `cash_deposit`; nothing about the
raw PTC ledger itself changed, only what the sheet stores. Verified against
two real 2026-07-01 rows before anything else was touched: ABD (H 99,437 ·
E 73,067 · I 87,937 · J 87,937 · L −320 → K 0 · M 84,247 · N 11,500) and AGT
(H 49,435 · E 56,241 · I 41,400 · J 41,400 · L −406 → K 0 · M 63,870 ·
N 8,035). Every place that used to negate or `.abs()` a stored
`cash_deposit` purely for display (the Summary tile, `float_tracker`'s
grid) has been removed — the stored value is already what a reader expects
to see. `store_sheet_export.py`'s Excel `FORMULAS` dict carries the
restated `=I{r}-J{r}`, `=H{r}+E{r}-I{r}+L{r}`, `=H{r}-I{r}` so the workbook
re-proves the same arithmetic.

**K's own polarity swapped with I's sign, and this changes what "positive"
and "negative" mean everywhere K is read.** A **positive** K is now cash in
transit — deposited, not yet credited (§ below, "in transit" was `K<0`
before, `K>0` now). A **negative** K is now Defect B — a credit exceeding
its deposit (`credit_exceeds_deposit_rows()` was `K>0`, is now `K<0`;
sorted worst-first is now ascending, not descending, since the worst case
is the most negative). `net_cash_receivable()` is unchanged in what it
computes — `max(0, sum(I) − sum(J))` — because summing I and J is linear
and gives the identical real rupee figure regardless of which sign
convention produced the per-row I and J; only the internal arithmetic
changed, never the business answer. The Rs 65,71,377 July figure in the
tile-ribbon history below is the same money under both conventions.

**Credit notes appear nowhere.** Total sale is digital plus cash only.

D and F both come from `r1_bill_lines`, which makes control (f) — "total D
equals the digital sale the dashboard reports" — true by construction rather
than by coincidence, and makes F exactly the matched subset of D rather than a
separately-derived figure that can drift.

#### The two rules that decide whether the sheet is correct

**1. Column J belongs to the DEPOSIT date, never the credit date.** Settlement
is T+0 for some stores and T+1 for others. A deposit made on 1 July and
credited on 2 July belongs to the 1 July row — that is the only way K means
"deposited and not yet received". Grouping on `CRDATE` instead would put K
non-nil on the deposit day and a phantom positive on the credit day, reading
as money received twice. Two tests assert the T+1 case lands on DEPDATE and
that both days show K = 0.

**A non-zero K at month end is real cash in transit and is never suppressed.**
Measured on July: 337 store-days across the month carry a non-zero K, but
**86 of them fall on 31 July alone — ₹65.11 L**, of which 85 are deposits with
no credit at all yet. That is the T+1 tail whose credit lands in August,
outside the loaded range. The page reports it as what it is.

**2. J is built from three types, each via its own source column — never one
blended SLIPAMOUNT figure. TYPE = ZPO is ignored entirely, on instruction.**
(Revised 2026-09-11; supersedes the earlier "COL only, others reported
separately" rule.)

| TYPE | Source column | Sign |
|---|---|---|
| `COL` | `SLIPAMOUNT` | + |
| `ADJ.CR.` | `ADJ` (column U) | + |
| `ADJ.DB.` | `RECOVEREDAMOUNT` (column X) | − (subtracted) |
| `ZPO` | — | ignored, not reported as a figure feeding J |

**`SLIPAMOUNT` on an `ADJ.CR.` row is the ORIGINAL slip being re-adjusted, not
the size of the adjustment** — confirmed against the raw July file: the three
`ADJ.CR.` rows carry `ADJ` 103 / 7 / 8 against `SLIPAMOUNT` 47,400 / 1,27,722 /
95,760 on the same rows. Using `SLIPAMOUNT` for `ADJ.CR.` would book the whole
original slip a second time. `ADJ.DB.` is the bank recovering an earlier
overcredit, so `RECOVEREDAMOUNT` reduces what the store is owed.

`cms_contribution_by_type()` reports each type's source column, row count and
value so the split is visible, never blended into one figure.
`excluded_credit_types()` now reports **only ZPO** — ADJ.CR. and ADJ.DB. feed
J directly via their own columns and are no longer "excluded".

July totals by type, all DEPDATEs, under this rule: **COL 4,484 rows /
₹49,30,47,618** · **ADJ.CR. 3 rows / ₹118** (from `ADJ`, not the ₹2,70,882 that
`SLIPAMOUNT` would have given) · **ADJ.DB. 0** · total contribution to J
**₹49,30,47,736**. ZPO (ignored) separately totals ₹49,30,47,736 — the same
figure to the rupee, a striking coincidence worth flagging but immaterial
since ZPO is excluded regardless of its size.

#### Two things the data does, that the sheet must not hide

**The 106 ZPO rows carry a single pseudo pickup code `AX38737`, a blank
LOCATIONNAME and no DEPDATE at all.** They can be attributed neither to a
store nor to a business date, so they are reported with both left explicitly
blank rather than forced onto a day they do not belong to (rule 6). This is
also why "4,487 of 4,487 pickup codes resolve" (§7, R3b readiness) is true of
the COL + ADJ.CR. + ADJ.DB. credit population (the types that feed J) and
**not** of all 4,593 rows — the 106 that do not resolve are exactly the ZPO
rows.

**The carry-forward always starts where the opening balance is KNOWN**, not at
the range's start. The balance file is as at 1 July, so asking for 20 July
alone still walks the cash book from 1 July and then shows only the 20 July
rows. Building 20 July directly gave **71 store-days with a negative closing**
— impossible, and caused entirely by seeding H with a three-week-old balance.
Carried forward properly, 20 July opens ABD at **159,361**, which is the §7
acceptance figure for 19 July's closing, and control (d) passes. The page says
in words that it is doing this.

#### Controls, all eight of which must hold and be reported

`a` C = D + E · `b` M = H + E − I + L · `c` opening(D) = closing(D−1) per
store · `d` no negative closing on any store-day · `e` the estate identity ·
`f` total D equals the dashboard's digital sale · `g` total E for 20 July =
₹1,29,41,046 · `h` the summary block equals the detail column by column.

Control (f) **fails loudly when no dashboard figure is passed** rather than
reporting a silent pass — an unverified control that reads as green is worse
than one that reads as red.

Measured on full July, every control holding: 6,076 store-days · 196 stores ·
opening ₹2,28,08,219.46 · total sale ₹1,03,10,08,097.79 (digital
₹54,16,30,226.21 + cash ₹48,93,77,871.79) · digital receivable ₹2,29,238 ·
deposits −₹48,98,98,591 · closing ₹1,64,60,853.60.

**A ninth, finer-grained check runs alongside the lettered eight
(2026-09-11):** `verify_date_rows_equal_store_sums()` asserts every
consolidated ALL-STORES date row equals the sum of that date's own store
rows, column by column. This is not the same claim as control (h), which
checks only the single grand TOTAL against the whole detail — a date could
disagree with its own stores while the whole-range total still happened to
balance, and (h) would not catch it. It is reported as
"(date=Σstores)" wherever the lettered controls are shown (the app page and
the export's Controls tab), deliberately not given a letter of its own so
the canonical a–h set is never renumbered.

#### The one on-screen table: by date, expandable to stores (2026-09-11)

**Summary 1 (by date) and Summary 2 (by store) are gone as separate
tables — merged into one.** Collapsed, the default: one row per business
date, all stores consolidated (`store = "ALL STORES"`), plus a TOTAL row.
Expanding a date splices that date's own store rows in beneath it, indented
(`store = "    " + code`, its own `date` left blank since it belongs to the
consolidated row above), in the exact same columns A..N — the collapsed and
expanded views read as one sheet, never two. "Expand all" / "Collapse all"
sit above the table; per-date control is a multiselect of which dates are
open, keyed to the active date range and store filter (the same
option-set-tied-key pattern as the store selector elsewhere in this app —
see §6.14's sidebar section — since a stale key here would show dates from
the wrong range). `store_sheet.build_grouped_table()` is the engine function;
the grand total stays a small separate block underneath, since it is not
"by date" and merging it in would have to repeat the TOTAL row from the
table above.

#### The Excel export: five tabs, not one (2026-09-11)

**One workbook, five tabs, so the operator can use whichever they need
without extracting sections from a single sheet:**

| Tab | Contents |
|---|---|
| 1 "By date" | one row per business date, all stores consolidated, plus TOTAL |
| 2 "By store and date" | the full detail — one row per store per date |
| 3 "By store" | one row per store for the whole range, plus a grand total |
| 4 "Excluded types" | ADJ.CR., ADJ.DB., ZPO — counts, values and whether each feeds J, plus the full J-contribution table for the whole figure in one place |
| 5 "Controls" | the 8 lettered controls plus the "(date=Σstores)" check, all in one place |

Every tab carries columns A..N in the same order, with the same live
formulas for C, G, K, M and N, and its own compact provenance line (run,
timestamp, range) — since any one tab may travel alone, each must still be
auditable on its own.

**Every TOTAL / grand-total row's SUM formulas point at the "By store and
date" tab directly, via a cross-sheet reference — never at another summary
tab.** This is the same principle the single-tab layout already held
(tracing a total must never depend on trusting a block in between), now
applied across tabs: `='By store and date'!D9:D6084` rather than a formula
that sums another summary tab's own rows. It is also what makes tab totals
provably agree rather than merely likely to: tab 1's TOTAL and tab 3's grand
total are the *same* SUM over the *same* detail range, just written on two
different sheets — they cannot quietly drift apart. Opening is the exception,
as before: the range's opening is the detail tab's FIRST row's opening
(`='By store and date'!H9`), never a sum, which would double-count the
carry-forward.

Indian grouping throughout via `#,##,##0.00`, which groups by two after the
first three digits.

#### The tile ribbon, and controls that are quiet when clean (2026-09-12, revised 2026-09-13)

**Seven tiles, never more than four in a row: TOTAL SALE, DIGITAL SALE, CASH
SALE, DIGITAL RECEIVABLE on row one; CASH DEPOSIT, CASH RECEIVABLE, CLOSING
BALANCE on row two.** All seven sit inside one shared, lightly-tinted panel
(`#f4f6f7`, a light neutral — never a strong colour) via
`st.container(key="tile-ribbon")` plus a scoped `.st-key-tile-ribbon` CSS
rule — the documented, stable hook Streamlit gives a keyed container, not a
guess at internal class names — so the row reads as one instrument panel
rather than floating cards.

**There is no "Excess credited" tile, on the operator's explicit instruction
— there is no such thing as excess credit in this business; the agency
never credits more than was deposited (2026-09-13).** This is the THIRD
design for this tile in as many revisions the same week, and worth reading
in order so a future change does not re-derive the same dead end:

1. **First shipped:** one tile, `sum(K)` over the whole range. Wrong — a
   store-day at -1,000 and another at +1,000 net to a silent zero, hiding
   either direction.
2. **Revised the same day:** two tiles, "Cash receivable" (`-K` summed over
   K<0 rows only, Rs 1,45,87,717 on July) and "Excess credited" (`K` summed
   over K>0 rows only, Rs 80,16,340). Better — nothing hidden — but wrong in
   a different way: a K>0 store-day is not a real balance at all. It is
   **Defect B** — a CMS credit posted against the wrong date, or a pickup
   code shared with another store crediting the wrong store (§5, "Pickup
   location master" — the same class of defect as the APM/MMR pickup-code
   split, confirmed 2026-09-13 to also explain 8 of BIA's July dates via
   BLA's deposits landing under BIA's code, and two further undocumented
   pairs found the same day). Giving it its own tile as "Excess credited"
   presented a data-quality problem as if it were a second kind of balance.
3. **Current (2026-09-13):** one tile only, **NET**:
   `cash_receivable = max(0, sum(I) - sum(J))` over the filtered range —
   `mop_reco.store_sheet.net_cash_receivable()`. On full July, all stores:
   **Rs 65,71,377** (matches an independent check of Rs 65,71,252 to within
   Rs 125). This is summing I and J FIRST and flooring LAST — **never** a sum
   of per-row values that were each floored first, which is exactly what
   design 2's "Cash receivable" tile did and why it overstated the range's
   actual receivable by the full Rs 80.16 L: `old_gross (Rs 1,45,87,717) -
   new_net (Rs 65,71,377) = Rs 80,16,340`, to the rupee, the same figure
   design 2 gave its own tile. Full diagnosis (which rows, why, three
   confirmed cross-store pairs, and how much of the Rs 80.16 L is timing-lag
   vs. misattribution vs. still unexplained): `docs/CASH_RECEIVABLE_
   DIAGNOSTIC.md`.

**Netting happens at whatever level is being displayed, never the whole
range then re-summed per row.** The tile nets over the filtered sheet; a
store-level total (if ever shown — today only the Excel "By store" tab)
would net over that store's own rows across the whole range, not sum each
day's already-floored value; a single store-date row's own net is trivially
its own `I − J`. `net_cash_receivable()` takes whatever frame represents
"the level" and does sum-then-floor itself, so getting this right does not
depend on the caller remembering the order.

**A negative-K row is never simply floored away (K's polarity swapped
2026-09-17 with column I's sign — a negative K is now the Defect B case,
where a positive K was before).** Every store-date where `cash_receivable <
0` is listed in a **"Data quality"** expander below the ribbon
(`store_sheet.credit_exceeds_deposit_rows()` +
`store_sheet_source.data_quality_rows()`, which attaches the CMS pickup
code(s) and slip number(s) that fed that day), labelled explicitly as a data
issue — *"These rows indicate a credit posted against the wrong date or a
shared pickup code, not money owed to the agency. Open item: Defect B (CMS
date attribution)."* — never as a balance, with its own CSV export. A one-line
caption above the expander (*"N store-day(s) show credits exceeding
deposits (Rs X) — under investigation, see Data quality below"*) appears
only when the list is non-empty. On full July: **63 store-days, Rs
80,16,340** — the same population and the same total design 2's "Excess
credited" tile showed (and unchanged by the 2026-09-17 sign correction,
since it is the same real money under either convention), correctly
labelled as an open item rather than a balance.

**No cash receivable figure displays negative anywhere on this page.** The
on-screen detail table (every store-date row, the date-consolidated ALL
STORES rows, and the grand total) shows `max(0, K)` for column K, never the
raw signed value — the same floor the tile applies, at whatever grain that
row represents. The "Cash in transit" section (in-transit tail,
`in_transit_tail()`) now excludes K<0 rows entirely — they are Defect B, not
cash in transit — and shows its K>0 population, already a positive
magnitude since 2026-09-17. **Column K in the detail table and every tab of
the Excel export are unaffected — still `I − J`, still signed** — none of
this touches the underlying sheet data or the export's live formulas, only
how this one page displays the figure.

**Digital sale and cash sale show their share of total sale, not the rupee
figure, as the sub-line** — e.g. "52.5% of total sale" / "47.5% of total
sale.** The two are computed as direct complements of each other
(`cash_pct = 100.0 - digital_pct`), not as two independently-rounded
percentages, which are not guaranteed to sum to exactly 100. The exact rupee
figure for both still lives in the hover tooltip — no figure is ever only
available in short form.

**Cash deposit displays positive** ("Rs 48.99 Cr", not "-Rs 48.99 Cr") —
and, since 2026-09-17, that is simply column I's own stored value, not a
display-side flip. Before that date I was stored negative and this section
described a display-only correction; now the sheet's own formulas
(`K = I − J`, `M = H + E − I + L`) are written for a positive I directly, so
the tile, the detail table and every tab of the Excel export all show the
same number with no separate positive/negative handling anywhere.

**Every control passing shows NOTHING — no success banner.** All 8 holding
is the normal case, and a banner repeating that on every load was noise. Only
a **failure** gets shown, as a red banner at the top naming which control
failed and by how much — the existing per-control detail string (e.g. "got
X, expected Y; difference Z") already carries the "by how much". The full
controls-in-full table and every run note (opening-balance date, unmapped
cost centres, the file's own Grand Total check) moved out of the page's top
and into a collapsed **"Data notes"** expander at the very bottom — still
there to find, no longer in the way of the figures every time the page loads
clean.

#### The seven-tile ribbon replaced by a particulars matrix, and MOP moved from four classes to six buckets (2026-09-18)

**The tile ribbon above is gone.** Total sale, Digital sale, Cash sale,
Digital receivable, Cash deposit, Cash receivable and Closing balance —
the whole subsection above this one — describes a tile ribbon that no
longer exists on Summary, on instruction. In its place: one table,
**Sale / % to total / Collected / Receivable**, for **Total sale / UPI /
Card / Total digital / Total cash**, plus **Gift Voucher Redeemed /
Credit Note Issued / Credit Note Received** as their own Total-sale-only
lines. `mop_reco.store_sheet_source.mop_particulars_matrix()` is the
engine; `mop_reco.mop_matrix.py` is the pure bucket-assignment module
(§8 — no DB, no Streamlit); Summary.py renders it via `st.dataframe` with
a pandas Styler for the two colour treatments the brief asked for
(Credit Note Issued in `--bad`, a non-zero Receivable tinted `--warn`) —
literal hex in the Styler, not `var(--warn)` (see §8.1's own note on why a
canvas `fillStyle` cannot resolve a CSS custom property).

**Six buckets, not four MOP classes.** `§6.1`'s `class` field (cash /
digital / digital_noref / non_settling) still governs R1/R2/R3a — nothing
there changed. A NEW, separate field, `config.yaml`'s `mop_classification.
*.bucket`, governs ONLY this matrix: `card`, `upi`, `cash`,
`gift_voucher`, `credit_note_issued`, `credit_note_received`. Every MOP
found in the data must carry a `bucket` or `mop_matrix.load_mop_buckets()`
raises, naming the MOP — the same "nothing is guessed" rule §6.1's own
class field already followed, applied to the new field too.

**The MOP inventory and the one ruling it needed: `docs/MOP_CLASSIFICATION.md`.**
Every MOP found in the July data, its row count, its total, and its
bucket — 10 of 11 MOPs classified with full confidence from the brief's
own rules; `EDC OTHER` and `EDC OTHER AXIS` (2,066 rows, Rs 18,97,679) could
not be — the "OTHER" in both names is the system's own admission that the
POS payload carried no usable reference at all (§6.3 Tier 2), so nothing in
the data says card or UPI. Reported as an unclassifiable OTHER bucket
rather than guessed into either, per the operator's own explicit
instruction; the operator ruled both are UPI-mode transactions, and
`config.yaml` now says so, with the reasoning recorded inline.

**Gift Voucher Redeemed is part of Total sale, self-collected, and its
Receivable is asserted at exactly 0 — never anything else.** The sale
data carries only one gift-voucher MOP, `Gift Voucher` (confirmed from
the data, not the differently-worded config comment) — Rs 42,500 over 15
rows in July. A voucher redemption settles a bill with no new money
arriving now or later, so it is treated as "collected" against its own
sale value the instant it is posted; **it never enters the R1 matching
population at all** — `r1_bill_lines` only ever holds `digital`/
`digital_noref` MOPs (confirmed: 0 Gift Voucher or Credit Note rows in
that table, for any run) — so a voucher-settled bill was never at risk of
becoming an exception bill, and 0 of them were found sitting in the open/
exception bills population when this shipped.

**Credit Note Issued and Credit Note Received have no "collection" event
of their own — a judgment call, stated rather than silently made.** Both
are `non_settling` under `class` (§6.1) and always have been: bill
adjustments, not new money. Unlike Gift Voucher, the brief gave no
explicit "receivable must be zero" instruction for either, so they are
added to Total sale's own Sale total but never to Collected — they flow
straight through into Total sale's own Receivable line (July: their net
is -Rs 4,771, small and explainable, not folded away). If this reading is
wrong, it is wrong in the open — see `mop_particulars_matrix()`'s own
docstring for the full reasoning.

**Cash's own Receivable is always exactly 0 — by definition, not by
accident.** The brief's own words: "Collected... for cash, banked or held
at store" — every cash sale is fully in hand the moment it happens, either
way, so Cash Collected = Cash Sale always. **This is a different concept
from the (removed) tile ribbon's old "Cash receivable" tile**, which
measured CMS deposit-vs-bank-credit lag (`net_cash_receivable()`, still
computed, still feeding the "whole-range totals" control below — nothing
about that calculation changed) — the two must never be confused just
because they once shared a name.

**Verified against the exact figures this file already records.** Total
digital (Card + UPI, including the ruled-UPI EDC OTHER/EDC OTHER AXIS) =
Rs 54,16,30,226.21, to the rupee the existing Digital sale (D) figure
above; Total digital Receivable = Rs 2,26,685.00, to the rupee the
existing Digital receivable (G) figure. Old Total sale (Rs
1,03,10,08,098.00) vs new Total sale (Rs 1,03,10,45,827.00) differs by
exactly Rs 37,729 — Gift Voucher (Rs 42,500) + Credit Note Received (Rs
2,68,54,512) + Credit Note Issued (-Rs 2,68,59,283) = Rs 37,729, to the
rupee. Nothing else moved.

**All three requested controls hold, asserted and shown, quiet only when
clean** — the same "loud when broken, silent when not" pattern every
other control in this app follows: (1) Total digital + Total cash + Gift
Voucher Redeemed + Credit Note Received + Credit Note Issued = Total
sale; (2) per column, Sale − Collected = Receivable; (3) Gift Voucher
Redeemed's own receivable = 0.

**A precomputed `mop_day_agg` table, one row per store per business date
per bucket** (`mop_reco.db.MopDayAgg`) — a MOP's own sale total never
changes with a manual match, so unlike `store_day_agg`'s F column this
table needs no live recomputation for its OWN figure; only "Collected" is
read live (see below). Written by `store_sheet_source.
persist_mop_day_agg()`, its own stage in `runs.run_reconciliation()`
(`persist_mop_day_agg`, alongside the existing `persist_store_day_agg`) —
`scripts/backfill_mop_day_agg.py [run_id]` brings an existing run forward
without a full re-run, the same shape `backfill_store_day_agg.py` already
established.

**"Collected" for Card/UPI is read live, at page-load time — never
stored — for the identical reason column F is live: a manual match must
move it with no re-run (§6.9b-ii item 1).** `_live_collected_by_bucket()`
is `_live_digital_received()`'s own system-matched + active-manual-match
cascade, narrowed by `mop` so a Card/UPI split is possible.

**A real performance bug, found and fixed before this shipped: one query
per MOP, not one query with `mop IN (...)`.** The first version ran a
single `mop IN (...)` query per bucket. SQLite's own query planner picked
a DIFFERENT index depending on how many values were in the list: 2 values
(Card) kept using `ix_r1_mop_date` and answered in ~265 ms; 5 values (UPI)
made the planner switch to `ix_r1_matched_date` instead — a full scan of
the month's ~660,000 matched rows, filtered by `mop` row-by-row after the
fact, measured at **3.9 seconds**, on its own more than doubling this
page's load time (`EXPLAIN QUERY PLAN` confirmed the different index for
each). Fixed by running one query PER MOP (each still using
`ix_r1_mop_date`, confirmed) and summing in Python — measured at 315 ms
for the identical UPI figure, the same number an order of magnitude
faster. `mop_particulars_matrix()`'s own total cost: ~585 ms, cheaper
than the existing F/digital_received live join it sits beside.

#### The store cash card beside "Store-wise detail" (2026-09-18, Part 2)

A five-cell card — Opening balance | Cash sale | Cash deposit | Expense &
income | Closing at store — sits to the right of the Store-wise detail
section's own toggle header, on the SAME row, regardless of whether the
section is expanded or collapsed (cheap enough — a handful of indexed
SUMs — that computing it every load costs nothing worth gating).
`store_sheet_source.store_cash_summary()` reads H, E, I, L, M straight
from the precomputed `store_day_agg`, never re-derived, so it can never
disagree with the detail table below it.

**Opening balance is the FIRST filtered date's own H, summed across
stores — never summed across dates.** H is a stock, not a flow; summing
it across many days for the same store would count the same
carried-forward money repeatedly. Cash sale / Cash deposit / Expense &
income ARE flows, summed across every store-day in the filter. Closing
at store is then `H(first) + ΣE − ΣI + ΣL`, which telescopes — via the
same carry-forward chain `H(d) = M(d−1)` already relies on — to exactly
the LAST filtered date's own M, summed across stores. That is what the
mismatch check compares against: "the same filter" in the operator's own
instruction means the grain the old Closing balance tile always used
(the last date, summed across stores), not a sum of M across every date
in the range, which would double-count the same carried balance.
Verified against full July: opening 2,28,08,219.46, cash sale
48,93,77,871.79, deposits 48,98,98,591.00, expense/income −58,26,646.65,
closing 16,460,853.60 — the exact §7 estate-identity acceptance figures,
and the mismatch check passes to the rupee.

Summary.py's own local `_section_toggle()` helper (not `dashboard_common`'s
— this one is page-specific, driving `store_detail`/`matched`/`exceptions`)
originally gained an optional `extra` render callback for this — a third
column on the section header's own row. **Superseded the next day (below)
— the card moved to its own full-width row and `extra` was removed again.**

#### Card vs UPI corrected: the matched transaction decides, never the MOP (2026-09-19)

**The matrix's first cut (above) split UPI vs Card from the POS MOP name
and was wrong** — it put Card at 74% of digital sale, not credible for
Indian retail and contradicted by the unmapped-transaction pool itself
(479 unmapped UPI vs 30 unmapped card). Root cause, confirmed against the
data: `"PAYTM EDC"` is one shared terminal that accepts BOTH card and UPI
(Axis TIDs are mapped onto Paytm machines for card acceptance) — the MOP
cannot say which one a given bill used. The cross-tab proves it directly:
that one MOP carries 17,774 Axis-card rows, 379 Paytm credit-card rows,
13,483 Paytm debit-card rows, and 444,782 Paytm-UPI rows, all under the
same three letters. Full data and the fix's own reasoning:
`docs/MOP_PAYMENT_MODE.md`.

**Fixed:** Card vs UPI is now decided PER MATCHED BILL LINE from the
gateway TRANSACTION's own identity — `mop_matrix.classify_digital_mode
(gateway, payment_mode, mop)` — never from `config.yaml`'s MOP bucket.
Axis → always `card` (settled card transactions only, by design, §5).
Paytm → from the transaction's own `payment_mode` field (`UPI` /
`DEBIT_CARD` / `CREDIT_CARD` — confirmed the only three values present).
Bajaj → from the bill's own MOP, the ONE deliberate exception: `"Bajajpay
Card"`/`"Bajajpay UPI"` are two distinct, separately-posted POS MOPs, not
a shared terminal like Paytm EDC — the field's own settlement data is
unreliable for this (§6.2), but the MOP itself is a fact here, not a
guess. Every digital MOP's `bucket` in `config.yaml` — Bajaj's two
included — is now a single `digital` value; no MOP anywhere carries
`bucket: card`/`bucket: upi` any more (rule 5's "nothing is guessed"
applied literally: the only place a card/upi decision is made is inside
`classify_digital_mode()`, once, tested).

**A digital bill line with no matched transaction has no known payment
mode and is never guessed into Card or UPI.** It is its own column,
**Unclassified**, added to the particulars matrix between Card and Total
digital. Its Collected is 0 by construction, so its Receivable is the
whole of that bill's amount — which is also why Card and UPI individually
always show a Receivable of exactly 0 (every rupee in either bucket is,
by definition, matched): the whole of the digital receivable (G) now
concentrates visibly into Unclassified rather than being invisibly spread
across Card and UPI. A new control is asserted and shown: "UPI + Card +
Unclassified = Total digital".

**New table, `mop_reco.db.DigitalModeAgg`** — one row per store per
business date per mode (card/upi/unclassified), built once at run time
directly from the run's own `bill_lines` DataFrame (payment_mode only
exists after R1 matching, so — unlike `mop_day_agg` — this cannot be
rebuilt from raw POS data; `runs.run_reconciliation()`'s new
`persist_digital_mode_agg` stage does it in the same pass, no extra read).
`r1_bill_lines` also gained a `payment_mode` column (nullable, added via
the same lightweight `ALTER TABLE` pattern as `date_diff_days`/
`match_note`) for traceability — captured at match time in
`r1_bill_txn.match_tier1`/`match_tier2_edc_other` from the gateway pool's
own `payment_mode` field, never derived from anything else.

**"Collected" for Card/UPI needs no separate live query any more.** Since
only a MATCHED line can ever be classified card/upi, Sale and Collected
are identical for both by construction — `_digital_mode_sales()` reads
the precomputed aggregate directly and adjusts it live only for the
handful of currently-active manual matches whose bill the engine still
shows unmatched (parsing the manual match's own `validation_detail.txn.
payment_mode`, or its MOP for a Bajaj gateway) — moving that bill's
amount out of Unclassified the instant the match is recorded, the
identical cascade-with-no-re-run guarantee column F/Collected already has
(§6.9b-ii item 1). The old MOP-based `CARD_MOPS`/`UPI_MOPS` tuples and
`_live_collected_by_bucket()` are removed — CLAUDE.md's own record of the
315ms-vs-3.9s query-planner fix they carried is superseded by this simpler
design, which needs no per-MOP query at all for Card/UPI any more.

**`scripts/backfill_digital_mode_agg.py [run_id]`** brings an existing run
forward, the same shape as `backfill_mop_day_agg.py` — but it must also
read the warehouse's own `paytm_settlement` Parquet once (an old run's
bill lines carry `matched_gateway`/`matched_txn_ref` but no
`payment_mode`, since that column did not exist yet) to look the mode up
by `transaction_id`, and backfills `r1_bill_lines.payment_mode` for those
rows at the same time.

Verified on full July, run #2: Card Rs 4,24,60,926.00 (4.1% of total sale)
· UPI Rs 49,89,42,615.21 (48.4%) · Unclassified Rs 2,26,685.00 (0.0%) —
**UPI the larger by a wide margin**, as expected. All controls hold,
including the new one. Live-verified in the browser at 1366px: no wrap, no
clipping, no console error.

#### The store cash card rebuilt as its own full-width row (2026-09-19)

**The Part-2 card above, squeezed into the section header's own row,
read as spillage — cramped labels, "Closing at store" wrapping onto its
own line.** Rebuilt as one white card (radius 16px, 1px `--border`, 20px
padding) on its OWN full-width row directly BELOW "Store-wise detail"'s
header line, never beside it — `Summary.py`'s `_store_cash_card()` no
longer takes a container to render into; `_section_toggle()` lost its
`extra` parameter entirely (nothing else used it) and the card is called
as a plain statement right after the toggle, still shown regardless of
the section's own open/closed state.

**Four input cells, a 1px vertical divider, then the one result cell** —
"four inputs, one result" — all five right-aligned with equal cell width,
labels 11px `--ink-soft` uppercase/0.08em tracking, figures 18px `--ink`/
600/tabular-nums EXCEPT Closing at store at 20px. A negative figure
(Expense & income in practice) carries `--bad` with a real ASCII
hyphen-minus — confirmed by codepoint (`U+002D`), not a rendering
artifact of a different dash.

**The font family for the figures moved from `--font-heading` (Plus
Jakarta Sans) to `--font-body` (Inter).** This is the actual fix for "the
current en-dash is the wrong glyph and breaks alignment" — the character
written was always a plain hyphen-minus (`formatting.indian_group()` never
used anything else), but Plus Jakarta Sans's hyphen-minus glyph does not
sit on the same baseline/width as its own digits at this weight, which is
what made it read as the wrong dash and broke column alignment. Inter's
tabular figures (already the table-cell font elsewhere in this app) keep
a true, aligned hyphen-minus.

**Below 900px, five flex items at a shared ~30% basis wrap to 3 then 2**
— a plain consequence of the browser's own line-wrapping, not an explicit
"3+2" rule to maintain; the divider (`display: none` below 900px) never
participates in the wrap. Verified live at 700px: Opening balance / Cash
sale / Cash deposit on row one, Expense & income / Closing at store on
row two, nothing truncated.

Verified live in the browser: at 1366px the card is 896px wide with no
wrap; the arithmetic and the mismatch assertion (`Closing at store = sum
of column M over the last filtered date`) are byte-for-byte unchanged
from Part 2's own design — only the layout and the font moved.

#### The store cash card rebuilt AGAIN as an equation, and the page's own margins trimmed to fit it (2026-09-20)

**Five loose tiles still didn't read as a calculation — rebuilt to show
the operators: `Opening + Cash sale − Cash deposit + Expense/income =
Closing`, left to right, ending in a result.** The header ("Store-wise
detail" / row count / its own +/- toggle) and the equation now live
inside ONE card — `st.container(key="store-cash-card")` wraps both (CSS
on `.st-key-store-cash-card`), with an explicit `<hr class="mop-cash-hr">`
as the 1px `--border` hairline between them — never a header sitting on
the grey page background above a separate white band. `+`/`−`/`=` render
as their own narrow flex items (`.mop-cash-op`) between the cells, in
`--ink-soft` at 16px, aligned to the FIGURE line via `align-items:
flex-end` on the row (labels sit above figures inside each cell, so
bottom-alignment naturally lines an operator up with the digits, not the
uppercase label). Cash deposit itself stays positive — only the operator
in front of it is a minus. Closing at store is a `--accent-tint` chip
(radius 10px, figure at 20px) so it reads as the conclusion, not a fifth
input.

**A real, measured width conflict, found before any pixel was called
final — not assumed.** The equation's own content (labels + 18/20px
tabular figures + the specified 24px/28px gaps) measures **978px** wide.
At this app's actual 1366px-window layout, the card's available content
width was only **855px** — Streamlit's own default `stMainBlockContainer`
padding (80px each side, nothing to do with anything in this app's own
CSS before this) plus the sidebar left only that much. Asked the
operator how to close the 123px gap rather than silently shrinking a
number they'd specified; the operator chose trimming the page's own
margins over compressing the equation's spacing. **`div[data-testid=
"stMainBlockContainer"]` padding cut from 80px to 16px each side** — a
global change, not scoped to this card, so every page on this app now
has more usable width, not only Summary's.

**Even after that, 1000px still needed a second, smaller fix.** With the
result group (`.mop-cash-op--eq` + the Closing-at-store chip) forced onto
its own line below 1100px (`flex-basis: 100%`), the REMAINING four
inputs + three operators still didn't fit the narrower line on their own
at a 1000px window (measured: 592.69px of content + six 24px gaps =
736.69px needed, against 616.67px available — the four-cells-together
requirement is what your own spec asked for at this breakpoint, so the
gap is what gives, not the cells). The 24px gap between cells (full width
only) drops to **3px, only inside `@media (max-width: 1100px)`** — found
by measuring the exact shortfall rather than guessing a number, and
confirmed by DOM measurement (not just a screenshot) that it lands
exactly two lines, never three: line 1 the four inputs with their
operators, line 2 `= Closing at store`, right-aligned (a wrapping flex
row's `justify-content: flex-end` applies per line on its own).

**A `.mop-cash-hr` hairline that silently rendered `0px none` until
`!important` was added.** `hr, div[data-testid="stDivider"]` already sets
`border-color !important` globally (2026-09-11's divider rule) but not
`border-style`/`border-width`; something later in Streamlit's own
generated CSS was still winning on those two properties at equal or
higher specificity. Rather than chase the exact conflicting rule, the
three sub-properties this hairline actually needs (`border`, `border-top`,
`margin`) are declared `!important` on `.mop-cash-hr` directly — confirmed
in the browser afterward: computed `border-top` reads
`0.667px solid rgb(228, 235, 243)` (the fractional px is the emulated
viewport's own device-pixel-ratio rounding, not a bug).

Verified live at both widths asked for: **1366px** — one line, right edge
at 1319px inside a 1366px window, nothing wrapped or truncated; **1000px**
— exactly two lines (DOM-measured, not eyeballed: all four cells share
one `top`, the result group sits on its own lower `top`), never three,
never a label separated from its own figure. Arithmetic and the mismatch
assertion are untouched — this was a visual-only change throughout.

#### Open bills default to UPI provisionally — Unclassified is gone; the hero band, composition bar and restyled matrix; the cash strip fixed for real this time; dates in DD/MM/YYYY with Year/Month quick pickers (2026-09-21)

**Four changes, the operator's own business rule first.** "An open bill
has no matched transaction, so it has no payment_mode. Until I manually
match it, treat that sale as UPI. Once matched, it reclassifies
automatically." `mop_matrix.PROVISIONAL_DIGITAL_MODE = "upi"` is the ONE
place this default lives, with a comment so nobody later mistakes it for
a discovered fact. **The Unclassified column is gone from the matrix
entirely — `card + upi = Total digital`, always, no third bucket.**
`store_sheet_source._digital_mode_sales()` now returns `card`,
`card_collected`, `upi`, `upi_collected`, `provisional_upi`,
`provisional_bills` — an open bill's amount counts as UPI *Sale* (folded
in via `upi = upi_collected + provisional_upi`) but never UPI *Collected*,
since nothing was actually confirmed collected for it. This is why UPI
(and only UPI) can carry a non-zero Receivable now — Card cannot, since
nothing reaches Card without a real matched transaction saying so. The
underlying `digital_mode_agg` table is UNCHANGED at rest (still stores a
factual "unclassified" bucket for a truly-open bill); the provisional
default is applied only at the one read function, never baked into stored
data — the same "raw truth vs. derived display" separation this app
already uses for Gift Voucher's self-collected treatment. The manual-match
cascade (§ above, "Card vs UPI corrected") is unchanged in shape — it
still moves a bill's amount out of the provisional pool the instant a
match lands, live, no re-run — only what it moves it OUT OF changed name
(provisional-UPI pool, not a separate displayed column).

**Verified, before-and-after, on full July (run #2):** UPI **Rs
49,89,42,615.21 → Rs 49,91,69,300.21** (up by exactly Rs 2,26,685.00, the
former Unclassified figure); Card unchanged at **Rs 4,24,60,926.00** (Card
never receives a provisional addition); `UPI + Card = 54,16,30,226.21 =
Total digital`, exactly, no third bucket. A new footnote under the matrix,
hidden entirely when its count is 0: *"Rs X of UPI sale (N open bills) is
provisional — these bills have no matched transaction yet and reclassify
on match."*

**The particulars matrix reshaped again — a hero band, a composition
bar, then the restyled detail — because giving every cell equal weight
made nothing read as a headline.** Three new pieces, all reading from the
SAME `mop_particulars_matrix()` — nothing computed twice, so nothing can
disagree:

1. **The hero band** — Total sale / Collected / Receivable, three cells
   in ONE card (`st.container(key="hero-band")`) divided by 1px `--border`
   hairlines, 34px/700/tabular figures, 11px uppercase `--ink-soft`
   labels, 12px `--ink-soft` sub-lines. A non-zero Receivable turns its
   own hairline into a 3px `--warn` bar — the only colour signal in the
   band, always paired with the sub-line's own percentage text.
2. **The composition bar** — one 28px, radius-6px, 100%-stacked bar,
   fixed series order UPI/Card/Cash always (never reordered by size,
   never recoloured by a filter), each segment `flex-grow` set to its OWN
   `MatrixColumn.pct` — the SAME number the matrix's own "% to total"
   shows, so the bar cannot drift from the table. A 2px `--surface` seam
   between segments; a direct `%` label inside a segment only when it is
   ≥8% wide (Card's 4.1% is legend-only); a legend row below with a
   coloured chip, the name in `--ink`, the value/% in `--ink-soft` — text
   is NEVER in the series colour, only the chip carries the hue.
3. **The matrix itself** — horizontal hairlines only, no vertical grid
   lines; "% to total" is no longer its own row, it sits as a
   `.mop-matrix-pct` sub-line tight under Sale's own figure; a
   `DIGITAL`/`CASH` group-header band (10px uppercase `--ink-soft`) spans
   UPI+Card+Total digital and Total cash; the Total sale column carries a
   `--surface-alt` background as the anchor everything else is measured
   against; a spacer row plus a hairline (`--first-adjustment`'s own
   `border-top`) separates Gift Voucher/Credit Note as their own block;
   the `--warn` tint stays the only tint, only on a non-zero Receivable
   cell.

**A canvas grid (`st.dataframe`) cannot do any of this** — no group
headers, no sub-row nesting, no per-column background, no hairline-only
borders (§8.1's own note on why `st.dataframe`'s glide-data-grid is
canvas-drawn applies here too). The whole matrix moved to a plain HTML
`<table>` (raw `st.markdown(..., unsafe_allow_html=True)`, the same
pattern the cash equation strip already uses) — a deliberate, small-scale
exception to "never restyle a dataframe with HTML" (§8.1), since this is
a fixed nine-row report, not a sortable/exportable data grid; nothing
here was ever sortable or exportable in the first place.

**The series palette was VALIDATED, not eyeballed** — the dataviz skill's
`validate_palette` (node unavailable on this machine; ran the shipped
Python twin instead, same math). The operator's own proposed Card colour,
`#10375C` (this app's existing `--accent-deep`), **FAILED outright** —
OKLCH lightness 0.33 (needs 0.43–0.77) and chroma 0.079 (needs ≥ 0.10): it
reads as near-black, not a hue, before even comparing it to its
neighbours. Re-stepped to `#92400E` (a burnt amber) — passes every check
(CVD ΔE 24.8 against target 8.0; normal-vision floor ΔE 30.9 against 15.0)
and sits well clear of `--warn`/`--good`/`--bad` so it is never mistaken
for a status colour on the same page. UPI (`#16A5C4`) and Cash (`#7A5AF8`)
were already fine as proposed. New `:root` tokens: `--series-upi`,
`--series-card`, `--series-cash` — reserved for this ONE composition bar,
never reused for a status meaning.

**A real bug found live, not assumed fixed from the code alone (§9's own
rule) — the hero band's 34px figures OVERLAPPED at 1000px.** The first
cut gave each cell `flex:1 1 0; min-width:0` with no wrap and no fluid
sizing — three fixed-34px figures cannot fit side by side once the
window narrows, and `min-width:0` let the cells shrink right through each
other's text instead of wrapping or shrinking the type. Fixed with fluid
type, not a breakpoint: `font-size: clamp(22px, 2.6vw, 34px)` on
`.mop-hero-figure`, plus `overflow:hidden`/`text-overflow:ellipsis` as a
belt-and-braces floor so a figure that still doesn't fit degrades to an
honest ellipsis, never a collision with its neighbour. Fixing this
introduced a SECOND regression, also caught before reporting done: the
28px cell padding chosen for 1366px ate just enough space to trip the new
ellipsis fallback at 1366px too (a 9px shortfall, confirmed by
`scrollWidth` vs `clientWidth`) — reduced to 18px, re-verified
`scrollWidth === clientWidth` (no truncation) at 1366px and no cell
overlap (DOM rects checked pairwise) at 1000px. The matrix table got the
same class of fix for the same reason (6 money columns do not fit
uncompressed under ~1150px): wrapped in a `.mop-matrix-scroll` div
(`overflow-x:auto`, table `min-width:720px`) so a narrow window scrolls
the table horizontally rather than silently squeezing or clipping a
column.

**The cash equation strip's own "crushed to the right, empty space on
the left" complaint (superseding 2026-09-20's first attempt) — cells were
content-sized and the row right-justified, so anything narrower than the
content's own total read as bunched up with a gap.** Fixed structurally,
not by re-tuning gaps again: every cell (and the result group) is now
`flex: 1 1 0` with a `min-width` floor (140px/170px) — an EQUAL FRACTION
of the full row, which is what makes the first label start at the card's
own left padding automatically, with no `justify-content` trick needed.
Spacing moved from a row-level `gap` (which needed hand-tuning per
breakpoint — the exact failure mode last time) to per-cell `padding`,
which scales with the cell's own width instead of needing a fresh magic
number at every width. The `<1100px` two-line behaviour is unchanged in
shape (the "=" + result travel together, `flex-basis:100%` forces them
onto their own line).

**"The equal-fraction cells simply have enough natural give" — that first
line was wrong, and live-testing at 1000px is what caught it, not a
re-read of the CSS.** Verifying this same day's own work at 1000px (§9's
own rule: a passing build is not evidence, check the running page) showed
**three lines, not two** — "Expense & income" wrapped onto its own line
between the three-cell first line and the result. Measured, not guessed:
the four cells + three 20px operator columns need `4×140 + 3×20 = 620px`
at the stated 140px min-width, but the card's actual content width at a
1000px window measured **617px** — a 3px shortfall, the identical class
of arithmetic that forced 2026-09-20's original gap-tightening fix,
just landing on a different property this time because the underlying
layout mechanism had changed. Fixed the same way as before, scoped to the
same `@media (max-width: 1100px)` block: the four non-result cells drop
to a **118px min-width and 6px padding** at this width only (reclaims
~104px, comfortably clearing the 3px gap) — the full-width 1366px layout
is untouched, since the rule sits inside the existing narrow-width query.
Re-verified by DOM measurement (not a screenshot): at 1000px all four
input cells now share one `top`, the result sits alone on the next `top`
— two lines; at 1366px all five cells share one common `bottom` — one
line, right edge at 1319px, unchanged from before this fix.

**Dates are DD/MM/YYYY everywhere, with slashes, one shared place.**
`formatting.DATE_DISPLAY_FORMAT` changed from `"%d-%m-%Y"` to
`"%d/%m/%Y"` — `display_date()` is still the only function that decides
this, so every table, export and page that already routed through it
picked up the new separator with no other code change. The three ad hoc
`%d-%m-%Y %H:%M` timestamp formats on Manual matches (upload/match/
register timestamps, which need the time portion `display_date()` doesn't
carry) were updated by hand to match. Every `st.date_input` in the app
(`sidebar()`'s From/To, Run's From/To, Masters' MID-check date) now
passes `format="DD/MM/YYYY"` — Streamlit's own token for this, which also
fixes the placeholder text (was the Streamlit default `YYYY/MM/DD`).

**Year and Month quick pickers, in the Filters sidebar, above From/To —
ONE state with the date fields, never a second source of truth.** A
button's selected/enabled/disabled state is DERIVED every run from the
CURRENT From/To against the loaded range
(`dashboard_common._year_span()`/`_month_span()`, both CLAMPED to what is
actually loaded so a year click can never produce an out-of-range date
that would trip `sidebar()`'s own "remembered date outside this run"
reset) — nothing is stored independently, so a button and a hand-typed
date can never disagree: typing a custom date simply means no span
matches `current`, so nothing shows selected, with zero extra code. A
year/month with no loaded-data overlap is `disabled=True`, rendered
dimmed via CSS scoped to `st.container(key="qp")` (`.st-key-qp
.stButton button:disabled`) — scoped so this never repaints the app's
other primary/secondary buttons elsewhere (e.g. "Build the Excel sheet"
keeps the theme's own primaryColor). Selected uses `type="primary"`,
overridden ONLY inside `.st-key-qp` to `--accent`/white rather than the
app's primaryColor blue, so a quick-pick button is visibly a different
control from an action button. Clicking writes via `on_click=`, which
Streamlit runs BEFORE the script reruns — the safe, idiomatic way to
mutate `st.session_state` here, sidestepping the widget-instantiation-
order trap (§6.14's "three widget-state traps") entirely rather than
needing a deferred-flag dance.

Full suite: 530/530 passing. Live-verified at 1366px (hero band,
composition bar, matrix, cash strip — nothing wrapped, clipped or
overlapping) and 1000px (cash strip's own two-line fallback still holds;
hero band degrades to ellipsis, never collision; matrix scrolls
horizontally, never clips). Backend cost measured directly (`load_fast` +
`mop_particulars_matrix` + `store_cash_summary`): **560 ms total** on the
full month — actually FASTER than the 2026-09-19 figure (585 ms for the
matrix alone), since the provisional-default rule replaced the old
per-MOP live "Collected" queries with a plain aggregate read plus one
cheap `COUNT`.

#### The composition bar is gone, the hero band and matrix show Rs Cr, and every font on Summary's own band/matrix/sidebar shrunk (2026-09-21, visual only — no figure, formula or column changed)

**The composition bar under the hero band is removed entirely, on
instruction — the UPI/Card split already lives in the matrix below and is
not repeated as a graphic.** `Summary.py`'s `_series`/`_bar_segments`/
`_legend_items`/`composition_html` and the `.mop-composition`/`.mop-comp-*`
CSS rules are deleted, not hidden; `--series-upi`/`--series-card`/
`--series-cash` go with them — confirmed by search before deleting that
nothing else referenced any of the three tokens (they existed only for
this bar). The hero band's own card padding dropped from 20px to 16px so
removing the bar does not leave a gap where it used to sit.

**Money on the hero band and the particulars matrix now reads in Rs
Cr, always — a new, separate function, `formatting.money_cr()`, never
`money_short()`.** `money_short()` (used elsewhere for a headline tile)
picks whichever unit fits best — Cr for a big figure, L for a smaller one
— which is exactly wrong for a table, where the whole point is that every
cell stays comparable at a glance; `money_cr()` never switches units, so a
0.00-Cr Receivable cell sits in the same unit as a 103.10-Cr Total sale
cell. Two decimals always, formatted straight from the same unrounded
float `mop_particulars_matrix()` already computed — nothing here sums
already-rounded Cr strings into a total (display-only rounding, per the
brief's own instruction). A one-line "All figures in Rs Cr" caption sits
under both the hero band and the matrix.

**Nothing else on the page changed unit.** The store-wise detail table
(columns A-N), the cash equation strip, every export, and the Float page
all still read `money_table()` — full rupees, two decimals, untouched.
Verified before/after on the same live figures: Total sale
Rs 1,03,10,45,827 → **Rs 103.10 Cr**; Receivable Rs 2,21,914 →
**Rs 0.02 Cr** — the underlying values are unchanged, confirmed by reading
`get_page_text()` on the running page rather than assuming the arithmetic
from the code alone.

**Font sizes cut across the hero band, the matrix and the sidebar filters
— the whole block was eating more vertical space than the data it
presents.** Measured live via `getComputedStyle`, not eyeballed:

| Element | Before | After |
|---|---|---|
| Hero label | 11px | **10px** |
| Hero figure | 34px (clamped) | **22px fixed** |
| Hero sub-line | 12px | **11px** |
| Matrix column header | 12px | **11px** |
| Matrix row label | 13px | **12px** |
| Matrix figure | 13px | **12px** |
| Matrix "% to total" | 12px | **11px** |
| Matrix row height (single-line) | ~40px | **~32px** (measured 31.9px) |
| Page title (`st.title()` → `<h1>`) | 28px | **22px** |
| Section heading (`#####` → `<h5>`) | 18px | **15px** |
| Sidebar "Filters" heading | default | **13px/600** |
| Sidebar helper caption | 12px | **11px** |
| Sidebar From/To label | default | **11px** |
| Sidebar From/To input | default | **12px text, ~33px tall** |
| Quick-picker Year/Month label | 11px | **10px** |
| Quick-picker button | 12px text, ~31px tall | **12px text, min-height 28px** (renders ~31px — Streamlit's own button chrome adds a few px beyond the CSS min-height that this app's other buttons already live with) |

**The hero figure's font-size went from a `clamp()` back to a fixed
value, on purpose.** The fluid clamp existed only because a fixed 34px
figure overlapped its neighbours below ~1150px (2026-09-21, earlier the
same day); at 22px there is enough headroom at every width this app runs
at that the fluid sizing is no longer needed — confirmed live, no overlap
at 1000px.

**The sidebar CSS is scoped narrowly enough not to catch anything by
accident.** `st.markdown("**Filters**")` is the sidebar's only true
markdown-bold text (`<p><strong>`) — every other bold string there (the
run/build stamps, "Working as: `<name>`") is raw HTML inside its own
styled div, so `section[data-testid="stSidebar"] div[data-testid=
"stMarkdownContainer"] p strong` cannot repaint those by mistake; the
caption/date-input rules are likewise scoped to `section[data-testid=
"stSidebar"]` so no other page's captions or date pickers are touched.

Full suite: 530/530 passing (added `test_money_cr_always_shows_crores`/
`test_money_cr_never_switches_to_lakh` to `tests/test_formatting.py`).
Live-verified at 1366px: composition bar gone (confirmed by `get_page_text`
carrying no UPI/Card/Cash bar or legend text), every figure in the hero
band and matrix reading Rs Cr with a consistent unit including the 0.00-Cr
cells, the "All figures in Rs Cr" note under both, the store-wise detail
table and cash equation strip still full rupees, no console errors, and
every measured font-size/row-height matching the table above.

#### Sidebar helper text gone, Year filter built from the data, "All figures in Rs Cr" removed, and Streamlit's own Material Icons font found broken app-wide (2026-09-22)

**Four small removals, one real bug.** The sidebar's "Apply to every page.
Leave From/To empty for every loaded date." caption is deleted outright —
`sidebar()` no longer calls that `st.caption()` at all, nothing left to
collapse a gap around. The "All figures in Rs Cr" note under both the hero
band and the matrix (added 2026-09-21) is gone the same way — every figure
already carries its own "Cr" suffix, so the note repeated information
already on screen.

**The Year row is now built from the loaded data, never a fixed list.**
`_quick_date_picker()`'s old `years = list(range(first.year - 1, last.year
+ 2))` padded a year either side of the loaded range for context — which
is exactly the hardcoded-adjacent-years shape the operator's instruction
ruled out ("Do not hardcode a fixed list of years"), and is why 2025 was
showing with no July data behind it. Fixed by deriving the year set from
every loaded date's own year — `sorted({date.fromisoformat(d).year for d
in dates})`, `dates` being the same `loaded_dates(run.id)` list `sidebar()`
already had — not just `first.year..last.year`, so a genuine GAP in the
loaded years (a skipped year, not just a padded one) is never shown as if
it had data either. On the one month of July 2026 this machine has loaded,
that resolves to a single button, `2026` — matching the instruction's own
"if that gives you 2026 only, show 2026 only." No button is ever disabled
under this scheme (a year only appears because it HAS data), so the
`disabled=` branch in the button loop is now dead in practice but left in
place — a future multi-year load with an actual gap would still need it.

**A real, systemic bug — found live, not assumed from a screenshot
description alone.** The operator's report ("rendering the literal text
'keyboard_double_arr'") was a truncated capture of a longer string;
inspecting the live DOM found the actual full text —
`keyboard_double_arrow_left` on the sidebar's own native collapse control
— and confirmed the ROOT CAUSE first, not just patched the one symptom:
`getComputedStyle` on the icon span reported its `font-family` had already
fallen back to `Inter` (this app's own body font), meaning Streamlit's
Material Symbols icon font genuinely failed to load in this container (no
outbound network access to Google Fonts for it) — every `[data-testid=
"stIconMaterial"]` span anywhere in the app renders its raw ligature NAME
as plain text instead of a glyph, not just the one control reported. A
full-page sweep (before any fix) found it in three places: the sidebar
collapse control (`keyboard_double_arrow_left`), Streamlit's native
`st.expander` triangle (`keyboard_arrow_right`/`_down`, present on every
page that uses one — Summary alone has two, "Closed and auto-resolved"
and "Data notes"), and one place in THIS app's own code,
`app/pages/3_Run.py`'s `st.page_link(..., icon=":material/arrow_forward:")`.

**Fixed per-context, never with one blanket icon swap** — a collapse
chevron and an expander triangle mean different things, so a single
replacement glyph for every `stIconMaterial` span would be wrong for at
least one of them. `theme.css` now hides the raw ligature text everywhere
(`[data-testid="stIconMaterial"] { font-size: 0; }`, confirmed live: the
icon's own bounding-box width is entirely the `::before` pseudo-element's
box now, not the hidden text) and paints a plain character back in via
`::before content`, scoped per selector:
- **Sidebar collapse/expand** — confirmed live that Streamlit keeps these
  as two SEPARATE native controls, `stSidebarCollapseButton` (only ever
  shows "collapse") and `stExpandSidebarButton` (only ever appears, and
  only ever shows "expand", once the sidebar is hidden) — never one
  control that toggles its own ligature text — so each gets its own fixed
  `::before` rather than tracking open/closed state: `«` (U+00AB) for
  collapse, `»` (U+00BB) for expand, 16px, `--ink-soft`/`--accent` on
  hover, in a 28px square hit area (`width/height: 28px` on the wrapper,
  confirmed by `getBoundingClientRect()` to be exactly 28×28 for both, not
  approximately).
- **`st.expander`'s own triangle** — CSS cannot select on a text node's
  own content, so `keyboard_arrow_right` vs `keyboard_arrow_down` is read
  off the real signal that produced it: the `<details open>` attribute the
  icon already sits inside (confirmed live: `stExpander` wraps `<details>`,
  which wraps the icon — `<details>` itself carries no testid of its own).
  `›` (U+203A) closed, `⌄` (U+2304) open, re-verified live by opening one
  of Summary's two expanders and confirming its icon's `::before` content
  switched while the other, still closed, stayed `›`.
- **The one instance in this app's own code** — `Run.py`'s `icon=
  ":material/arrow_forward:"` on its "Open the Summary" link was simply
  DROPPED, not replaced with a third CSS override; a decorative arrow on a
  nav link isn't load-bearing enough to justify one, and the link reads
  fine as plain text.

**A left-open trap, named rather than silently accepted:** any future
`:material/...:` icon added anywhere in this app will render BLANK (hidden
by the blanket `font-size: 0` rule, no `::before` written for it) rather
than showing broken ligature text — better than the bug just fixed, but
still not a real icon, since the Material Symbols font has no path to load
in this environment at all. `theme.css`'s own comment says so, so the next
person who reaches for `icon=":material/...:"` finds the warning before
shipping a fourth invisible icon.

Full suite: 535/535 passing, no test changes needed (nothing asserted the
removed caption's text, the old year-range shape, or the "All figures in
Rs Cr" note). Verified live at 1366px: the sidebar's Year row shows only
`2026` (full-width, single button), no helper caption beneath "Filters",
no "All figures in Rs Cr" under either the hero band or the matrix, the
collapse/expand controls hold a real 28×28px hit area with the correct
`«`/`»` painted via `::before` (confirmed via `getComputedStyle`, not a
screenshot, since the glyph is too small to read reliably in this
environment's own compressed screenshot capture), both expander triangles
switch correctly between `›` and `⌄`, and a full text-node sweep of the
live DOM confirms every remaining raw ligature string
(`keyboard_double_arrow_left`, `keyboard_arrow_down`, `keyboard_arrow_
right`) renders at `font-size: 0` — present in the DOM, never visible.
Console: zero errors on a freshly opened tab (the handful of
`ERR_CONNECTION_REFUSED` entries seen on the reused tab are the same
stale-websocket-reconnect artifact from repeated `preview_stop`/
`preview_start` cycles documented earlier in this file, confirmed again by
opening a genuinely fresh tab showing none).

#### The hero band is gone entirely, and the Year filter offers one padding year so a year is pickable before its data arrives (2026-09-22)

**The hero band — Total sale / Collected / Receivable in its own card
above the matrix — is REMOVED outright, not emptied.** The page now
starts with the particulars matrix directly under the title.
`_hero_cells`/`hero_html`/`st.container(key="hero-band")` and the band's
own %-of-sale calculations are deleted from `Summary.py`, not hidden —
confirmed nothing else on the page used them before deleting. The three
figures are not lost: they are the matrix's own Total sale column
(Sale/Collected/Receivable rows) — the SAME `matrix.total_sale` fields
the band used to read, never a separate computation, confirmed identical
before the band came out (July: Rs 103.10 Cr / Rs 103.08 Cr / Rs 0.02 Cr,
byte-for-byte what the band showed). `range_label` (imported only for the
band's Total-sale sub-line) is no longer imported in `Summary.py`. In
`theme.css`, `.st-key-hero-band` and every `.mop-hero-*` rule are deleted
the same way — `.st-key-matrix-card` now carries the card styling alone.

**The Year row now offers one year beyond the loaded range — "the next
calendar year" — so a year is selectable before its own data arrives, on
instruction: "Do not hardcode `{2026, 2027}` — derive it, so it stays
right as the years roll on."** `_quick_date_picker()`'s `years` list is
`loaded_years + [loaded_years[-1] + 1]` — a computed padding year, never a
literal 2027 anywhere in the code. On the one month of July 2026 this
machine has loaded, that resolves to exactly `2026, 2027` — the 2025
removal from the prior entry stays correct (a year strictly BEFORE the
loaded range is still a genuine gap, still disabled, never padded).

**The padding year is ENABLED, only visibly DIMMED — a real, deliberate
change from "no data → disabled" (2026-09-21's own rule), because the
instruction this time is explicit: "clicking it sets the date range and
the page shows an empty state, not an error."** `_year_span()`/
`_month_span()` both gained the same one exception: a year/month entirely
AFTER `last` (which, by construction, is only ever the ONE padding year
`_quick_date_picker` ever offers) returns its own plain, UNCLAMPED
calendar span rather than `None` — there is nothing loaded to clamp
against, so the honest answer is the full calendar year/month, not a
disabled button. A year/month that is a genuine GAP (entirely BEFORE the
loaded range, or a skipped year inside a multi-year load) is still `None`
→ still disabled — the exception is specifically "after `last`", not "no
overlap" in general.

**Three places had to widen together, or a padding-year click would
silently undo itself on the very next rerun — found and fixed as one
change, not patched separately after the fact.** `sidebar()`'s own
"remembered date outside this run" reset used to compare against `last`;
a click into 2027 would set `K_DATE_FROM`/`K_DATE_TO` to 2027 dates via
`on_click` (which runs BEFORE the rerun), and on the very next run
`sidebar()`'s reset check would see `2027-01-01 > last` and silently null
it back out — the exact bug class §6.14's "three widget-state traps"
already catalogues, just a new instance of it. Fixed with one shared
helper, `_padding_upper_bound(last)` (`31 December of last.year + 1`),
used in THREE places that must all agree: the reset-check's upper bound,
both `st.date_input` widgets' own `max_value` (was `last` — a hand-typed
date inside the padding year needs the WIDGET itself to accept it, not
just survive the reset check), and — structurally, by construction, since
`_year_span`/`_month_span` only ever special-case a year/month that is
already known to be at most the one padding year — the quick-picker
buttons themselves.

**Dimming uses the same `st.container(key=...)` CSS-scoping pattern this
app already uses everywhere, with FIXED keys, never the year value.** The
padding year's own button sits inside `st.container(key="qp_pad")`; each
of its twelve month buttons (shown once that year becomes `active_year`)
sits inside `st.container(key=f"qp_pad_m_{month}")` — `month` is 1-12,
never a year, so this needs no update as the calendar rolls forward.
`theme.css` targets `.st-key-qp_pad` and, via an attribute-substring
match (`[class*="st-key-qp_pad_m_"]`), all twelve month keys with one
rule — `opacity: .5` on the button, layered on top of the existing
`kind="secondary"` styling so a dimmed-but-selectable button still reads
as visually distinct from both a disabled gap-year button (existing
`opacity: .4` + `cursor: not-allowed`) and a normal enabled one.

**Verified live, clicking through both directions, not just read from the
code.** Clicked 2027: button state flips to selected (`type="primary"`),
every month dims and stays enabled (`disabled: false`, confirmed via
`getComputedStyle`), From/To fill with `01/01/2027`/`31/12/2027` with no
widget error, the range label reads "1 January – 31 December 2027", and
the page renders a clean empty state — every matrix figure `Rs 0.00 Cr`,
zero exceptions, zero matched bills, `71,540` store-day rows all zeroed
(the store-day grid materialises a row per store per loaded date even
with no data behind it — pre-existing behaviour, not new) — **no
traceback, no error banner**. Clicked back to 2026: July's real figures
return exactly as before. Console: zero errors on a freshly opened tab
both before and after the click (the `ERR_CONNECTION_REFUSED`/
`ERR_NETWORK_IO_SUSPENDED` entries seen on the reused tab across this
server restart are the same stale-websocket artifact documented earlier
in this file).

Full suite: **541/541 passing** — 535 before this change, +6 new tests
(`test_year_span_clamps_to_the_loaded_range`,
`test_year_span_is_none_for_a_year_entirely_before_the_loaded_range`,
`test_year_span_is_the_full_calendar_year_for_the_one_padding_year`,
`test_month_span_is_the_full_month_for_a_month_in_the_padding_year`,
`test_month_span_is_none_for_a_month_entirely_before_the_loaded_range`,
`test_padding_upper_bound_is_the_end_of_the_year_after_last`, all in
`tests/test_dashboard_chrome.py`, calling `_year_span`/`_month_span`/
`_padding_upper_bound` directly rather than only asserting on source
text, since these three functions' actual return values are exactly what
this change depends on being right).

#### Cash received / Cash receivable get their own equation row on the store cash card — never folded into Closing at store (2026-09-22)

**A second row, `Cash deposit − Cash received = Cash receivable`, added
to the SAME card, below a hairline — deliberately NOT inline with row 1,
on instruction: "Closing at store is H + E − I + L and adding J or K to
that line would make the arithmetic wrong."** Row 1's own formula is
byte-for-byte unchanged. `store_sheet_source.store_cash_summary()` grew
four new keys — `cash_received` (sum of J over the filter, a flow, summed
exactly like cash_sale/cash_deposit/expense_income already are),
`cash_receivable_net`, `check_receivable` and `matches_receivable` — read
from the same precomputed `store_day_agg` table row 1 already reads, no
new query shape, no re-derivation.

**Cash deposit repeats in both rows on purpose — the SAME value, same
formatting, both places** (verified live: both rows show
`48,98,98,591.00` on full July). It is the opening term of the
Closing-at-store equation AND the starting term of the receivable
equation; showing two different-looking figures for the same column
would have been the actual bug here.

**The netting rule is the one already agreed everywhere else on this
page, reimplemented inline rather than called** —
`store_sheet.net_cash_receivable()`'s own formula, `max(0, sum(I) −
sum(J))`, summed over the filter FIRST and floored LAST, never a sum of
daily K (which would either overstate the total by dropping every
negative day, or let unrelated stores' good and bad days cancel in a way
that hides which store actually has the problem). Reimplemented rather
than calling the pandas-frame helper because `store_cash_summary()` reads
via SQL aggregates, not a DataFrame — the arithmetic is identical,
confirmed against the same July acceptance figure this file already
records for `net_cash_receivable()`: **Rs 65,71,377.00**, to the rupee,
live in the browser.

**A negative net (Defect B — a credit exceeding its deposit) shows as
`0.00` here, never a negative number, on instruction** — the count and
value stay in Float's own Data quality section (§6.9d/§6.14's earlier
note on where that section now lives); this card does not repeat it.
Tested directly: `test_store_cash_summary_cash_receivable_floors_at_zero_
never_negative` feeds a store-day where J (140) exceeds I (100) and
asserts the displayed net is `0.00`, never `-40.0`.

**A genuine control, not a tautology** — `check_receivable` is
`sum(StoreDayAgg.cash_receivable)` (the aggregate's own precomputed K,
summed WITHOUT flooring) compared against `cash_deposit − cash_received`
computed independently in this same function. K = I − J is an algebraic
identity that must hold on every row regardless of sign, so this catches
a genuinely corrupted aggregate row the same way row 1's `matches` check
catches a corrupted `closing_balance` — two independently-sourced
figures, asserted equal, MISMATCH shown in `--bad` naming the difference
if they ever disagree. `test_store_cash_summary_flags_a_receivable_
mismatch` plants a row with `cash_receivable=999.0` against a real
`100 − 40 = 60`, and asserts the control fires while `cash_receivable_
net` still reports the correct `60.0` — the display and the control are
deliberately independent, so a corrupted K cannot poison the number the
operator actually sees.

**Alignment: row 2 is right-aligned as its OWN group under the right half
of the card, not stretched cell-by-cell under row 1's own columns** — the
instruction's own explicit fallback, taken deliberately rather than
attempted-then-abandoned. Row 1 is a 4-term equation (5 flex-1 slots
counting the result group); row 2 is a 3-term one (Cash deposit / Cash
received / Cash receivable). Forcing "Cash deposit" to sit under row 1's
own Cash-deposit column would need two invisible leading spacer slots in
row 2 sized to match row 1's Opening/Cash-sale columns exactly — the
same class of fragile pixel-chasing this card's own 2026-09-20/21 history
(documented above) already spent two rounds fixing the hard way. Instead,
row 2 reuses every row-1 CSS class verbatim (`.mop-cash-cell`,
`.mop-cash-label`, `.mop-cash-figure`, `.mop-cash-op`, `.mop-cash-result-
group`, `.mop-cash-cell--result`) for identical type sizes (10px/600/
uppercase labels, 18px/600 tabular figures, 20px result), inside a new
`.mop-cash-row2` container that switches the cells from row 1's
edge-to-edge `flex: 1 1 0` to natural `flex: 0 0 auto` sizing plus
`justify-content: flex-end` — the whole group hugs the card's right edge
instead of spreading across the full width.

**Non-zero Cash receivable gets its own `--warn` left bar on the result
chip — a different signal from the `--bad` MISMATCH colour, on
instruction: "it is money deposited and not yet credited," an expected
state, never an error.** `.mop-cash-cell--result-warn` adds the 3px
`--warn` border (confirmed live via `getComputedStyle`:
`2.667px solid rgb(232, 150, 46)`, the fractional px the emulated
viewport's own device-pixel-ratio rounding, matching the identical
artefact already noted for `.mop-cash-hr`) and trims 3px off the chip's
own left padding so the total inset matches the plain result chip's.
Closing at store (never warn-tinted, never non-zero-flagged) correctly
shows no border in the same check.

Verified live at both widths asked for, by DOM measurement, not
eyeballing: **1366px** — both rows one line each, no MISMATCH banner
(both controls hold); **1000px** — row 1 wraps to its own existing
two-line shape (unchanged), row 2 stays on ONE line (all five of its
elements share one `bottom`, confirmed via `getBoundingClientRect()`),
right edge at 953px inside the 1000px window, no label separated from
its own figure. Full suite: **544/544 passing** (541 before + 3 net new
in `tests/test_mop_matrix.py`: the flooring test, the mismatch test, and
`test_store_cash_summary_cash_received_and_receivable_are_the_deposit_
equation`, plus the pre-existing empty-selection test extended to assert
the new keys default sanely). Console: zero errors on a freshly opened
tab.

### 6.10 Business date source

Never derive a business date from a filename or a sheet name — both have been
seen to be unreliable (the CMS file's sheet name embeds `24-08-2026`, which
matches no date anywhere in that file's actual data). Always take the date from
a data column:

| Source | Business date column |
|---|---|
| POS (store-wise, bill-wise) | `BILLDATE` |
| Paytm | `transaction_date` |
| Axis EDC | `TRAN_DATE` (settlement due date computed from `PROCESS_DATE`) |
| CMS | `DEPDATE` (deposit date), `CRDATE` (credit date) |
| PTC | `Billdate` / `BILLDATE` per layout (§5) |

### 6.11 Pre-flight checks

Run before any figure is reconciled for a business date. A failure is reported
by name, never silently absorbed into a downstream difference:

- All expected inputs present for the business date. Reconcile only what can be
  reconciled; label every affected figure partial.
- Bill-wise total equals store-wise total per store-day — when the store-wise
  dump is absent (as of 2026-08-24, it always is), report
  `"store-wise dump not supplied"` as its own named condition instead of
  silently skipping the check.
- **No MID maps to more than one store as at the business date** — checked
  against both the Paytm and the Axis MID masters. This is the check that would
  have caught the `037215021610005` → `GLA`/`MBA` conflict on the day it was
  introduced, instead of it sitting undetected in the master until someone
  happened to look. A hit here is a master-data-gap exception (`store_ambiguous`
  in the Axis case), never a silent pick of either candidate.
- Sum of MOP classes equals total collection per store-day (§6.1).
- Every store, MID and TID present in the masters (non-negotiable rule 5).
- No duplicate transaction id in any gateway report.
- Only SUCCESS/ACQUIRING rows in the R1 matching pool.

### 6.12 Master versioning (system responsibility, not the file's)

Both masters we have (`6.MID WISE STORE CODE MASTER`, `5.CMS DEPOSIT MASTER`) are
flat lists with no `effective_from`/`effective_to` column, and they are expected
to **stay that way** — effective-dating is the loader's responsibility to add,
not something to wait on from whoever maintains the source file.

Every master load is versioned:
- On each load, snapshot the master with the load date/timestamp and keep every
  prior version — never overwrite.
- A lookup for a past business date uses the version of the master that was
  current **as at that business date**, not the latest one.
- Without this, a correction like the `037215021610005` fix (§5, Axis EDC
  resolver) would silently rewrite the resolution used by every past
  reconciliation that MID ever appeared in. Versioning is what makes "any date
  can be re-run at any time" (non-negotiable rule 2) safe to apply to master
  data, not just transaction data.

---

### 6.13 The sale universe — a row with no bill number is not a bill

**Any POS row without a `BILLNO` is excluded from the sale universe, whatever
its MOP.** It is not a sale; it is a cash in/out movement — an expense
movement, not a bill.

Excluded rows are **excluded but visible**: reported in the load report as
their own named line with **count and total per MOP**, so a sudden change in
them is noticed rather than absorbed. They stay in the ingested DataFrame and
in `rows_read`; nothing is dropped from what was read.

Confirmed on the data (2026-08-29): the 20-Jul daily `.xlsx` carries 628 such
rows, **all** under MOP `Cash` and none under any other MOP — which is exactly
why cash was the only class that failed to tie. Summing `BASEAMT` over all
cash rows gives **−18,482,043**; restricted to rows that carry a `BILLNO` it
gives **+12,941,046**, matching the store-wise dump exactly. The other three
classes tied all along (digital 13,660,562 · digital_noref 42,102 ·
non_settling −2,092).

`BASEAMT = BASETENDER − BASEBALANCE` holds on real bills (tendered minus
change), so this was never a sign-convention or tender/change defect — it was
non-bill rows being counted as sales.

The monthly `.xlsb` export omits these rows entirely (0 across both sheets, and
its 20-Jul slice natively totals 12,941,046 on 40,242 rows vs the daily file's
40,870). Going forward the monthly export is the authoritative format and no
further such cases are expected — the rule stays as the control that keeps it
that way.

### 6.14 Two-phase execution — raw files are never read during a page interaction

**Phase 1 — an explicit run.** Triggered deliberately, never by browsing. Streams
each raw source **once**, normalises it, writes per-source/per-business-date
Parquet into `warehouse/`, runs the reconciliation, and writes results,
exceptions and control totals into SQLite. Budget ~4–8 minutes for a cold
full-month run, with per-source progress logged. Once POS is in Parquet, a
re-run of a restated date reads Parquet, not the 222 MB `.xlsb` — seconds, not
minutes. This is what makes non-negotiable rule 2 ("every date must be
re-runnable") practical rather than a chore.

**Warehouse-first re-reads (implemented, measured 2026-09-04).** An
already-ingested file is read back from Parquet; the raw workbook is opened only
when it is new or its path/mtime/size has changed, tracked in
`warehouse/<source>/_manifest.json`. Measured on the July POS `.xlsb`:
**441s raw → 70.6s from Parquet (6.3×)**, identical rows, cash total and
duplicate groups. A tampered mtime is correctly rejected and forces a raw
re-read. The residual 70.6s is dominated by the `TPEDCRESPONSE` column, which is
**65% of the frame's memory** and is only needed during reference extraction —
dropping it from the Parquet is the next available win, not yet done.

**A source folder can hold one file per month, not always one dominant dump
(found 2026-09-24, the five-month April-August reload).** `ingest_pos_bill_wise`,
`ingest_axis_edc`, `ingest_bajaj` and `ingest_axis_cms` used to pick only the
single most-recently-modified file across the WHOLE source folder
(`io_.latest_file()`) — correct when a folder held one dominant dump plus an
occasional superseded duplicate, silently wrong the moment it holds five
genuinely different months at once: it would have read only August and
silently dropped April-July, with nothing said about it. Fixed: every
discovered file is now read, and a genuine overlap (the same business date, or
the same natural key — RRN for Axis EDC, Transaction Id for Bajaj,
pickup_code+deposit_date+slip_no for CMS) resolves to the most-recently-modified
contributor, exactly as before, just scoped correctly. Deliberately
per-NATURAL-KEY rather than per-date for the settlement sources: a whole-date
rule broke on Bajaj's June file, which genuinely carries 12 rows dated in
July — a handful of real transactions the July file ALSO happens to carry
(confirmed by Transaction Id, not a coincidence) — and a date-only rule would
have let June's file evict July's own July-1 rows just because one file
happened to have a marginally newer mtime. POS itself stays date-scoped
(its one real duplicate — July's two competing full-month exports — is a
genuine whole-file supersession with no natural per-row key to check against).
The per-file Parquet cache (`_write_parquet_single_file_cache`,
`warehouse_manifest`, keyed on file path + mtime + size) was extended to
cover this multi-file case for POS specifically — measured on the July file
inside a 5-month run: **168s cold → 30.65s from cache**.

**The remaining reconciliation bottleneck, once file parsing is cached, is R1
matching itself — not file I/O (measured 2026-09-24).** A full five-month run:
ingestion (all 4 sources, POS warehouse-cached) took **~13 minutes total**; R1
matching alone (`match_tier1`/`match_tier2_edc_other`, both still a per-row
pandas `.at[]` loop over 3.7M digital bill lines against a ~4.4M-row gateway
pool) took **2,427.6s — 40.5 minutes, three times the ingestion cost.**
Measured throughput: **~1,247 bills/second** against Paytm's own pool at this
scale (slower than July alone's ~3,300 bills/sec, since the per-gateway hash
index and the surrounding `.at[]` overhead both grow with pool size). **Do not
spend further effort on file caching or warehouse reads to speed up a full
multi-month run — that part is solved.** The next real win is vectorizing
`match_tier1`/`match_tier2_edc_other` into a pandas merge/join, replacing the
per-row `.at[]` loop; the acceptance bar for that work is an EXACT match
against a saved multi-month baseline (see `docs/BASELINE_APR_AUG.md` once it
exists), not "close" — a faster matcher that reconciles even one rupee
differently does not ship.

**A run commits per BUSINESS DATE, not once at the end (fixed 2026-09-24,
after an hour-long run gave no visibility into whether it was working or
hung).** `runs._persist_r1()` used to build the whole range's results in
memory and commit exactly twice, at the very end — Summary showed nothing
until 100% of the run had both matched AND persisted, and a late failure lost
the whole run's work. It now commits store-day results and bill-line detail
ONE DATE AT A TIME, oldest first — a date is visible on Summary the moment
its own commit lands, and a crash partway through only loses whatever date was
mid-flight, never the dates already committed. R1 matching itself still has
to see the whole loaded range at once before persistence can begin at all (a
gateway reference is deliberately date-agnostic — §6.9b — so a bill's match
outcome cannot be certified date-by-date while matching is still in
progress); only the PERSISTENCE of an already-complete match is incremental.
The three whole-range aggregate stages that follow persistence
(`persist_store_day_agg`, `persist_mop_day_agg`, `persist_digital_mode_agg`)
are NOT yet per-date incremental — they still commit once for the whole
requested range, so the cash-side (opening/closing/deposit) and MOP-matrix
figures for an in-progress run only become available once ALL of persistence
has finished, even though the R1/bill-matching side (matched, unmatched,
digital sale) is available per date as soon as its own commit lands.

**`sync_exceptions()` merges same-key candidates BEFORE touching the database
(fixed 2026-09-24, found via a real 5-month run crash).** The exception key
(business_date | store | bill_no | mop, §6.9) is deliberately coarser than one
bill LINE — a bill with two unmatched lines sharing the same MOP is meant to
collapse into ONE exception. That collapsing was never actually enforced
before it reached SQLite: `sync_exceptions()`'s own `existing` lookup was
built once from the database and never updated as new rows were added within
the same batch, so two same-key candidates in one run both took the "not in
existing" branch and both tried to INSERT — the second one violating the
UNIQUE constraint on `exception_key` and crashing the run. Fixed with a
merge pass over `candidates` before any database access: one row per key,
amounts SUMMED (never dropped — rule 4's "no write-offs" applies to an
accidental loss as much as a deliberate one), `detail.merged_line_seqs`
keeping every contributing line's own identity so the merge is visible, not
silent. A SECOND, compounding bug sat behind the first: the run's own
top-level exception handler tried to set `run.status = "failed"` and commit
on a session SQLAlchemy had already put into "pending rollback" after the
IntegrityError — which raised a second exception that masked the first and
left the run sitting at `status="running"` forever, indistinguishable from a
genuine hang. Fixed by calling `session.rollback()` first, discarding only
that failed stage's own uncommitted work; every earlier stage's per-date
commits are untouched by it. A run that fails now always ends up
`status="failed"` with a real reason in `notes`, never stuck.

**Phase 2 — the dashboard reads precomputed rows only.** No Streamlit page ever
opens a raw file or holds a full-month DataFrame. Aggregates come from a results
table already grouped by store × date; transaction detail is server-side
paginated. Target page loads: summary < 1s, detail < 1s per page, exception
report < 1s.

**Measured page loads with a full month (660,150 bill lines) loaded:** summary
**46 ms**, exception report **23 ms**, transaction detail **876 ms**.

**The detail view needs the composite index `ix_r1_page` on
(run_id, business_date, store, bill_no).** It pages with `LIMIT/OFFSET` in that
order; without a covering index SQLite sorts the whole month for every page —
measured **25,105 ms** to reach `OFFSET 500,000`, versus **67 ms** with it.
Single-column indexes do not help an `ORDER BY` across three columns. Do not
drop this index.

**The landing page needs `ix_r1_matched_date` on
(run_id, matched, business_date, store, amount).** Every headline figure is
"matched vs not, over a date range". Measured on 660,150 lines: the verdict
aggregate **3,092 ms → 225 ms**, and the by-reason breakdown **5,496 ms →
435 ms**. `matched` precedes `business_date` so the 205 unmatched rows are a
seek, not a scan of the month, and `amount` is carried so the SUM is answered
from the index. Do not drop this index either.

**The by-reason breakdown must never pull the month into Python.** Building the
duplicate-twin set by fetching all 660,150 lines took 8.5 s; asking the
database for only the keys the unmatched bills actually use takes 435 ms.

**There is no Home page** (removed 2026-09-10 — it duplicated Summary with
different formatting). `app/Summary.py` is the Streamlit entrypoint and the
landing page; the nav is **Summary, Float, Manual matches, Run, Masters**
(as of 2026-09-17 — see the consolidation note below; Float added
2026-09-13, directly below Summary, per instruction — see §6.15; there is no
Upload page any more — see the same section), which is why the page files
carry those numbers. "Data available to read" lives on **Run**, where it
answers "can I run, and against what". Configuration values (tolerance, MOP
classes, sources configured) live behind a fold on **Masters** under
Admin — they are settings, not information an operator needs on opening the
app.

#### Four pages collapsed into Summary, and Manual matches became the entry point (2026-09-17, on instruction)

**Digital sale, Bill match, Transactions and Exceptions are gone from the
nav entirely.** Their reporting content is now sections on **Summary**,
stacked top to bottom — never tabs, on instruction — so the whole picture
reads on one page:

1. The tile ribbon (unchanged, §6.9d/§6.9d's tile-ribbon subsection below).
2. The store-wise grid (unchanged, §6.9d).
3. **"Matched"** — collapsed by default. As of 2026-09-19, **matched bills
   only** — `queries.matched_bills_page/_export` (see the 2026-09-19 note
   below) — with its own "export all in filter" button. A matched-
   transactions table sat here between 2026-09-17 and 2026-09-19; it never
   belonged next to a bill-side table and was removed, on instruction. It
   is not "Exception transactions" (item 4) with a different name — that
   table is untouched, its own count/value/export, never merged with this
   one.
4. **"Exceptions"** — expanded by default, since this is the work queue.
   Two plain tables, never merged into one count: **Exception bills**
   (types in `queries.NOT_RECEIVED_TYPES` — `bill_not_matched`,
   `ambiguous_edc_other`, `no_source_report`; columns date, store, bill_no,
   mop, amount, status, remark, remark_by) and **Exception transactions**
   (types in `queries.RECEIVED_NOT_BILLED_TYPES` plus
   `original_txn_not_found` and `store_ambiguous` — the two types that are
   "neither" under §6.9a's risk split fold in here since their natural shape
   is gateway/transaction, not bill/mop; columns gateway, transaction_id,
   date, store, amount, payment_mode, status, remark, remark_by). Each
   table's header carries its own row count and total value. The **Show**
   filter (Open / Under investigation) is the one page-level filter kept
   from the old Exceptions page, applied to both tables at once; closed and
   auto-resolved items move into a bottom expander, itself split into two
   tables for the same never-merge reason, never made unreachable (rule 4).
   `queries.latest_remarks()` is a new bulk lookup (one query for a whole
   page of exception keys, never one per row — §6.14) that supplies the
   `remark`/`remark_by` columns. **This view is plain and read-only, on
   instruction — "no drill-through."** The old Exceptions page's grouped-by-
   type layout, severity stripes, bulk remark/status actions and per-
   exception detail panel are gone with the page, not rebuilt here; adding a
   remark or changing an exception's status has no UI any more as of this
   date.

**Digital sale's own reporting (the verdict sentence, the risk-split tiles,
the "amount not received by store" cut) has no replacement.** It is
superseded by the plainer Exception bills/transactions tables above, not
reproduced in the same shape — there is no verdict sentence, no risk-split
call-out and no by-store grouping anywhere in the app after this date. Every
description of "the Digital sale page" elsewhere in this file (§6.9c
describes what USED TO be there) is historical.

**Manual matches is now the data-entry page, not only the register.** It
absorbed everything that made a bill match happen, all of which used to live
on the now-removed Bill match page: the open-bills table with the inline
paste-to-match cell (§6.9b), the date-lag-matches information expander
(§6.9b), Suggested matches (§6.9b-ii item 3) and Bulk upload (§6.9b). The
audit register and reversal (below this section on the page) are unchanged.
Every cross-reference elsewhere in this file to matching "happening on Bill
match" now means **Manual matches** — the mechanism is byte-for-byte the
same, only the page it lives on changed.

**The two-sided controls are shown on both pages that need them, computed
independently.** Summary shows bills (open+closed=total) and transactions
(mapped+unmapped=total) once, above the Matched/Exceptions sections, visible
regardless of which is expanded. Manual matches shows its own bills
open+closed=total beside the open-bills table, since that is the number the
entry-side work is against. Both read the same `queries.bill_match_summary`/
`gateway_txns` sources, so they can never disagree.

**Everything here still cascades immediately, no re-run required (§6.9b-ii
item 1) — nothing about that changed by moving where it is read from.**
`queries.bill_match_page/_summary/_export`, `gateway_txns.with_bill_mapping`
and `queries.exception_rows` were already overlay-aware before this move;
Summary's new sections and Manual matches' open-bills table simply call the
same functions the removed pages called.

**Superseded by the above — kept as the historical record of how the nav
got here, not as current fact:** the "Two pages swapped identities on
2026-09-12" note and the "old Transactions page split into two on
2026-09-12" note immediately below both describe page shapes that existed
between 2026-09-12 and 2026-09-17 and no longer do. `app/pages/2_Bill_match.py`,
`3_Transactions.py`, `4_Exceptions.py` and `6_Digital_sale.py` were deleted
outright, not hidden; the surviving pages were renumbered so the nav has no
gaps (`1_Float.py`, `2_Manual_matches.py`, `3_Run.py`, `4_Masters.py`).

**There is no source-file "Upload" page (removed 2026-09-13, on
instruction).** All source data (POS, Paytm, Axis, Bajaj, PTC, CMS) is
dumped into the numbered folders on disk (§4) and picked up from there by
the **Run** page — there is no manual source-file upload path in this
system, and there never was meant to be one; `app/pages/8_Upload.py` and
`mop_reco/uploads.py` (a generic validated-store-and-record uploader for
any of those sources) were removed entirely, not merely hidden. The word
"upload" still appears twice in this system, and both are kept, each moved
onto the page that uses it rather than standing as its own nav item: bulk
manual matches (**Manual matches**, since 2026-09-17 — see above; §6.9b)
and store float remarks (**Float**, §6.15). Neither reads or writes a
reconciliation source file — one records manual-match assertions, the other
records remarks — so neither is a back door into the removed source-file
path.

**Two pages swapped identities on 2026-09-12, on instruction (superseded —
see above).** "Summary" (this section's digital-sale/exceptions view,
`q.verdict`, the risk-split tiles, "amount not received by store") moved
from `app/Summary.py` to `app/pages/6_Digital_sale.py` and was called
**Digital sale** — its own content and logic were unchanged at the time,
only the file, nav label and page title moved. What was
`app/pages/4_Store_sheet.py` (§6.9d, the daily store-wise sale and cash
sheet) became the new `app/Summary.py` — the entrypoint and landing page —
and is now called **Summary**, which is still current. Every reference to
"the Summary page" or "the Summary headline row" in §6.9c below describes
the **Digital sale** page's content as it existed before this rename and
before Digital sale itself was removed 2026-09-17; every reference to "the
Store sheet page" in §6.9d describes what is now the **Summary** page,
still accurate.

**The old "Transactions" page split into two on 2026-09-12, on instruction —
it showed bills, not gateway transactions, which was confusing (superseded
2026-09-17 — both pages named below were later deleted; kept as the
historical record of why Bill match and Transactions existed as separate
pages at all, and where their column shapes and control logic came from).**

- **Bill match** (`app/pages/2_Bill_match.py` at the time, since deleted)
  was the bill side: Open bills (no transaction found) vs Closed bills (a
  transaction is matched — by the engine **or** by a person). Because the
  manual-match overlay never writes to `r1_bill_lines` (§6.9b — "the engine
  runs untouched"), a manually-matched bill still reads `matched=False`
  there; "Closed" is computed by checking the bill's number (globally
  unique, §5) against the small set of currently-active manual matches,
  never by pulling a date range of bill lines into Python to answer it.
  `queries.bill_match_summary/_page/_export` are the sanctioned readers —
  never read `R1BillLine.matched` alone expecting it to reflect a manual
  match. These functions are unchanged and now called from Summary's
  Matched section and Manual matches' open-bills table instead.
- **Transactions** (`app/pages/3_Transactions.py` at the time, since
  deleted) was the gateway side: every Paytm, Axis and Bajaj row in the
  range, mapped (a bill is matched against it, system or manual) or
  unmapped (none is). `mop_reco.gateway_txns` is the engine —
  `gateway_pool_for_range()` reads the per-gateway, per-date warehouse
  Parquet already used by the manual-match window lookup (§6.14:
  precomputed, bounded to the dates asked for, never a raw file), and
  `with_bill_mapping()` attaches the mapped/unmapped verdict from the same
  two sources (system-matched `r1_bill_lines`, active `manual_matches`) that
  Bill match read. Mapped + Unmapped = Total, exactly, by construction. This
  module is unchanged and now called from Summary's tile-ribbon two-sided
  control and its Matched section.

Both pages carried the same two-sided control shape (bills: matched +
unmatched; transactions: mapped + unmapped), which is why Summary's own
replacement controls (above) still show both, independently, the same way.

#### Summary reshaped again, 2026-09-19 — collapsible sections, a reformatted Matched table, and the two-sided control as a visible strip

**Every major section carries its own +/- toggle**, not a plain
`st.expander`: `store_detail`, `matched` and `exceptions` each have a
compact button before the section title (`_section_toggle()` in
`Summary.py`), and the header always shows the row count (and value, where
the section has one) even while collapsed. State lives in plain
`st.session_state` keys (`sm_open_<name>`), the same non-widget-key pattern
the sidebar's own filters use (§6.14 below) — so a filter change or a
switch to another page and back never resets which sections are open.
Default on first load: **Exceptions open, Store-wise detail and Matched
closed.** "Expand all" / "Collapse all" (kept at the top of the page, now
also driving these three toggles, not only the per-date multiselect) set
all three at once.

**A collapsed section skips its own expensive work, not just its
rendering.** Store-wise detail's row count (`len(shown)`) is already free —
no query, the frame is already in memory — so the grouped-table build and
the `display()` formatting over up to several thousand rows only happen
when opened. Matched's header count comes from `bill_summary` (already
computed for the two-sided-control strip below, so no extra cost), and its
paged bill query only runs when opened. Exceptions is the one section where
the header count needs its own query even when collapsed —
`queries.exception_counts()` (new, 2026-09-19) is a plain COUNT+SUM per
bucket, never the full per-row build with JSON-parsed detail and the bulk
remarks join that `exception_rows()` does; that heavier read only happens
once the section is actually open.

**The "Detail" sub-heading above the raw per-store-per-date table is
gone**, on instruction — the section header above it already says what the
table is, and repeating it inside was noise. The table itself, and the
drill-down beneath it, are unchanged, just no longer separately titled.

**The Matched section is bills only, reformatted, and paged with the
transaction id shown in full.** `queries.matched_bills_page/_export`
replace `bill_match_page/_export` here specifically, returning date, store,
bill_no, mop, amount, gateway, transaction_id, txn_date, lag, match_source
— `lag` is `txn_date − bill_date` in days: read straight from
`R1BillLine.date_diff_days` for a Tier-1 match, always 0 for Tier 2 (its
own candidate pool is same-date by construction), and for a manual match,
parsed from `ManualMatch.validation_detail`'s own `date_lag_days` rather
than assumed to be 0 — rule 5 forbids guessing it. A non-zero lag is
flagged with a "⚠" prefix in the displayed cell (the same pattern the Float
page uses for a negative float) rather than a per-cell background colour,
since Streamlit's dataframe grid has no supported hook for the latter.
Column-click sorting and a frozen header are the grid's own default
behaviour, not something configured here.

**"Bills and transactions" is a permanent strip, not a section** — two
small tables (Bills: Closed/Open/Total; Transactions: Mapped/Unmapped/
Total), directly below the "Build the Excel sheet" button, always visible
regardless of any section's open state. Each **Total** row is the
arithmetic sum of the two lines above it, displayed as that sum; it is also
checked against its own independently-queried total (`bill_summary`'s own
`"total"` bucket for bills; `len(gateway_pool)` for transactions), and a
disagreement shows as a red **MISMATCH** naming the difference rather than
a total that merely looks right. This replaced the older simple
"✓ Bills: open (...) + closed (...) = total (...)" caption line, which said
the same thing with less visibility.

#### The two store-wise tables became one — a parent table plus a per-date expander, no glyph (2026-09-20, revised the same day)

**First shipped, then reverted the same day: removing the date filter from
Summary's sidebar.** An earlier instruction removed Summary's own From/To
fields (reasoning: the merged table shows every loaded date as its own row
anyway, so a filter seemed redundant) — that instruction was explicitly
**withdrawn** a few hours later. Summary's sidebar is back to the same
shape every other page uses: **From, To, Store, Clear filters, Refresh** —
`dashboard_common.sidebar()` has no `show_dates` parameter any more (it
existed only for the few hours this was tried) and no page opts out of the
date filter. Do not re-add a per-page date-filter opt-out without a fresh,
explicit instruction — this was tried once and reversed.

**First shipped, then fixed the same day: a "+"/"−" glyph prefixed onto the
Date cell.** The first version of the merged table spliced a date's own
store rows directly into the SAME dataframe as its `ALL STORES` parent row,
with `"+ 2026-07-01"` / `"− 2026-07-01"` marking collapsed/expanded state on
the date cell itself. **In the browser this glyph rendered as a floating
box that overlapped the header row and the first data row** — a real
rendering defect, not a cosmetic quibble — and was removed entirely, on
instruction: *"If Streamlit's dataframe cannot do a clean row-click expander
at this scale, do NOT fake it with a glyph column. Instead render the
parent rows as the table, and put each date's store rows in a
`st.expander` below the row that opens on selection. Working and plain
beats clever and broken."*

**Current, working shape:** the on-screen table is the PARENT (`ALL
STORES`) rows only — `ss.build_grouped_table(shown, summary_by_date_shown,
[])`, always called with an empty expansion list, so no child row is EVER
spliced into this dataframe any more. `A Date` shows the plain date, no
prefix, for every row. Clicking a parent row (`st.dataframe(...,
on_select="rerun", selection_mode="single-row", key="sm_grouped_table")`)
toggles that date in `st.session_state["sm_open_dates"]` (a plain,
non-widget key holding a Python `set`, mutated in place — the same pattern
`sm_open_<section>` already uses, surviving a filter change, a page switch
and a re-run). For every date now in that set, its own store rows render in
their own `st.expander(f"{date} — N store(s)", expanded=True)` **below**
the parent table, each with its own `st.dataframe(..., key=f"sm_store_rows_
{date}")` — clicking a row inside ONE of those still opens the same
drill-down (digital bills / cash bills / PTC rows / CMS credits) that used
to live under the old flat table, self-contained per expander rather than
needing a shared lookup back to a spliced dataframe.

**A collapsed date is genuinely not built** — the for-loop that renders
expanders only iterates `sorted(d for d in all_dates if d in open_dates)`,
so a date's own `shown[shown["date"] == d]` slice, its `display()`
formatting and its own `st.dataframe` call never run for a date nobody
opened.

**Local Expand all / Collapse all, beside the table's own heading, separate
from the page-level ones.** The three page-level buttons (top of page) now
drive ONLY the three section toggles (Store-wise detail / Matched /
Exceptions, later a fourth — Data quality, see below); a second pair sits
on the SAME LINE as "Store-wise sale and cash — by business date",
right-aligned, and drives only `sm_open_dates` (`set(all_dates)` / `set()`).
These used to be the same buttons; splitting them followed from making the
per-date control local to the table rather than a page-wide concept. The
caption that used to explain the click target ("Click a date's ALL STORES
row to expand or collapse it...") is gone too, on instruction, now that the
buttons make the interaction obvious without it.

**Two controls, both asserted and shown, quiet only when they hold** (both
kept from the first cut of this table, unaffected by the expander rewrite):

1. **Every parent row equals the exact sum of its own child rows, column by
   column, for every column C through N** — `ss.verify_date_rows_equal_
   store_sums()` carries `opening_balance` (H), `closing_balance` (M) and
   `float_amount` (N) alongside the original C, D, E, F, G, I, J, K, L; all
   three are stock/derived values, but `summarise_by_date()` builds them
   the identical way — summing each date's own store rows via `groupby` —
   so the same per-date parent-vs-children assertion is exactly as
   meaningful for them as for the flow columns. A disagreement names the
   date, the column and the difference; nothing is hidden (rule 4).
2. **The parent (by-date) rows, summed, agree with the tile ribbon's own
   totals above them** — Total sale (C), Digital sale (D), Cash sale (E),
   Digital receivable (G) and Cash deposit (I) as plain sums; Cash
   receivable via `ss.net_cash_receivable()` on the SAME parent rows (sum I
   − sum J, floored — matching how the tile itself is computed, never a sum
   of pre-floored per-date values); Closing balance (M) as the LAST parent
   date's own closing, never summed across dates, since M is a stock.
   Measured on full July: every one of the 7 checks agrees to the rupee
   (difference 0.00 on each). A mismatch shows the tile's name, the sum of
   parent rows, the tile's own figure and the difference, never silently.

**Collapsed-state row count is exactly one row per loaded business date,
plus the TOTAL row — 32 on full July (31 dates + TOTAL)** — confirmed both
programmatically (`len(ss.build_grouped_table(sheet, summary, []))`) and
live in the browser.

**Automated clicks on this table need the row's own selection checkbox,
not the visible text — a browser-automation quirk, not an operator one.**
Confirmed the hard way verifying this live: clicking the "A Date"/"B Store"
text repeatedly left `event.selection.rows` empty on the Python side even
though the row appeared to highlight on hover, while a click on the small
checkbox Streamlit's grid renders at the row's left edge registered every
time. A person clicking with a mouse does not hit this — Streamlit's own
grid treats a normal click anywhere on the row as selecting it; this only
bit pixel-coordinate-driven automation.

#### Two other Summary changes the same day: the Bills/transactions strip's position, and the performance rework

**The "Bills and transactions" strip already sat ABOVE "Store-wise
detail"** when asked to move it there — the instruction described the
opposite current order, but the code's actual order (verified by reading
it, then confirmed live) already matched the requested end state:
tile ribbon → Data quality → Build-the-Excel-sheet → **Bills and
transactions** → **Store-wise detail** → Matched → Exceptions. No file
change was needed for this part; noted here so a future read of this
section isn't confused about why no diff exists for it.

**Performance: the store-day sheet moved from view-time to run-time
(2026-09-20).** Summary's own page load used to call `store_sheet_source.
build()` — the same engine the Excel export and a couple of lazy sections
still use — which re-read PTC/CMS/opening-balance/POS for the WHOLE
requested range on EVERY page load, no matter how many times the range or
store filter changed. Measured on the one month of data this machine has
loaded (docs/PERF_BASELINE.md): **33.4 seconds, 486 MB peak**, dominated by
an ORM-level `r1_bill_lines` pull (17s for 660,150 rows), a 222 MB `.xlsb`
read materialised as 1.49M Parquet rows (11s), and — this is the one that
does not stay flat as more months load — a full re-ingest of the raw CMS
zip file rather than a warehouse read (2.3s on one month; the CMS source
folder holds one file per month, so this scales with how many months are
configured, not with the view's own filter).

Fixed with the pattern the operator's own instruction named: **"the Run
stage must write persisted aggregate tables ... the Summary page READS
these."**

- **New tables, `store_day_agg` and `date_total_agg`** (`mop_reco.db`) —
  one row per store per business date, and one row per business date
  (`ALL STORES`), for columns A..N **minus F (digital received) and G
  (digital receivable)**. F/G are deliberately excluded: CLAUDE.md 6.9b-ii
  item 1 requires a manual match to move G with NO re-run, and F is the one
  column a manual match actually changes — storing it would go stale the
  moment someone recorded a match. Indexed on `(run_id, business_date,
  store)`, `business_date` and `store` individually, per the instruction.
  Also carries a precomputed `defect_b` boolean (K < 0 that store-day) —
  see the Data quality note below.
- **`store_sheet_source.persist_aggregates(session, run_id, date_from,
  date_to)`** runs the existing `build()` ONCE, writes both tables (replace,
  never append, so a replay of the same run_id cannot leave duplicates),
  and returns a report including `static_bundle_json(bundle)` — everything
  in a `build()` bundle that is NOT store-day-shaped and NOT
  overlay-dependent (the 8 lettered controls, the run's own notes, the CMS
  excluded/contribution/other-types breakdowns, unresolved pickups — none
  of which a manual match or a store filter can move) serialised to plain
  JSON. Called from `runs.run_reconciliation()` as its own stage
  (`persist_store_day_agg`), right after R1 results are persisted; the
  report — including the static payload — is stored on `Run.notes` under
  `"store_day_agg"`.
- **`store_sheet_source.load_fast(session, run_id, date_from, date_to,
  store=None)`** is what Summary's `load()` calls now: an indexed SELECT on
  `store_day_agg`/`date_total_agg` with the date range and store filter
  pushed into the SQL WHERE clause (never a pandas post-filter over an
  already-cached whole-range frame, which is how the store filter used to
  stay cheap before this — now BOTH the range and the store filter are
  cheap the same way). F is joined in live via `_live_digital_received()`,
  the one function on this page's hot path that still reads
  `r1_bill_lines` — **aggregated in SQL, not pulled into pandas**: an
  earlier version of this function pulled every matching bill-line row and
  summed them in pandas, measured at 16.3s for 660,150 rows (the ORM
  row-materialisation cost, not the index, dominated); it is now TWO
  grouped SQL sums — `SUM(amount) WHERE matched=1` (uses the existing
  `ix_r1_matched_date` index, leading on `matched`, directly) plus
  `SUM(amount) WHERE matched=0 AND bill_no IN (<small active-manual-match
  set>)` — added together. Falls back to the slow `build()` path when
  `store_day_agg` has no rows for the run (a run from before this existed);
  this is the one place on the page that can still call `build()`, and only
  for a legacy run.
- **`store_sheet_source.load_static(run)`** reads the JSON payload back off
  `Run.notes` — controls, notes, excluded/contribution/other_types,
  unresolved pickups — with no query and no file read. Same legacy-run
  fallback (`None` when the key is missing) as `load_fast()`.
- **Data quality became the fourth collapsed-by-default toggle section**
  (`store_detail`/`matched`/`exceptions` already were one each —
  `data_quality` joins them), on the SAME instinct as the rest of this
  rework: it used to be an unconditional `st.expander` that called
  `data_quality_rows()` (a CMS-file walk) on every page load just to decide
  whether to show a caption. `store_sheet_source.defect_b_summary()` — a
  plain `COUNT`/`SUM` on the precomputed `defect_b` column, the identical
  "cheap header, expensive body only when opened" shape
  `queries.exception_counts()` already used for Exceptions — answers the
  header regardless of whether the section is open; the CMS-joined table
  (pickup codes, slip numbers via `data_quality_rows()`) only builds once
  it actually is.

**Measured, on the one month of data this machine has loaded: 33.4s / 486
MB → 1.7s / 20.3 MB — about 19x faster and 24x less peak memory** (see
docs/PERF_BASELINE.md for the full stage-by-stage breakdown, before and
after, plus what this pass did NOT get to — server-side pagination for the
Exception bills/transactions tables, and a precomputed `match_summary`
aggregate for the Bills-and-transactions strip). **What this cannot yet
say: the actual numbers at 3x or 12x the current data, since this machine
has only one month of source data loaded** — the new path's cost is an
indexed SQL scan bounded by the FILTERED result size rather than by how
much is loaded in total, which is the architectural property a full year
needs, but that property is asserted by construction here, not verified at
that scale. Recommend loading a second and third month (real or a
synthetic replay) and re-running `scripts/perf_summary_sheet.py` before
treating full-year performance as closed.

**Correctness was checked column by column, not assumed from the
architecture change** — `load_fast()`'s sheet and `build()`'s sheet were
compared directly for every one of columns C through N on the full month:
every column agreed exactly, including the TOTAL row's closing balance
against the long-standing acceptance figure (Rs 1,64,60,853.60, §7). Also
tested: a manual match on an engine-unmatched bill moves G on the very next
`load_fast()` call with no call to `persist_aggregates()` in between —
the cascade rule holds under the new architecture, not just the old one.

**A run from before this existed still works** — `scripts/
backfill_store_day_agg.py [run_id]` runs `persist_aggregates()` against an
existing run's own loaded date range and writes the SAME `Run.notes`
payload `runs.run_reconciliation()` would have, so `load_fast()`/
`load_static()` stop falling back to the slow path for it without needing
a full ~16-minute re-reconciliation. Every run from here on writes this
itself, as part of the normal run.

#### Summary reshaped a third time the same day: Bills/transactions moved back below Store-wise detail, two captions and the Data-quality section removed, dates reformatted everywhere (2026-09-20, later the same day — supersedes the position note just above)

**The Bills-and-transactions strip moved BELOW Store-wise detail, reversing
the paragraph immediately above.** That paragraph was accurate for the
order that existed a few hours earlier the same day; a later instruction
the same day asked for the opposite — tile ribbon → Download-the-sheet
button → Store-wise detail (with its own Expand/Collapse) → Bills and
transactions → Matched → Exceptions — and the code was moved to match. Two
same-day position changes for the same block is confusing to read back
later, which is why both are recorded rather than silently overwriting the
earlier note.

**Two captions deleted outright, on instruction, nothing else changed
around them:** the "Five tabs: By date, By store and date..." paragraph
under the "Download the sheet" button, and "Bills and transactions are
different populations..." under the Bills-and-transactions heading. Both
were explanatory prose the operator no longer wanted on screen; the
features they described (the five-tab Excel export, the two populations
never being expected to reconcile against each other) are unchanged.

**The Data-quality toggle section is REMOVED from Summary entirely and its
content now lives on Float, merged under Float's own existing
deposit-vs-credit heading.** Not a deletion of data — `store_sheet_source.
defect_b_summary()` and `data_quality_rows()` are unchanged, only which
page calls them changed. Float's page (§6.15) already had its own
"Negative float" anomaly (deposit > opening); it now sits under a shared
**"Data quality"** heading alongside a second, separate expander for
credits exceeding deposits (Defect B) — two different anomalies, one place
to look, exactly the "merge them there under one heading" instruction.
Since Float's own `_load_grid()` still calls the slow `store_sheet_source.
build()` (out of scope for this change — Float was not migrated to
`load_fast()`), it now also returns the raw `bundle.sheet` alongside the
float grid, because `data_quality_rows()` needs `cash_receivable`, a column
`build_float_grid()` does not carry into its own narrower column set.
Verified live: the count and value are byte-for-byte the acceptance figure
this file already recorded for this section (63 store-days, Rs 80.16 L) —
unchanged by the move, as expected, since nothing about the underlying
computation changed.

**One shared date-display helper, `formatting.display_date()`, DD-MM-YYYY
everywhere — never ISO, never formatted ad hoc per page.** Applied to: the
store-wise detail table's "A Date" column and its per-date expander
headings, the Matched-bills table (replacing an ad hoc
`pd.to_datetime(...).dt.strftime(...)` that happened to already produce the
same format — now going through the one shared function instead), the
Exception bills/Exception transactions tables, the Manual matches page's
date-lag expander/By-bill/By-transaction tables/Manual-match bill
report/Refund report, and the Float page's tiles, grid, negative-float and
data-quality tables. Dates are stored as a date/string type internally
exactly as before; only the display changed. A second helper,
`formatting.blank()`, is the shared null-safety function — see §6.9c-ii for
the "nan" bug it fixes.

#### The sidebar, and which filters are global (2026-09-11)

Sidebar, top to bottom: nav · divider · **Filters** (From, To, Store — every
page, no exceptions; see §6.14's note on the 2026-09-20 attempt to drop this
for Summary specifically, tried and reversed the same day) · the run
stamp, small and muted · and at its foot "Working as: <name>" with a change
link. `dashboard_common.sidebar()` renders all of it and every page calls it
once, before the page body — which is the point of the change: with the three
controls out of the page, **the verdict line and the tiles are visible without
scrolling.**

**Only date-from, date-to and store are global**, because only those three mean
the same thing on every page. A filter belonging to one view — status, MOP,
exception type, gateway, the search box — **stays on that view**, on one
horizontal row above its table. Putting a page-specific filter in the sidebar
would make it look global and silently apply nowhere else.
`tests/test_dashboard_chrome.py` asserts both halves of that split.

**The Run page's own date range is not a view filter.** It is the run
parameter — what gets reconciled — so it stays on the page, captioned to say
so. Its defaults come from the loaded dates; they were hardcoded to
`date(2026, 7, 1)`/`date(2026, 7, 31)`, which broke rule 8.

**Filters persist in PLAIN session keys, never widget keys.** Streamlit
garbage-collects the state of a widget that is not rendered in the current
run, so a `key=` alone does **not** survive a page switch — setting July on
Summary and opening Transactions silently reverted to the default. The widgets
carry their own `w_flt_*` keys seeded from the canonical `flt_*` values, and
the canonical value is written back on every run. `sidebar()` never calls
`st.rerun()` for a filter change, so changing one refreshes the page in place
without losing scroll position.

**No default date, anywhere (revised 2026-09-11 — supersedes the "most
recent reconciled date" default and the range presets below, both removed
on the operator's instruction: "delete the preset radio buttons entirely",
"NO default date").** `From` and `To` open **empty**. Empty means every
reconciled date that has been loaded — `sidebar()` resolves the empty
widgets to the run's actual first/last loaded date before returning, so
every caller still gets concrete dates to query with, but the **widgets
themselves stay visibly empty** rather than being silently pre-filled with a
date the operator never chose. `st.date_input(value=None)` is what makes an
empty widget possible (Streamlit ≥ 1.57): it renders blank and returns
`None` until a date is typed. What "empty" resolved to is written beside the
page title instead (see below), so it is never a guess what is on screen —
this is the one thing that changed to keep rule 8 ("no hardcoded dates
anywhere") honest: a computed default *is* a hardcoded date the moment
nobody chose it that day.

**The active range — and now the store filter too — is written beside the
page title in words**, e.g. "1-31 July 2026, all stores" or "20 July 2026,
ABD only". `range_label()` builds the date phrase; `_write_range_label()`
appends the store. With no default date, this line is the ONLY place that
says what an empty From/To actually resolved to.

**"Clear filters" and "Refresh" sit under the three filters, side by side
(2026-09-11).** Clear filters resets From, To and Store back to empty/"All
stores" in one click — written to both the canonical keys and the widgets'
own keys (see widget-state trap 1 below), but that write happens in a
**deferred pending-flag block at the top of `sidebar()`**, not inside the
button's own click handler — see widget-state trap 3, the one that actually
crashed the page before this was caught. **Refresh re-reads the current
results from the database and
redraws the page — it does not re-run the reconciliation.** It calls
`st.cache_data.clear()` (every page's cached `load()`, not just the current
page's) and reruns; a full reconciliation only happens from the **Run**
page, on purpose, and the button's own help text says so, so it is never
mistaken for one.

**Three Streamlit widget-state traps, all of which shipped as visible bugs
before being fixed. All three are now tested.**

1. **Setting a canonical key is not enough to move a widget.** Streamlit
   prefers a widget's existing state over a `value=` argument — this first
   showed up in the now-removed range presets (a preset that set only
   `flt_date_from`/`flt_date_to` had the date inputs write the OLD dates
   straight back on the same run) and the same fix applies to **Clear
   filters** today: it must also write `w_flt_from`/`w_flt_to` (to `None`),
   not only the canonical keys, or clicking it would appear to do nothing.
2. **A widget key must be tied to its option set.** The store selector under
   the store-wise breakdown kept a store from the previous range's list:
   Streamlit reset the selection server-side while the client went on showing
   the old label, so the box read "KHR - 1 bills, Rs 1,221" above KPH's bills.
   Its key now includes the date range and store filter, so a changed
   population gives a fresh widget. The Store sheet page's own date-expansion
   multiselect (see §6.9d) uses the identical pattern for the same reason.
3. **A widget's own session_state key cannot be written AFTER that widget
   has been instantiated in the current script run — Streamlit raises
   immediately, `st.rerun()` afterward or not.** Clear filters originally set
   `st.session_state["w_flt_from"] = None` etc. from inside the button's own
   click branch, which sits AFTER the `st.date_input(..., key="w_flt_from")`
   call earlier in the same `sidebar()` run — clicking it crashed the whole
   page with `StreamlitAPIException: st.session_state.w_flt_from cannot be
   modified after the widget with key w_flt_from is instantiated`. The fix is
   the standard deferred-reset pattern: the button only sets a
   `_pending_clear_filters` flag and calls `st.rerun()`; the actual
   `w_flt_from`/`w_flt_to`/`w_flt_store` reset happens at the very TOP of
   `sidebar()`, checked and consumed before any of that function's own
   widgets are created — so by the time the widgets run, the keys are free to
   write again. Any future reset-style button in this sidebar must follow
   the same shape, not write directly to a widget key from below it.

#### The operator name is asked at the point of the action

Never up front. There is no form standing in the sidebar demanding a name
before anything can be looked at, and **no red Continue button** — red is
reserved for critical state (see the `primaryColor` note above).

- `require_operator(action, key)` renders a small inline prompt where the
  action is, and returns "" until a name is given.
- `confirm_with_operator(clicked, action, key)` is the pattern the pages use:
  nothing is asked until a button is actually pressed, and the click is
  remembered so the action completes as soon as the name is given rather than
  making the operator press the button twice.
- The sidebar footer only *reports* — "Working as: <name>", or "Not signed in"
  — and asks nothing.

**A page importing a helper that no longer exists crashes the moment it is
opened, and nothing catches it but opening every page.** `5_Upload.py` imported
`get_session_cached` and `require_operator` when neither existed and had been
broken since it was written. `test_dashboard_chrome.py` now checks every page's
`dashboard_common` imports against what that module actually defines.

**Red is reserved for critical state.** Streamlit's default `primaryColor` is
a bright red that it paints onto every `type="primary"` button, which made a
routine "Continue" button read as an alarm. `.streamlit/config.toml` sets
`primaryColor` to the action blue `#2471a3`; red belongs to the `action`
severity and the state tiles only.

**Consequence that must stay visible:** what a view shows is as of the last
run, not as of now — so the **run timestamp is displayed in the summary
header**, never buried.

### 6.15 Float — the store cash float and whether it is sitting too high (2026-09-13, formula and threshold corrected 2026-09-15)

**Float = opening balance LESS the cash deposited that business date. It is
NOT the closing balance.** The first build of this page read Summary's
closing balance (column M) and called it float — wrong, and caught by the
operator: the deposit made on day D is the PRIOR day's cash, so float
measures what the store held BACK instead of banking, not what it holds at
day's end (which also includes that same day's own cash sale, not yet even
eligible for deposit). `mop_reco.float_tracker.build_float_grid()` now reads
Summary's own `float_amount` column instead — Summary's column N,
`= H − I` (§6.9d; `I`, cash deposit, is carried POSITIVE since 2026-09-17,
so `H − I` equals opening minus the deposit's magnitude — before that date
the same real value was reached as `H + I` with `I` stored negative) —
and still never recomputes cash movement itself; the bug was reading the
wrong ALREADY-COMPUTED column, not an arithmetic error of this module's own,
and this module needed no change at all when I's own sign was later
corrected, since it only ever reads N and never I directly for its own
arithmetic. Verified against four real 2026-07-31 rows before anything else
changed: ABD (69,550 − 61,550 = 8,000), AGT (64,766 − 46,120 = 18,646), AJL
(49,143 − 37,975 = 11,168), ALC (1,03,576.32 − 88,400 = 15,176.32).
`app/pages/1_Float.py` is the page, sitting directly below **Summary** in
the nav (§6.14), on instruction.

**The threshold is configurable and separate from R3a's own threshold.**
`config.yaml`'s `r3a.high_cash_threshold` (Rs 2,00,000, §6.6) answers a
different question — is the computed cash book internally consistent.
Float's own `float_tracker.float_threshold` (Rs 10,000 default — corrected
2026-09-15 from an earlier, much higher default that must not reappear
anywhere in the code) is the operator's day-to-day float-management
threshold, deliberately separate and lower, with an optional per-store
override in `float_tracker.threshold_overrides` — the supplied store master
carries no threshold column of its own (§5), so an override is never
guessed from it; a store gets a different threshold only by an explicit,
named entry in config, per rule 5. **`high_float` is float above threshold —
never closing balance above threshold — and `days_held_high` is the same
streak recomputed on the float basis.**

**The grid, one row per store per business date, in this exact order:**
store | business_date | day | opening | cash sale | deposit | **float**
(opening − deposit) | float threshold | high float? | days held high | cash
received | closing balance | remark | remark_by | remark_at. Opening, cash
sale, deposit, cash received and closing balance all stay on the grid
alongside float so the arithmetic is checkable by eye, row by row — closing
balance is labelled plainly as "Closing balance", never as float again.
`cash_deposit` is shown **positive** here — since 2026-09-17 that is simply
the sheet's own stored value (§6.9d), not a display-side flip.

**A negative float is shown as-is, never floored and never excluded
(rule 4).** `deposit > opening` means either a catch-up deposit (more than
one day's cash banked at once) or a deposit attributed to the wrong date —
either way it is a real number, not an error to zero out. The grid's Float
cell carries a "⚠" flag on a negative row; `float_tracker.negative_float_rows()`
lists every such store-date (store, business_date, opening, deposit, float,
most-negative first), and the Float page shows a count-and-value caption
under the tiles — *"N store-day(s) show a deposit larger than the opening
balance (catch-up or misdated deposit)"* — with its own drill-down expander
and CSV export, exactly like the Summary page's Data quality section
(§6.9d) treats a different kind of anomaly the same way.

**`days_held_high` is a per-store streak, computed in `build_float_grid()`**:
consecutive business days (in the loaded range, in order) this store's
FLOAT — not closing balance — has stood above its threshold, up to and
including this date — resetting to 0 the day it drops back at or below
threshold. It does not carry across a gap in the loaded range and does not
leak between stores.

**The weekend/holiday auto-remark is computed, never stored.** A high float
is *expected* — no bank pickup — on a Sunday, the 2nd or 4th Saturday of the
month, or a listed public holiday, so these get an automatic explanation
rather than sitting in the unremarked queue:

- **Sunday or the 2nd/4th Saturday of the month** → auto-remark "Weekend",
  `remark_by = "system"`. The 2nd/4th Saturday is found by
  `second_and_fourth_saturdays()` **counting which Saturday-of-the-month each
  one is** (`monthrange` + a weekday scan), never a fixed date range — a
  month starting on a Saturday would otherwise miscount (verified: August
  2026 starts on a Saturday, and its 2nd/4th are the 8th/22nd, not the 1st and
  15th a naive "days 8-14" range would give).
- **A listed public holiday** → auto-remark "Holiday", via the same
  `masters._HOLIDAY_SET` that §6.5's due-date working-day logic already uses
  — not a second calendar. No holiday calendar has been supplied yet
  (`config.yaml`: `holiday_calendar: null`), so "Holiday" cannot fire today;
  the day one is loaded, this starts working with no code change.
- Neither an auto-remark nor an operator's own remark is a row that gets
  double-counted — `with_remarks()` prefers a recorded operator remark when
  one exists, falls back to the computed auto-remark, and reports `is_auto`
  so the UI can grey out an auto-remark rather than invite editing over it
  silently. **An auto-remark is never stored** (never a `float_remarks` row)
  — computing it fresh on every read is what makes it free to change if the
  weekend/holiday rule itself is ever revised, with nothing to migrate.

**Manual remarks are keyed on (store, business_date), survive a re-run, and
keep history — never a silent overwrite (rule 4).** `set_float_remark()`
upserts the *current* row in `float_remarks` (one lookup, not a scan of
history) and always appends to the append-only `float_remark_events` log, so
a correction is a new row, never an update that erases what was there.
`float_remark_history()` returns every remark ever recorded for a store-date,
oldest first — the page shows current + priors in an expander, never only
the latest. **An auto-remark can be overwritten by the operator with their
own text; once they do, `remark_by` becomes their name and the system never
overwrites it again on a later run** — `with_remarks()` only ever falls back
to the auto-remark when no recorded row exists at all.

**Bulk upload, template `store, business_date, remark` — same shape as
elsewhere (§6.9b's bulk upload for manual matches).** Every row is validated
before anything is applied: a row is rejected when its store or date is
outside the loaded/filtered grid, or the store-date is not actually flagged
high float that day — never guessed onto a day that was never high (rule 5).
Each accepted row applies independently via `apply_upload()` (unlike a
manual-match batch, there is no cross-row consumption to protect, so one
row's failure never blocks another's).

**Tiles:** stores above threshold today (count) and total float above
threshold (Rs) are both read as of the **last loaded business date** in the
active range — there is no other candidate for "today" on a system with no
hardcoded dates (rule 8). High-float store-days without a remark (count) is
the whole-range backlog — "the action" tile, the work queue this page exists
to drive to zero. Longest run of consecutive high-float days (store + n
days) is the maximum `days_held_high` across the whole filtered grid, named
by store and the date it runs through.

**Default view is the work queue: high-float rows with no remark at all,
most recent first.** Filters: All / High float only / Unremarked only /
Weekend-explained only. Export covers the whole grid respecting the current
filter, never only the rows on screen (§8's export rule, applied here even
though this page has no server-side paging — the whole filtered frame is
already in memory).

### 6.16 The performance rewrite (2026-10-13) - rules that now hold

Measured in `docs/PERF_BASELINE.md`; every claim below was proved against Run #11 (`docs/BASELINE_APR_AUG.md`,
which stays the truth until September is reconciled, then is extended to APR_SEP with Apr-Aug unchanged).

* **R1 is chunked** (`r1_chunked.py`): a chunk of consecutive dates (default 100,000 digital lines, lowered if the RAM
  available at run time could not hold it) is matched at a time against a disk-resident SQLite index of the whole
  pool, so a gateway reference is still found across ALL loaded dates (6.3 is unchanged). Consumption is a `used`
  flag on the pool row; Tier 2 runs in a second pass. Commits stay per business date. The vectorized matchers in
  `r1_bill_txn.py` are proved against a frozen copy of the per-row engine in `tests/reference_matcher.py` - that
  oracle is never edited. Time is flat from ~100k lines per chunk; only memory grows with chunk size.
* **Masters have a schema guard** (`masters.preflight_masters`): it runs FIRST in every run, read-only, and fails the
  run - naming every master, file and missing column - if a master file, sheet or required column is gone. Soft findings
  (rows added/removed, Excel `#REF!`, an identifier mapped to two stores) are warnings recorded on the run.
  Incident: `PICKUP LOCATION MASTER.xlsx` lost its LOCATION column on 2026-10-08; LOCATION is what splits pickup code
  AX31918 between APM and MMR (Apr-Aug: APM Rs 1,45,53,494 / MMR Rs 1,78,56,493 depend on it).
* **The Summary reads precomputed aggregates, never raw rows.** `txn_day_agg` (per run x date x store x gateway, filled
  by the engine) feeds the Mapped/Refunded/Unmapped strip; per-store-date `reconciliation_results` feed the bills strip.
  A manual match or a refund is applied live on top (no re-run, 6.9b-ii). The strip's "mapped" means "a bill matched
  this (gateway, reference)" - NOT "this physical row was consumed": 80 duplicate Axis lines (same RRN, amount, date,
  store) count as mapped although the engine lists them as unmapped. Open finding; the strip keeps its original definition
  so no baseline figure moves.
* **Exceptions are paged in the query**, 50 rows a page; a collapsed section reads nothing but its count; exports are
  a separate full-population path in chunks. Section state, Show/Sort choices and pager offsets live in plain session
  keys (a widget key is dropped when its page is not rendered).
* **Ingest uses Polars only where it measured faster, and never lossily.** `.xlsb` via calamine (`fastexcel`) with column
  types INFERRED from every row under strict coercion (a declared type silently nulls a mismatched cell); any error falls
  back, named in the log, to pyxlsb. The one known difference from the pyxlsb path is the raw `BILL_TIME` serial float
  (<= 1.1e-16, unused after ingest). Dependencies are pinned: `polars==1.44.2`, `fastexcel==0.21.0`.
* **A process pool for file parsing was measured and NOT adopted** (1.24x for +37% memory; a monthly run has one new file).
* **Navigation is a top bar of `st.page_link`s** (`dashboard_common.NAV_PAGES`), not `st.tabs` (which renders every tab
  on every run), with a status pill under it; the native sidebar list is hidden. Filters stay in the sidebar.

### 6.17 Navigation below the matrix, the logo, and Store Task (2026-10-14)

* **The navigation bar sits BELOW the page's opening block and is sticky.** On Summary that is under the particulars
  matrix (`page(..., nav=False)` then `top_nav("Summary")` after the matrix); on every other page it is directly under
  the heading and status pill, so it is in the same place relative to the page's own top block everywhere. Stickiness is
  on the `stLayoutWrapper` that Streamlit puts around the keyed container (`div[data-testid="stLayoutWrapper"]:has(>
  .st-key-topnav)`) - sticking the container itself does nothing, its parent wrapper is exactly as tall. The page order is
  logo + title + period, the Live pill, the matrix, the bar. `theme.css` is cached per process: restart to see CSS edits.
* **Logo:** `app/assets/citykart-logo.png`, 32px high, aspect kept, left of the title on the same line (data: URI, read
  once per file version). If the file is missing or is not a PNG the title stands alone and a warning is logged - never a
  broken image. THE FILE IS NOT IN THE REPOSITORY YET; it has to be supplied.
* **Store Task** (`pages/7_Store_Task.py`, `mop_reco/store_tasks.py`, tables `store_tasks` + append-only
  `store_task_events`). Tasks are generated from data, close themselves when the data clears them, and a store's reply
  never closes one. Types and conditions:
  * `open_bill` - an engine-unmatched bill line with no active manual match, merged to one task per
    date|store|bill_no|mop. Closes when the bill is matched (engine or person) or leaves the dump.
  * `high_float` - float (column N) above the store's threshold and no weekend/holiday auto-remark. Active only while the
    store's high-float streak is unbroken to the last loaded date; the first later day at/below threshold closes it.
  * `cms_not_done` - merged per store|date from two facts: NO DEPOSIT (a day's cash sale was due for deposit by the next
    banking day - not Sunday, 2nd/4th Saturday or a listed holiday - and no deposit voucher appears; clears when the store
    later makes a deposit the agency credited) and NOT CREDITED (a deposit the running CMS ledger still shows outstanding
    past T+1 working day, allocated to the newest deposits first). The running ledger, not per-day K, is used on purpose: K
    shows phantom shortfalls wherever a credit was booked on another date or store (Defect B). The T+0/T+1 per-store cycle
    is in no master, so T+1 is the bound (config `store_tasks.cms_credit_due_working_days`).
  * Keys are stored type-prefixed (`open_bill:`, `cms_not_done:`, `high_float:`) because the last two share the
    `store|date` shape. `generation` is 1 for the first task on a key; a task a person closed is never reopened - if the
    condition is still true on a later sync a NEW task (generation + 1) is raised.
  * Conditions that arose in the loaded history and have ALREADY cleared are counted (`SyncReport.already_clear`) but not
    persisted as tasks. Auto-closure only touches tasks whose business date lies inside the dates the syncing run covers.
  * Raised on = the sync date; Due by = raised + `store_tasks.sla_working_days[type]` working days (default 3); Overdue
    = not closed and due before today. "Closed this period" = closed tasks whose business date is in the sidebar range.
  * The sync runs as the `store_tasks` stage of every run (failure is logged and recorded on the run, never fatal) and
    again on page load when the run or the set of active manual matches changed, so a manual match closes its task at once.
  * Replies: bulk upload `task_id, store, reply, replied_by`, every row validated (task exists, not closed, store is the
    task's own, no task twice in a file), preview, confirm. A reply replaces the current reply; earlier ones stay in the
    history. Every read takes `store`, which is how a store login will be scoped later.
  * Measured on Run #11 (Apr-Aug): 7,968 live tasks - 4,769 open bills (Rs 46,85,928), 3,123 high float on 64 stores
    (Rs 12.95 Cr of float), 76 CMS not done (Rs 1.38 Cr); a further 5,200 high-float and 3,004 no-deposit conditions had
    already cleared.

### 6.18 Tab restructure, Exceptions pairing, masters failing gracefully (2026-10-14)

* **Tabs, in order:** Summary | Exceptions | CMS Reco | Float | Store Task | Upload | Masters. **Manual matches and Run
  are gone as tabs.** Nothing was deleted: By bill / By transaction matching, the bulk upload, the refund upload, the
  manual-match report and the register are sections of the **Exceptions** page (`pages/8_Exceptions.py`, under a "Manual
  matching" heading); the Run controls, run history, heartbeat and "what a run would read" are the Run section of **Upload**
  (`run_panel.py`, below the upload form). The Exceptions section moved off Summary unchanged.
* **Exception bills are paired with candidate transactions** (`mop_reco/exception_pairing.py`). Candidates are the engine's
  open unmapped-transaction exceptions (less anything a person matched or refunded) in the SAME store with the EXACT amount,
  first within +/-10 minutes (needs a time on both sides; Axis has none), then +/-2 days; nearest in time first
  (`config.yaml` exception_pairing). Time orders and narrows; it never decides. The table shows Txn 1 / Amount 1 / Txn 2 /
  Amount 2 / More ("+N more", full list in a picker) / Match / Remark. Txn 1 and Txn 2 are editable (a pasted id is
  validated like any manual match, searching the whole warehouse with `manual_match.targeted_pool`, ~1.5 s); Match records
  the manual match with full validation, naming the bill that holds a transaction already used. A bill with two genuine
  payments is NOT forced to 1:1: after matching one, the bill is closed and the other transaction stays open, flagged under
  "Second transaction still open" for a person to decide (refund, or a second bill). On Run #11: of 4,769 open bills 2,808
  have 1 candidate, 542 have 2, 279 have more than 2, 1,140 have none (30 have one within 10 minutes).
* **Two slow queries were fixed because the page now carries them:** the open-bills page/count used to walk the whole 3.7M-row
  table (23 s) - an index hint plus a hint-preserving count make it 0.9 s; the "matched on a different day" list scans every
  matched line (37 s) and now loads only when asked.
* **A master that cannot be loaded no longer takes the Masters page down** (`masters.load_all_masters_safe`): each master is
  pre-flighted and a bad one is reported - "Pickup location master cannot be loaded - LOCATION column missing (header on
  disk ...; file modified ...)" - while every other master renders and the stored version stays in force. Nothing is inferred.
* **Page chrome:** title 18px; the period is no longer written beside the title (the Live pill shows it); the build stamp
  ("code loaded ...") is the Live pill's tooltip, not page text; the "newer run", "Not signed in" and "All figures in Rs Cr"
  texts are gone. Summary's three buttons are one left-aligned row, 12px gap, Build keeps the accent fill. The matrix's
  DIGITAL / CASH headers are bold and centred exactly over their column spans.
* **Store Task tiles** are Open (raised, no store reply) / Pending (store replied, awaiting HO review) / WIP (HO reviewing,
  action underway) / Closed (resolved by the data or a person), each count and value; Sort, Overdue-only, the Due by column
  and the Overdue column were removed; filters are one row (Store code, Search, Type, Status) styled like the sidebar.

### 6.19 One definition of the Exceptions tables, for the screen and the export (2026-10-15)

* **Incident:** exporting Exception bills raised `KeyError: ['Txn 1', 'Amount 1', ...] not in index`. The screen had gained the
  candidate-transaction columns; the export query still returned the old frame and the page selected the screen's column list
  against it. The progress bar had said "Collected 4,769 of 4,769" and then crashed at the write step.
* **Fix:** `mop_reco/exception_tables.py` holds the declared schema (data columns, candidate columns Txn 1 / Amount 1 / Txn 2 /
  Amount 2, remark columns, and the UI-only `More` / `Match`) and the frame builders (`bill_frame`, `txn_frame`,
  `add_pairing`) that the page AND the export both call, so they cannot drift. `export_view()` refuses a UI-only column by name
  and `select()` names a declared column that is missing (`ExportSchemaError`) instead of a KeyError. An open-bills export has
  the candidate columns; a closed one does not (nothing to pair). Dates are written DD/MM/YYYY, ids and times as text.
* **Progress is honest:** `export_all_button` shows Collecting (to 60%), Building the file (60-95%), then Ready; a failure
  shows an error with its reason instead of a full bar. The first chunk is shaped and checked before the rest are read.
* **The open-bills query** (`queries._bill_match_query`) wraps its ORDER BY in `coalesce` so SQLite stops walking the page
  index for the order: the open-bills export fell from 81 s to 0.3 s. (SQLAlchemy cannot emit INDEXED BY; its `with_hint` renders
  nothing on SQLite - the earlier "hint" was not applied and the speed-up on a 50-row page was luck of the planner.)

### 6.20 Candidate transactions are a GLOBAL ASSIGNMENT, not a per-bill lookup (2026-10-15)

* **Incident:** the first pairing looked up store + amount per bill independently. 705 of 3,582 suggested transactions were
  offered against more than one bill (1,573 slots; one id against five bills) - accepting them would have closed five bills with
  one payment. It also filled Txn 2 on 821 bills with the "next best" transaction: all 821 had Amount 1 equal to the bill, so
  **0 of 821** satisfied Amount 1 + Amount 2 = bill amount - every one was noise.
* **Rule:** `exception_pairing.assign()` builds the candidate set once over the WHOLE open population (open + under
  investigation, never a page or filter) with a hard constraint that a transaction is suggested to at most one bill.
  1. EXACT: same store, exact amount, within +/-2 days. A contest is won by the smallest time difference (untimed - Axis, or a
     bill with no time - is "about half a day" plus whole days, so a date with no clock never invents minutes), then by the bill
     whose MOP belongs to the transaction's gateway (config exception_pairing.mop_family), else NOBODY gets it.
  2. SPLIT: a bill with no exact candidate at all may take TWO free transactions whose amounts sum exactly to the bill. This is
     the only meaning of Txn 2. A duplicate payment (second transaction equal to the bill) is never Txn 2.
  3. AMBIGUOUS clusters (N bills and M transactions of one store and amount that cannot be separated) are left blank, flagged
     "ambiguous - N bills, M transactions", and offered in a resolver for the page: N == M can be paired as a set in time order
     (amounts are equal, so no figure moves); otherwise listed for a decision.
  Contests are decided by sort key and tie-group components, never by row order (a test reverses the input).
* **Asserted, not trusted:** `assert_unique()` runs inside `assign()` and fails the build if a transaction appears twice across Txn 1
  and Txn 2, or is both suggested and in an ambiguous cluster. At match time the page re-reads the unmapped pool and refuses a
  suggestion another bill has since taken, naming the holder; a split bill is not one-click matched (a manual match holds one
  transaction).
* **Run #11 result (4,769 open bills, 6,272 unmapped transactions):** exact 3,089 bills / Rs 30.94 L; split 213 / Rs 3.56 L (all 213
  sum exactly); ambiguous 532 / Rs 1.99 L in 242 clusters (220 of them N == M); none 935 / Rs 10.36 L (925 have no same-store,
  same-amount transaction, 10 lost the contest). 3,515 distinct transactions suggested, 0 more than once.
* The screen and the export both call `exception_tables.add_pairing(frame, assignment)` with the same assignment.

### 6.21 The self-check now covers every project module, not a hand-kept list (2026-10-15)

* **Incident:** the Exceptions page crashed with `module 'mop_reco.exception_pairing' has no attribute 'open_population'`. The
  function was on disk; the running dashboard had imported an older copy (CLAUDE.md 9a). `selfcheck` should have said so, but it
  only knew two hand-kept lists (`WATCHED` aliases, `FROM_MODULES`) and `ep` was on neither, so it was blind to the module.
* **Fix:** `selfcheck.run_check()` now checks EVERY reference to a project module (`mop_reco.*`, `dashboard_common`,
  `inline_match`, `run_panel`) in every page, every app module and every `mop_reco` module - `alias.attr` calls and plain
  attribute loads, and `from module import name` - against the module as LOADED in the process, and the stale check covers every
  loaded project module. An alias that a file rebinds locally is never trusted. 457 references resolve today; tests delete a
  function from a previously unlisted module and demand it is named.
* **The exceptions assignment cache key** (`_pairing_fp`): run id, number of ACTIVE manual matches and the newest manual-match id
  (a reversal changes the first), refund count, and the size and newest id of the open exception population (a bill or
  transaction closed, auto-resolved or re-raised by any route). There is deliberately no date/store filter in it: the assignment
  runs over the whole open population so a transaction is never suggested to different bills in different views.

### 6.22 CMS Reco - one block per deposit date, chained per store (2026-10-16)

* Replaces the running-ledger page. Per store, per deposit date d: OPENING (previous CLOSING; first block from config
  `cms_reco.opening`, default 0 at the run's first date) + Cash/CMS Deposit (-1 x PTC deposit vouchers) - CMS Report (1)
  (COL slips, DEPDATE = d, CRDATE = d) - CMS Report (2) (same, CRDATE = d+1) = CLOSING. `mop_reco/cms_blocks.py` (engine),
  `cms_blocks_export.py` (wide workbook, live formulas), `pages/2_CMS_Reco.py`. Inputs are persisted at run time into
  `cms_slips` / `cms_ptc_deposits` (stage `cms_slips`; `scripts/backfill_cms_slips.py` for an older run).
* CRDATE, not CLGDATE: CLGDATE equals DEPDATE on every COL row, so it cannot tell same-day from next-day credit.
* A credit dated day+2 or later is in NEITHER column. It stays in CLOSING and is listed under "Credited later than day+1"
  (T+10 or more flagged). Store comes from the pickup master (location-aware); no match = UNRESOLVED (kept in totals);
  BNK is its own pseudo-store. ZPO (no dates) and ADJ rows are not in the grid and are listed on the page.
* Controls: block arithmetic, chain, summary Diff = last CLOSING, page deposit = store-sheet column I. Page CMS Report vs
  column J is shown as a MISMATCH with a full bridge (credited later, credits after the run's last date, unmapped stores,
  ADJ net). Column J is NOT redefined; awaiting a ruling. Remarks are keyed on store + period, with append-only history.

## 7. Acceptance numbers

Every stage is proved against these. They come from the two workbooks already
analysed, plus the 2026-08-24 data inventory. **A stage is not complete until its
numbers match exactly.**

### R3a — 19 July 2026
- All **193 store-days reconcile to nil**
- ABD: opening 76,938 · cash sale 82,608 · net PTC −185 · closing 159,361
- Deposits derived: ARG 56,003 · CNW 41,850 · VSW 31,864

### R1 — 20 July 2026
- 40,870 bill rows → **17,536 digital bill lines**
- Tier 1: **17,492 matched** (99.69% by value)
- Tier 2 on residual: **38 matched, 0 ambiguous**
- **6 bills left open**
- Unmapped Paytm: 57 before Tier 2 → **21 after**. Axis: 2 → **0**
  - **Do not "correct" 21 back to 19.** An earlier version of this document said
    19, which was an arithmetic error in the source: it credited all 38 Tier-2
    consumptions to Paytm (57−38=19). The 38 matches actually consumed 36 Paytm
    + 2 Axis, so the residual is Paytm 57−36=**21** and Axis 2−2=**0**. 38
    matches cannot consume 40 transactions. Tier 2 runs across all gateways.
- Digital sale by store ties to the manual format sheet for all 188 stores
- BILLNO: 36,889 unique across 40,870 rows; 3,276 bills carry multiple MOP lines
- Ambiguity risk: 1,664 store+date+amount groups covering 4,158 bills

### Paytm / Axis split — 20 July 2026
- 17,098 Paytm transactions: UPI 16,726 · DEBIT_CARD 359 · CREDIT_CARD 13
- 453 Axis EDC rows
- **279 distinct payout_ids** for 17,098 transactions

### R2 — Paytm settlement, 20 July 2026

No longer blocked as of 2026-08-24 — the settlement export is present and usable.
Fixed acceptance numbers from the file we have:

- The Paytm settlement export reads **17,099 rows** across its three zip parts
  (11,819 + 5,014 + 266), before any dedupe
- **279 distinct `payout_id` values**
- `settled_amount` is **populated on every one of the 17,099 rows** — not null,
  not zero

Axis and Bajaj legs of R2, and the bank-statement leg, remain open — no bank
statement or Bajaj transaction data has been supplied yet. Stage 10 (§ build
order) is unchanged as the home for R2, but it is now buildable rather than
waiting on missing data.

### Full July 2026 — measured from the July dump, 2026-09-02

Volumes (these are the control totals a full-month run must reproduce):

| Source | Rows | Coverage |
|---|---|---|
| POS bill lines | **1,490,141** | `Sheet1` 791,166 (01–15 Jul) + `Sheet1 (2)` 698,975 (16–31 Jul) |
| Paytm settlement | **642,165** | 3 zip parts, 0 duplicates, 8,609 payout_ids |
| Axis EDC | 17,870 | 34 columns, 402 MIDs |
| Axis CMS | 4,594 | COL 4,484 · ZPO 106 · **ADJ.CR. 3** |
| PTC | 21,690 | 4,490 `Cash/CMS Deposit` vouchers |
| Bajaj | 573 | 490 July + 83 August |

MOP class split, which must sum to the row total:
`digital 660,150 · cash 726,060 · non_settling 103,931 = 1,490,141`
(of which `EDC OTHER` 2,061 · `EDC OTHER AXIS` 5 · Bajaj MOPs 451)

Cross-gateway sanity: 660,150 digital bill lines vs 660,035 gateway rows
(642,165 Paytm + 17,870 Axis) — a 115-row gap before matching.

Non-ACQUIRING linkage (§6.3): CHARGEBACK 13 → all 13 originals found, no
exception · REFUND 9 → 1 found, **8 exceptions** · CHARGEBACK_REVERSAL 2 → 0
found, **2 exceptions**. Expected total **10 exceptions**.

### 20 July 2026 — the same date from two files

The monthly `.xlsb` and the loose daily `.xlsx` agree on the entire sale
universe; they differ only by the 628 non-bill rows (§6.13):

| | Monthly `.xlsb` | Daily `.xlsx` |
|---|---|---|
| rows | 40,242 | 40,870 |
| rows without `BILLNO` | 0 | 628 (all `Cash`) |
| distinct `BILLNO` | 36,889 | 36,889 |
| bills with >1 line | 3,276 | 3,276 |
| digital bill lines | 17,536 | 17,536 |
| **cash total** | **12,941,046** | **12,941,046** (after §6.13) |

Both files must produce cash **+12,941,046** — the daily file only after the
§6.13 exclusion, the monthly natively. That pair is the test.

### R3a — full July 2026 (measured 2026-09-10)

The estate identity, which must hold **exactly**:

```
    opening  1 Jul        22,808,219.46      (193 stores)
  + cash sale            489,377,871.79
  + net PTC               -5,826,646.65      (income 1,072,111.51 · expense -6,898,758.16)
  - CMS deposits         489,898,591.00      (4,490 vouchers)
  = closing 31 Jul        16,460,853.60
    actual                16,460,853.60
    DIFFERENCE                     0.00
```

- 6,076 store-days · **196 stores** · 31 dates (193 with an opening + BGP, KGT, RSG at zero)
- carry-forward breaks: **0**
- store-days with a negative closing: **0**
- stores over Rs 2,00,000 at 31 July: **3** — MBA 390,285 · PNA 234,500 · KHR 222,408
- runs of 3+ collection days with cash sales and no deposit: **11 across 5 stores**
  (MBA 5 of them, MDH 4). MBA is the worst on every measure and holds no deposit
  voucher at all on 31 July.
- 20-Jul cash sale reads **12,941,046** — the §6.13/§7 figure, reproduced by the
  cash book rather than asserted separately.

### R3b readiness — full July 2026 (report only, measured 2026-09-10)

The CMS bank-credit file for July is a **zip of CSV**, not `.xlsx` (the daily
file is `.xlsx`), encoding **utf-8**, header on row 1, 67 columns with
`RECOVEREDAMOUNT` and `SUBCUSTOMERNAME` each appearing twice — read by
position, per §5. Every data row carries 68 fields against the 67-column
header (a trailing comma), so it is ragged and must be trimmed.

- 4,593 rows: COL 4,484 · ZPO 106 · ADJ.CR. 3
- **ZPO's 106 rows total 493,047,736 — a near-mirror of COL's 493,047,618.**
  This is why §6.7 excludes ZPO: including it would roughly double the
  credited value.
- pickup code resolves to a store: **4,487 of 4,487 — zero unmapped**, across
  187 distinct codes and 187 stores
- 82 credit rows carry a deposit date before July (18–30 Jun), credited in
  July — they belong to June's receivable
- credit lag: T+0 2,464 · T+1 1,691 · T+2 165 · T+3 165 · T+4 1 · T+20 1

The figure R3b has to explain:

| | |
|---|---|
| PTC Cash/CMS Deposit, July | 4,490 vouchers · **Rs 489,898,591.00** |
| CMS credited, July deposit dates | 4,405 rows · **Rs 483,597,978.00** |
| **difference** | **Rs 6,300,613.00** (1.286% of deposits) |

That difference is cash in transit at month end (a late-July deposit credits in
early August, which is not in this file) **plus** anything genuinely missing.
It is **not yet an exception** — separating in-transit from missing needs the
pickup-level match, and that is R3b.

### R3b — 1–6 August 2026
- 550 PTC vouchers under `Cash/CMS Deposit`, total **₹8,76,37,684**
- The manual grid captures ₹8,74,44,724 — **₹1,92,960 falls outside it**
- Credit lag: T+0 1,724 · T+1 1,259 · T+2 3 · T+3 247 · T+4 1
- CMS TYPE: COL 3,231 · ZPO 65 · ADJ.DB. 2 · ADJ.CR. 1
- Monthly difference ₹1,25,214 across 23 stores. **Three are real:**
  PDR +61,996 · ANM −3,59,310 · MMR −56,949. The other 20 are opening residue
- Pickup master: 210 codes, format is not standardised (see §5) — flag on
  resolution failure only

---

## 8. Code conventions

- Engines are **pure functions**: DataFrames in, DataFrames out. No Streamlit imports
  in `mop_reco/` — the dashboard is a viewer over tested engines.
- Every rule that could change lives in `config.yaml`, not in code.
- Money as `Decimal` or rounded float to 2 places. Never compare floats with `==`;
  use a `TOLERANCE` constant set to 0.
- Log every run: dates, files read with their mtimes, row counts, control totals.
- Tests in `tests/`, one file per stage, asserting the section 7 numbers.
- Never `print()` for output — use the logger.

**Export rules (added 2026-09-09).** Every export covers **every row matching the
current filters**, never the page on screen — the button reads
`Export all N matching rows` where N is the filtered count, and the filtered
count and the exported row count are asserted equal. Under
`export.XLSX_ROW_LIMIT` (100,000) it produces `.xlsx`, above it `.csv`, and the
UI states which it produced and why. Reference ids are written as **text**: a
Paytm `transaction_id` is 35 digits and Excel keeps only 15 significant digits
of a number, so a numeric column would silently corrupt it. Every export opens
with a header block — run id, run timestamp, business date range, filters
applied, row count, total value, export timestamp — because a file detached
from its run is unauditable. A long export reports progress; the page never
appears frozen. Measured on full July: 660,150 rows → CSV, 91.5 MB.

**Two gaps in the text-format rule, found and closed 2026-09-20.** The rule
above ("reference ids are written as text") already existed, but two things
about it were not actually true everywhere:

1. **`export.ID_COLUMN_HINTS` did not cover `slip_no` or `pickup_code`.**
   `mid` and `tid` happened to match by accident (both contain the
   substring `"id"`), but `slip_no` and `pickup_code` matched none of the
   hints at all — so a comma-joined CMS slip number on the Data quality
   export was exactly as exposed to Excel's numeric-coercion bug as a
   Paytm `transaction_id`, just not yet reported as broken. All four are
   now named explicitly in the hint list, not left to accidental substring
   matches.
2. **The `.xlsx` branch trusted `to_excel()`'s own `write()` to leave a
   numeric-looking string alone, and CSV had no quoting at all.** Both are
   now explicit rather than assumed: every ID column's cells are
   individually re-written with `worksheet.write_string()` (never `write()`)
   under a `{'num_format': '@'}` text format, and the CSV branch passes
   `quoting=csv.QUOTE_NONNUMERIC` so every text field — every id column,
   already converted to a Python `str`, plus every other text column —
   opens quoted, which is what stops Excel guessing a bare numeral is a
   number when the file has no other type information at all. Verified
   directly (not just by reading the code): built a real export through
   the same `queries.exception_rows()` → `build_export()` path Summary's
   Exceptions section uses, for a live 35-digit transaction id
   (`20260720111030000285964210320294741`) drawn from the actual database —
   the cell landed as `'20260720111030000285964210320294741'`, openpyxl
   data type `'s'` (string), number format `'@'` (text): not
   `2.02607E+34`, not truncated, not padded with zeros. Confirmed live in
   the running app too: the on-screen Exception transactions table already
   showed the id in full before export, and the downloaded workbook's cell
   read back exactly the same digits.
3. **Two ad hoc `st.download_button(..., df.to_csv(...))` calls bypassed
   `build_export()` entirely** — Summary's Data quality expander and
   Float's negative-float expander each wrote a raw, unquoted,
   un-stringified CSV straight from the DataFrame, with no header block
   either. Both were the sort of export the rule above was written to
   cover and simply predated it. Both now go through `frame_export_button()`
   like every other export on their page.

**Nothing about `write()` vs `write_string()` changed the OLD `.xlsx`
export's actual correctness** — a direct test before this change already
showed a plain `to_excel()`-written 35-digit id landing as a string cell,
so the underlying pandas/xlsxwriter behaviour was not silently corrupting
ids on the `.xlsx` path today. The explicit `write_string()` re-write is a
belt-and-suspenders change against a future pandas/xlsxwriter version doing
something different, made because the operator's own implementation
instructions were specific about it, not because a live bug was reproduced
on that path. The CSV path and the `slip_no`/`pickup_code` hint gap were
both real, reproducible gaps, not just defensive hardening.

**`zoho_no` added to `ID_COLUMN_HINTS` the same way, 2026-09-20**, when the
customer-refund feature (§6.9c-ii) introduced it as a new identifier column
— a Zoho ticket number is exactly the kind of value that must never be
silently coerced to a number by Excel, the same reasoning as `slip_no`/
`pickup_code` above.

### 8.1 Visual theme — one stylesheet, injected once (2026-09-18 restyle)

**Visual only, on instruction — no query, formula, column or figure changed
by this pass.** Every number this section's own screenshots show is
byte-identical to the same tile before the restyle; a passing test suite
was not treated as proof of that, the actual figures were read back from
the running app before and after (see the report delivered with this
change for the full before/after pair).

**`app/assets/theme.css`** is the ONE stylesheet — every colour, font and
spacing value the dashboard uses is a CSS custom property declared once at
`:root` there (`--bg`, `--surface`, `--ink`, `--accent`, `--good`/`--warn`/
`--bad` and their `-tint` pairs, etc.) — and `dashboard_common.inject_theme()`
is the ONE place it is read and injected (`st.markdown(f"<style>{css}</style>")`,
the file cached in memory via `st.cache_data` so it is read from disk once
per process, not once per rerun). `page()` calls it, so every page picks it
up automatically; no page carries its own `<style>` block any more — the
three ad hoc tinted-ribbon styles that used to live inline in Summary.py,
Float.py and Manual_matches.py are now one rule each in theme.css, keyed on
the same `st.container(key=...)` hooks those pages already used
(`.st-key-tile-ribbon`, `.st-key-mm-open-ribbon`, `.st-key-float-ribbon`).
`.streamlit/config.toml`'s `[theme]` block carries the same five core
values by hand (Streamlit's own chrome and the dataframe grid's base
colours can only be set there, not via CSS) — the two files are kept in
sync manually since there is no way to share one definition across a TOML
file and a CSS file.

**`st.dataframe`'s grid is drawn on an HTML canvas (glide-data-grid), not
DOM table cells — no CSS selector can reach a cell, a row stripe or a
header inside it.** This is why theme.css's own header comment says so
explicitly: the grid's base colours come from `.streamlit/config.toml`'s
`[theme]` block (which Streamlit does thread through to the grid), not from
CSS, and nothing in theme.css can restyle a table's zebra stripes, sticky
header behaviour or hover colour — those are the grid's own defaults.

**A canvas `fillStyle` cannot resolve a CSS custom property — found live,
the hard way.** `dashboard_common.status_styler()` tints a status column's
cells via a pandas Styler (`df.style.map(...)`, background-color/colour
only — the officially-supported subset for `st.dataframe`), never by
baking HTML into the cell value, so sorting/filtering/export all still read
the plain original word (rule 4: colour is additional, never the only
signal; a black-and-white printout still reads correctly). The first
version of `STATUS_TINT` used `var(--good-tint)` etc., matching theme.css's
own variables — and every tinted cell rendered **blank**, not merely
uncoloured, because the grid draws its cells with a browser Canvas 2D
`fillStyle`, and `fillStyle` cannot resolve a CSS custom property; it
silently fails rather than throwing. `STATUS_TINT` now holds the same five
colours as literal hex, duplicated by hand from theme.css's `:root` block —
there is no way to share one definition across a CSS file and a
canvas-consumed Python dict. Confirmed live afterwards: the register's
Status/Lifecycle columns and the Exceptions tables' status column render
correctly tinted, sort and export both still work, and the exported cell
values are the untouched raw strings (`"matched_manual"`, `"open"`, …).

**`dashboard_common.tile()` gained `accent` and `feature` parameters,
`state` no longer drives any colour.** The brief's own rule — a 3px left
accent bar reserved for exactly two named tiles (Digital receivable, Cash
receivable, both `accent="warn"`) and any genuine break/mismatch, "everything
else — no accent bar" — is a deliberate calming-down from the previous
design, where every "attention"/"action" state got its own colour and five
tiles ended up competing for the eye. `state` (`"clean"`/`"attention"`/
`"action"`/`"neutral"`) is still accepted by every existing call site
unchanged, but no longer paints anything; only `accent` does. `feature=True`
is for the one filled `--accent-deep` tile (Total sale on Summary) — never
set on more than one tile per page.

## 9. Working agreement

- One stage at a time. Do not start the next stage.
- Show the test output. If numbers do not match, say so plainly and stop — do not
  adjust the expected numbers to fit the code.
- If a rule here is ambiguous for the case in front of you, ask rather than choose.
- **When the operator gives a numbered plan, run it through to the end.** Report
  at each step, but only STOP if a step fails, a control breaks, or the step
  itself says to stop. "Report step 1 before doing step 2" means show your work
  and continue — it does not mean wait for a reply before proceeding. Waiting
  for approval that was not asked for costs the operator a day each time. This
  does not weaken the "one stage at a time" bullet above, which is about the
  *pipeline's own build stages* (do not start Stage 2 of the reconciliation
  build without proof Stage 1 works) — a numbered plan the operator hands over
  in one message is a single unit of work to complete, not a sequence of
  separate stages each needing its own go-ahead. Added 2026-09-29, after a
  three-message round trip re-confirming a plan that had already been approved
  in full.
- **When you narrow the scope of something the operator asked for, say so in one
  line at the top of your reply — every time, not just when it seems load-bearing.**
  Added 2026-09-24: a five-month run (April-August) was stalling, so a July-only
  run was built with an explicit file-path override to get *something* working
  while the throughput problem was fixed — a reasonable call. But the reply that
  delivered it read as if the whole request had been done, and the operator spent
  a day believing April/May/June/August had failed to load, when they had simply
  never been asked to run at all. A one-line "scoped to July only, not the full
  range you asked for" at the top would have prevented the entire detour. This
  applies to any narrowing — one month instead of five, one source instead of
  all, a sample instead of the full file — not only the cases that turn out to
  matter later, because which ones will turn out to matter is exactly what is not
  knowable in advance.

**A passing test suite is not evidence the application works.** Any change that
affects the UI must be verified in the running application — load the actual
page, confirm the change is visible and the page renders without error — before
it is reported as done. If the page cannot be loaded and checked, say so and
report the change as unverified rather than complete.

*Added 2026-09-11 after reporting four UI changes as working when the app the
operator was running crashed on the Summary page. The code on disk was correct
and 269 tests passed; the failure was that verification happened on a
freshly-started process while the operator's process — started hours earlier —
was serving cached modules. See §9a.*

### 9a Streamlit keeps modules in memory. Restart, do not assume.

**Streamlit re-runs the page scripts on every interaction but keeps imported
modules in `sys.modules`.** A process started before a change to
`mop_reco/*.py` or `dashboard_common.py` therefore serves the NEW page script
against the OLD module. The visible result is a raw `AttributeError` partway
down a page, or a change that simply is not there — with correct code on disk
and a green suite.

Rules that follow from it:

- **Verify on the process the operator is using**, not one started for the
  purpose. Check what is actually running (`Get-CimInstance Win32_Process`
  filtered to `streamlit run`) before believing a screenshot.
- **One dashboard at a time.** Seven Streamlit processes were found running on
  2026-09-11, the oldest two days old, two of them bound to port 8501. The
  launcher now stops any existing dashboard before starting, because an old
  process left bound to the port is a silent-wrong-answer bug, not clutter.
- `pkill -f "streamlit run"` from Git Bash **does not** kill Windows processes
  started outside that shell. It reported success and killed nothing.
- **`app/selfcheck.py` runs on every page load** and names the failure instead
  of throwing it: every `module.function(...)` a page calls must exist in the
  module as loaded, every `from module import name` must resolve, and any
  module whose file is newer than the process start is reported as stale with
  its resolved path and both timestamps.
