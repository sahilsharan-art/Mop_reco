# ERP database integration — brief for the Claude session that will work with the ERP database

Written 2026-11-23. Audience: a Claude session (or a DBA) that has access to the Citykart **ERP database (PostgreSQL)** and will
help the MOP Reconciliation application get a **read-only** connection to it. Self-contained: you do not need the other session's history.

> **Handling note.** This file names internal servers. Do NOT commit it to the public GitHub repository and do not paste it anywhere public.
> **No password, key or token appears in it, and none may be put in chat, in a file in the repository, or in a command line** (see section 3).

---

## 1. The ask, in one paragraph

The MOP Reconciliation application (Streamlit + PostgreSQL, runs on server **10.0.1.98**) currently gets its **POS bill data** and
**petty-cash (PTC) vouchers** from Excel/CSV files that someone exports and copies into folders. We want it to read the same data
**directly from the ERP database, read-only**, then retire the Excel files source by source. Your job is **discovery and safe access
only**: find where the data lives, set up (or specify for the DBA) a **read-only role and stable read-only views**, prove the numbers match the
Excel exports, and hand back a precise mapping. **You do not build the reconciliation, and you change nothing in the ERP.**

## 2. What the application needs (the "data contract")

Two sources are in scope first. Everything below is what the Excel exports contain today, so it is also the definition of "correct".

### 2.1 POS bill lines — the big one

**Grain:** one row per **bill x payment line** (a bill paid with two modes of payment has two rows). Roughly **1.2 to 1.5 million rows a month**
(about 8.6 million for April to August 2026). Columns the app uses (names are the Excel headers; the ERP names will differ):

| Logical field | Excel header | Meaning / example | Notes |
|---|---|---|---|
| bill_no | `BILLNO` | e.g. `SB58/024800/0726` | **Globally unique per bill.** Format `<series>/<sequence>/<MMYY>`. |
| site_ref | `PSITE_POSBILL_CODE` | e.g. `SSRKPHB-145937` | site-wise reference, not the bill number |
| store_code | `ADMSITE_CODE` | store code | |
| store_name | `STORE_NAME` | store short code used by the app, e.g. `ABD` | the app resolves everything to the short code |
| business_date | `BILLDATE` | the bill date (a **date**, no time) | **The business date. Always taken from this column.** |
| bill_time | `BILL_TIME` | time of day of the bill | may be a time or a day-fraction; used only to order candidates |
| mop | `MOPDESC` | payment mode name | see the list below; **exact spelling matters** |
| mop_settlement | `MOPSTLMDESC` | settlement description | |
| amount | `BASEAMT` | the amount of this line | `BASEAMT = BASETENDER - BASEBALANCE` (tendered minus change) |
| tender / balance | `BASETENDER`, `BASEBALANCE` | | |
| edc_response | `TPEDCRESPONSE` | **JSON text** returned by the payment terminal | critical, see below |
| tp_bill_ref | `TPBILLREFNUMBER` | third-party bill reference | |
| created_by | `CREATEBY` | till operator, text as is | |

**`TPEDCRESPONSE` is the most important column.** It is how a bill is matched to a payment-company transaction. Three shapes exist:
- Paytm: `{"head":..., "body": {"acquirementId": "...", ...}}` (card/EDC) or `body.txnId` (QR/DQR)
- Axis: `body.retrievalReferenceNo` (and sometimes `body.issuerMaskCardNo`, a **masked** card number)
- Bajaj Pay: a **flat** JSON with a lowercase `rrn` and `tid` (no head/body)
Confirm that the ERP stores this text **unchanged** (not parsed, truncated, or re-encoded) and say which column and type holds it.

**MOP names the app knows** (`MOPDESC`, exact): `Cash`, `PAYTM EDC`, `PaytmDQR`, `PAYTM DQR MOREFUN`, `EDC OTHER`, `EDC OTHER AXIS`,
`Bajajpay UPI`, `Bajajpay Card`, `Credit Note Issued`, `Credit Note Received`, `Gift Voucher`. Report any other value you find, with a count.

**Rules the data must follow (or be explained):**
- A **row with no bill number** is not a bill: in the old daily export these are cash-in/out movements (they mirror the PTC ledger). The monthly export has none.
- IT **deletes duplicate bills 7 to 15 days after the sale**. We must learn *how* the ERP records that: hard delete, a deleted/cancelled flag, a status column, or a later
  change-log. This decides how the app re-pulls recent days. **This is the most important question in the discovery.**
- Nothing may be filtered out silently: if the ERP has cancelled/held/draft bills, say how to tell them apart.

### 2.2 Petty-cash (PTC) vouchers

**Grain:** one row per voucher. About **21,700 rows a month**.

| Logical field | Excel header (layout B) | Meaning |
|---|---|---|
| store | `Source Short Name` | store short code |
| business_date | `Billdate` | voucher date |
| voucher_no | `Billno` | voucher number |
| mode | `Ptcmode` | `PAYMENT` (negative) or `RECEIVE` (positive) |
| head | `PTC Head` | e.g. `Cash/CMS Deposit` |
| ledger | `Ledger` | e.g. `CMS-RECD.` |
| amount | `PTC Bill Amount SUM` | **the amount** (PAYMENT rows negative) |
| remarks | `Remarks` | free text |
| other | `Sub Ledger, PTC Bill Status, Settlement Status, Terminal, Createdon, Createdby, Lastmodifiedon, Lastmodifiedby` | kept for traceability |

A **cash deposit** voucher is `PTC Head = 'Cash/CMS Deposit'` **and** `Ledger = 'CMS-RECD.'`; today the two always agree (0 disagreements).

### 2.3 Out of scope for now (do not spend time here, but tell us if you see them)
- Paytm / Axis EDC / Axis CMS / Bajaj settlement reports. They come from outside companies. Say whether the ERP happens to hold any settlement or bank-credit data.
- Masters: store list (full name + short code), card-terminal (MID) to store, pickup codes, opening cash balances (a Tally cost-centre ledger). They are held in the application's own database
  and edited on screen. Just say whether the ERP holds an authoritative store list.

## 3. Hard rules (non-negotiable)

1. **Read-only, absolutely.** No INSERT/UPDATE/DELETE/DDL on anything the ERP team owns. The only objects you may create, and only if the DBA agrees in writing, are: one login role, and
   **views** in a separate schema (e.g. `mop_export`) that expose the contract in section 2. Prefer giving the DBA the SQL and letting the DBA run it.
2. **No secrets in chat or files.** Never print, paste, log or commit a password. The login password is set by the DBA (or typed by the operator) on the **server**, and the application stores it
   encrypted on server 10.0.1.98 (machine-scope Windows DPAPI), exactly as it does for its own database password. If you need to show a connection string, show it with `***` for the password.
3. **Do not hurt the ERP.** Always filter by date range and use the date index (check with `EXPLAIN` first, never `SELECT *` on a big table without a date predicate). Set
   `statement_timeout`, `idle_in_transaction_session_timeout` and a small `CONNECTION LIMIT` on the role. Prefer a **read replica / reporting copy** if one exists. Heavy pulls are for off-peak hours.
4. **Customer-sensitive data.** `TPEDCRESPONSE` can contain masked card numbers, and refund data holds customer names. In anything you send back, include only a handful of **masked** sample rows
   and never full card numbers, account numbers or personal data.
5. **Nothing is guessed.** If the ERP does not clearly hold a field, say "not found". Do not infer or invent a mapping.

## 4. Discovery checklist (what we need you to find out)

Answer each with evidence (a query and its result), not a guess:

**A. The database**
1. Server host, port, PostgreSQL version, database name, time zone setting, encoding. Is it production or a replica/reporting copy? Is there a separate reporting copy we should use instead?
2. Is this the same database as the company "central data lake", or separate?
3. Is TLS available? Which authentication method does `pg_hba.conf` use for the role (we want `scram-sha-256` over `hostssl`)?

**B. Locating the POS data**
4. Which schema/tables hold bill headers and payment lines? Search column and table names for: `bill`, `billno`, `mop`, `tender`, `tpedc`, `edc`, `pos`.
5. Row counts per month for April to November 2026, and the date column that partitions or indexes them. Are the tables partitioned? What indexes exist on the bill date?
6. For one known bill, `SB58/024800/0726`, show its row(s) so we can compare with the Excel line. Confirm `BASEAMT = BASETENDER - BASEBALANCE`.
7. Where is `TPEDCRESPONSE`, with what type, and is it stored unchanged?
8. The distinct `MOPDESC` values and counts for July 2026 (compare with the list in 2.1).
9. **How are duplicate bills deleted** (see 2.1)? Is there an `updated_at` / `modified_on` / change-log we can use to find rows that changed after the sale? How long is history kept?

**C. Locating the PTC data**
10. Tables for petty-cash vouchers; the same row/date/index information; confirm the head and ledger values of a cash deposit.
11. Does the ERP show the 628 cash in/out rows on 20 July 2026 that the old POS daily export carried (they mirror the PTC ledger)?

**D. Anything else useful** — masters (store list), settlement/bank-credit data, and the owner/contact for each table.

## 5. The read-only access to set up

Suggested shape (adjust names to your standards; the DBA runs it, not necessarily you):

```sql
-- 1. a login that can only read, with guard rails
CREATE ROLE mop_reader LOGIN PASSWORD '<set by the DBA on the server - never in chat>'
  CONNECTION LIMIT 4;
ALTER ROLE mop_reader SET default_transaction_read_only = on;
ALTER ROLE mop_reader SET statement_timeout = '15min';
ALTER ROLE mop_reader SET idle_in_transaction_session_timeout = '2min';
ALTER ROLE mop_reader SET lock_timeout = '5s';

-- 2. stable views = the contract; the ERP team can change tables underneath without breaking us
CREATE SCHEMA IF NOT EXISTS mop_export;
CREATE VIEW mop_export.pos_bill_lines AS SELECT ...;     -- columns exactly as in section 2.1
CREATE VIEW mop_export.ptc_vouchers   AS SELECT ...;     -- columns exactly as in section 2.2
GRANT USAGE ON SCHEMA mop_export TO mop_reader;
GRANT SELECT ON mop_export.pos_bill_lines, mop_export.ptc_vouchers TO mop_reader;
-- nothing else is granted

-- 3. pg_hba.conf: only the application server, encrypted
-- hostssl  <erp_db>  mop_reader  10.0.1.98/32  scram-sha-256
```

Network: **server 10.0.1.98 must be able to reach the ERP host on its PostgreSQL port** (usually 5432). The connection will be made **from 10.0.1.98**, so the allow-list entry is
that single address. (The development PC 10.0.0.156 is for development only and must not be given access.) **Testing the connection from 10.0.1.98 is done by the other session**; you only need to
confirm the firewall/pg_hba side.

The application already has the PostgreSQL driver (`psycopg 3`), so no new software is needed on the application server.

## 6. Proof that the ERP data equals the Excel data

The application holds known-good numbers from the Excel exports. Reproduce these **from the ERP views** and report ours-versus-yours side by side. Any difference is a finding to explain, never to ignore.

**POS bill lines**
| Check | Expected |
|---|---|
| 20 July 2026, row count | **40,242** (monthly export; the old daily file had 628 extra no-bill rows) |
| 20 July 2026, distinct `BILLNO` | **36,889** |
| 20 July 2026, bills with more than one payment line | **3,276** |
| 20 July 2026, digital lines (MOP not Cash and not credit note/voucher) | **17,536** |
| 20 July 2026, `Cash` total of `BASEAMT` | **12,941,046** |
| July 2026, total rows | **1,490,141** |
| July 2026, digital lines | **660,150** |
| July 2026, `Cash` rows | **726,060** |
| July 2026, credit-note and gift-voucher rows | **103,931** |

**PTC (July 2026)**
| Check | Expected |
|---|---|
| total rows | **21,690** across all 31 dates |
| `PAYMENT` / `RECEIVE` rows | **19,148 / 2,542** |
| `Cash/CMS Deposit` vouchers | **4,490**, total **489,898,591.00** |

Also give, per month April to November 2026: row count, distinct bill count, and total `BASEAMT` by `MOPDESC`, so the application can compare month by month with its own stored data.

## 7. What to hand back (one folder or message)

1. **`ERP_FINDINGS.md`** with: the answers to section 4 (each with its query and result), the proposed mapping table (ERP table.column to logical field, type, any transformation), the `CREATE VIEW` SQL for `mop_export.*`, the index and `EXPLAIN` evidence that a one-day and a one-month pull are fast, and a list of caveats and open questions.
2. **Small masked samples**: one store, one day (20 July 2026), as CSV for each of the two sources (20 to 50 rows each; masked as in rule 4).
3. The section 6 results, side by side with the expected values.
4. The exact **host, port, database, role name and schema** to use (never the password), and confirmation that the `pg_hba` entry for 10.0.1.98 is in place.

## 8. What happens next (the other session / the application side)

For awareness only; you do not need to do any of this.

1. Connect from server 10.0.1.98 with the read-only role; store the password encrypted on the server.
2. Build one **database reader per source** that produces the **same normalised table the Excel reader produces today**, so the matching engine and pages are untouched.
3. **Shadow run**: load the same months both ways and compare row by row and in rupees. A source switches to the ERP only when it matches exactly, differences explained.
4. **Incremental pulls**: by business date, always **re-pulling the last 20 days or so** because duplicate bills are deleted 7 to 15 days after the sale; restated days are reported, never frozen.
5. A setting per source (`file` or `ERP`) with the Excel Upload kept as a fallback for one to two clean months; then the Excel path is retired. The original files are archived, never deleted.
6. Estimated effort once access exists: POS bills 2 to 3 weeks (including the shadow run), PTC about 1 week, retirement of the Excel path after 1 to 2 clean months.

## 9. Questions the answers should settle

- Is a read replica / reporting copy available? Which host?
- Which tables are the source of truth for bills, payment lines and PTC vouchers?
- How are deleted or duplicate bills recorded, and is there a change timestamp?
- Is `TPEDCRESPONSE` stored verbatim?
- Does the ERP hold the store list, terminal (MID) mapping, pickup codes or opening cash?
- Who is the DBA contact for the role, the views and the `pg_hba` entry?
