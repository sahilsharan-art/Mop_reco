# Data Inventory

Walked read-only on 2026-08-24. Nothing in `D:\MOP_Reco` was moved, renamed or modified.
Working directory holds six numbered top-level folders — no `data/`, `masters/` etc. yet
(that's the CLAUDE.md §4 target layout, not what exists today).

---

## 1. Per-source inventory

### 1.1 POS bill-wise sale — `1. POS Sales\`

- **Holds:** the POS bill-wise dump. There is **no POS store-wise file** in this drop —
  see §3.
- **File:** `MOP SALE DATA (20.07.2026).xlsx` — one file, one sheet (`Sheet1`).
- **Naming pattern:** date is in the **filename**, `DD.MM.YYYY` in parentheses. No
  per-date subfolder.
- **Format / header:** `.xlsx`, header on **row 1**.
- **Date range:** single business date, `BILLDATE = 2026-07-20`, no gaps (one date).
- **Columns (12, exact spelling):**
  `ADMSITE_CODE, BILLDATE, PSITE_POSBILL_CODE, MOPDESC, MOPSTLMDESC, BASETENDER, BASEBALANCE, BASEAMT, TPEDCRESPONSE, TPBILLREFNUMBER, STORE_NAME, BILLNO`
- **Row count:** 40,870 data rows.
- **Sample row:**
  `(298, 2026-07-20, 'SSRKJGRS-66580', 'PaytmDQR', 'PaytmDQR', 199, 0, 199, '{...TPEDCRESPONSE json...}', '1176511490939257,126593352256,07202026,165229', 'JGR', 'SB82/019115/0726')`
- **MOPDESC breakdown:** Cash 20,051 · PAYTM EDC 12,424 · PaytmDQR 4,014 · Credit Note Issued 1,657 · Credit Note Received 1,626 · PAYTM DQR MOREFUN 1,055 · EDC OTHER 43. (No Bajajpay, no EDC OTHER AXIS, no Gift Voucher rows in this file.)
- **Distinct stores:** 188. **Distinct BILLNO:** 36,890 of 40,870.

### 1.2 Axis EDC settlement — `2. Settlement reports\Axis\`

- **File:** `Axis_20th_July'26.xlsx` — one sheet (`Sheet1`).
- **Naming pattern:** date in filename, `Dth_Month'YY` style. No subfolder.
- **Format / header:** `.xlsx`, header on row 1.
- **Date range:** `TRAN_DATE` all `20-Jul-2026`; `PROCESS_DATE` all `21-JUL-2026` (T+1, single value each, no gaps).
- **Columns (32, exact spelling):**
  `TERM_ID, TRAN_DATE, TRAN_TIME, BATCH_NO, CARD_TYPE, TI, CARD_NO, APPROVE_CODE, RRN, GROSS_AMT, MDR, EMI, GST, NET_AMT, MID, INTL_FLAG, CASH_TYPE, CASH_AMOUNT, PROCESS_DATE, UTR, On-Us/Of-Us, MCC-Category, Commercial Card Bifurcation, DCC, Deferred_Date, Remark, DEF_FLAG, MDR %, MCC_DESC, RBI_CATEGORY, Payer ID, Payer Name`
- **Row count:** 453 data rows.
- **Sample row:** `('22974834','20-Jul-2026','000000','00000','RC','POS','652984XXXXXX1760','111960','620111716333','601.00','4.21','0.00','0.00','596.79','037135021530036', ...)`
- No `Store Code` column exists in the file — store must be derived from `MID` (+`TERM_ID` for the one ambiguous MID, see §2/§3).

### 1.3 Bajaj Pay settlement — `2. Settlement reports\Bajaj Pay\`

- **File:** `Settlement Report (1).xlsx` — one sheet (`Settlement Report`).
- **Naming pattern:** **no date anywhere** — not in filename, not in a folder, not inferable from the "(1)" suffix.
- **Format / header:** `.xlsx`, header on row 1, **81 columns**.
- **Row count: 0.** Header only — no transaction rows at all.
- **Date range:** not determinable — no data.
- **Columns (81, exact spelling):**
  `Dealer Finnone Code, Parent Mid, MID, Routing MID, Routing TID, Legal Business Name, DBA Name, Store Alias Name, Store State, Store City, Store Pincode, Transaction Date, Transaction Id, RRN, Order Id, Transaction Status, ON US/OFF US, Transaction Type, Description, Payment Instrument, Transaction Amount, Transaction Currency, Conversion Rate, Amount In INR, QR Code/ QR Reference No, QR Name, TID, Hardware Type, Customer Id/ vpa/ credit/ debit card, Customer Name, Payment Mobile Number, Channel/Issuer, Card Scheme, Card Product, Card Burification, International/Domestic, Invoice Amount, Tip Amount, Payment Source, Auth Code, Acquirer, Bank Code, MDR %, MDR, CGST %, SGST %, IGST %, CGST on MDR, IGST on MDR, SGST on MDR, GST on MDR, Total MDR, DBD% for EMI, DBD for EMI, GST on DBD, Total DBD GST, Transaction Processing, Transaction processing Amount, CGST on TPC, SGST on TPC, IGST on TPC, GST on TPC, Total TPC, Net Amount, Settlement Status, Payout Date, Settlement Type, Settlement Date, ARN, UTR, Bank Account Number, Bank Name, Batch Number, Contactless, MCC Code, Hold Status, Hold Reason, Hold Amount, Release Date, Processing Remark, Txn Note`
- No sample row possible — file is empty of data.

### 1.4 Paytm settlement — `2. Settlement reports\Paytm\`

- **Holds:** what turns out to be a combined transaction+settlement export (see §3 — this
  is *not* the plain "transaction report" CLAUDE.md describes as a separate input).
- **Files:** three `.zip` archives, each a Spark-style export (`_SUCCESS` marker + one `part-00000-*.csv`):
  - `CITYKA63122853439026_settlement-report_1541332651440963584.zip` → 11,819 rows
  - `CityKa43339811109723_settlement-report_1541332243560480768.zip` → 5,014 rows
  - `CityKa70434858856837_settlement-report_1541332345793499136.zip` → 266 rows
- **Naming pattern:** filenames are opaque export IDs — **no date anywhere in the name**. Date must be read from the content (`transaction_date`/`settled_date`). No per-date subfolder. It is not yet established why the export is split into three files (possibly a batching/pagination artifact of the source system) — flagged as open in §3.
- **Format / header:** CSV inside zip, header on row 1, **100 columns** (not 103).
- **Date range:** `transaction_date` = `20-07-2026` throughout; `settled_date` = `21-07-2026` throughout (T+1, single value each across all 17,099 rows, no gaps).
- **Columns (100, exact spelling, comma list):**
  `transaction_id, order_id, merchant_unique_ref, transaction_date, updated_date, transaction_type, status, original_mid, merchant_name, customer_id, nickname, customer_phone_no, customer_email_id, amount, commission, gst, merchant_bill_id, payout_id, channel, utr_no, payout_date, settled_date, payment_mode, issuing_bank, pos_id, extserialno, settled_amount, prn, validate_count, prn_validate_time, prn_validated, comments, promocode, promocode_response, bank_transaction_id, link_description, reference_transaction_id, merchant_ref_id, gateway, commission_rate, product_code, gmv_tier, request_type, transaction_slab, dispute_id, card_network, feefactor, acquiring_fee, platform_fee, acquiring_tax, platform_tax, reseller_id, reseller_name, split_flag, van, ifsc_code, bank_name, beneficiary_name, purpose, issuingbankid, issuing_bank_name, maskedcardno, instunioncode, depositor_name, transfermode, transactiondate, rrncode, van_udf1, van_udf2, van_udf3, van_udf4, van_udf5, reference_no, link_customer_name, settlement_type, sub_merchant_name, tip_amount, merchant_request_id, upi_mode_sub_type, capture_id, protection_plan_provider, protection_plan_amount, split_transaction_id, bill_amount, discount_amount, txn_amount, udf1, udf2, udf3, partner_id, loan_amount, subvention_amount, subvention_type, bank_offer_discount, bank_offer_cashback, Payment_Currency_Code, Payment_Currency_Name, Payment_Currency_Amount, offer_processing_fee, offer_processing_tax`
- **Row count (all 3 files combined): 17,099.** `transaction_type`: ACQUIRING 17,098 · **REFUND 1**. `payment_mode`: UPI 16,727 · DEBIT_CARD 359 · CREDIT_CARD 13. Distinct `original_mid`: 279. Distinct `payout_id`: 279.
- **`settled_amount` is populated on all 17,099 rows** (not nil) — commission is non-zero on 576 rows, GST non-zero on 411 rows.
- **Sample row (ACQUIRING):**
  `transaction_id='20260720010940000286044359942557297', amount='99.0', commission='0.0', gst='0.0', settled_amount='99.0', settled_date='21-07-2026', payout_id='ONSPG202607210000Cityka59692909552290', utr_no='PB0295831894', payment_mode='UPI'`
- **Sample row (the one REFUND):**
  `transaction_id='20260720021062861052571300741139957', amount='5000.0', settled_amount='5000.0', reference_transaction_id='20260423011020000254071890986259957' (original sale was 23‑Apr‑2026), status='SUCCESS'`
- Values throughout are wrapped in single quotes, exactly as CLAUDE.md §5 warns.

### 1.5 CMS received — `3. CMS received\`

- **File:** `CMS 21st_July'26.xlsx` — one sheet, oddly named **`CUSTTRNDT24-08-2026577`** (the sheet name embeds `24-08-2026`, a date that matches neither the filename's 21‑Jul nor any date found in the data — looks like an internal export-run stamp, not a business date; flagged in §3).
- **Naming pattern:** date in filename (`21st_July'26`), not reflective of the full date range actually inside the file (see below). No subfolder.
- **Format / header:** `.xlsx`, header on row 1, 67 columns (not 68).
- **Date range:** `DEPDATE` spans **2026-07-20, 07-21, 07-22** (3 consecutive days, no gap). `CRDATE` spans **2026-07-21, 07-22** only.
- **Columns (67, exact spelling):**
  `TYPE, CUSCODE, PRDCODE, LOCAT, LOCATIONNAME, DepostBranch, DepostBranchName, DEPDATE, ADJDATE, CLGDATE, CRDATE, RECDATE, RTNDATE, REVDATE, REALDATE, SLIPNO., DIVCD, DIVISIONNAME, HIRCD, HIERARCHYNAME, ADJ, NOFINS, SLIPAMOUNT, RECOVEREDAMOUNT, SUBCUS, SUBCUSTOMERNAME, D.SADDTNLINFOCODE1, D.S.ADDITIONALINFORMATION1, D.SADDTNLINFOCODE2, D.S.ADDITIONALINFORMATION2, D.SADDTNLINFOCODE3, D.S.ADDITIONALINFORMATION3, D.SADDTNLINFOCODE4, D.S.ADDITIONALINFORMATION4, D.SADDTNLINFOCODE5, D.S.ADDITIONALINFORMATION5, INSL, INSTNO, INSTDATE, INSTRUMENTAMOUNT, InstrumentType, ADJUSTEDAMOUNT, RECOVEREDAMOUNT, DRNON, DRAWNONLOCATION, DRNBK, DRNBR, SUBCU, SUBCUSTOMERNAME, DRCOD, DRAWERNAME, RTN, RETURNREASON, INSADDTNLINFOCODE1, INSADDITIONALNFORMATION1, INSADDTNLINFOCODE2, INSADDITIONALINFORMATION2, INSADDTNLINFOCODE3, INS.ADDITIONALNFORMATION3, INSADDTNLINFOCODE4, INS.ADDITIONALINFORMATION4, INSADDTNLINFOCODE5, INS.ADDITIONALINFORMATION5, REMARKS1, REMARKS2, REMARKS3, PICKUPPOINTCODE`
  (Note: `RECOVEREDAMOUNT` appears twice — columns 24 and 43 — and `SUBCUSTOMERNAME` appears twice — columns 26 and 49. Duplicate header names in the raw file, not a transcription error here.)
- **Row count:** 365 data rows. `TYPE`: COL 357 · ZPO 8. **No ADJ.DB. or ADJ.CR. rows in this sample.**
- **Sample row:** `TYPE='COL', LOCATIONNAME='ARRAH', DEPDATE=2026-07-21, CRDATE=2026-07-21 (T+0), SLIPNO.=118144, SLIPAMOUNT=70820, PICKUPPOINTCODE='AX31918'`
- Confirmed by position: `LOCATIONNAME` is column **E**, `DEPDATE` **H**, `CLGDATE` **J**, `CRDATE` **K**, `SLIPNO.` **P**, `SLIPAMOUNT` **W**, `RECOVEREDAMOUNT` (first occurrence) **X**, `PICKUPPOINTCODE` **BO** — all match the letters CLAUDE.md §5 cites, despite the total column count differing (67 vs 68).
- Distinct `PICKUPPOINTCODE` values in this file: 185, **all 185 resolve cleanly against the pickup-code master** (§1.6) — zero unmapped.
- **Credit lag is narrower than the real distribution — confirmed 2026-08-29.** This file's 357 dated slips give `DEPDATE`→`CRDATE` lag **T+0: 197 · T+1: 160**, nothing beyond T+1. The user's separately-measured August workbook, covering a wider date range, shows **3,234 slips** with a real late-credit tail: T+3 **247** and T+4 **1** present (full August distribution: T+0 1,724 · T+1 1,259 · T+2 3 · T+3 247 · T+4 1, per CLAUDE.md §7). So the `3. CMS received` folder holds a much narrower extract (3 days, one credit-lag regime) than the August period it was expected to represent. **Stage 5's ageing/late-credit logic cannot be tested against a real late-credit tail until a wider CMS date range is supplied** — flagged as a blocking data gap for that stage, not a code defect.

### 1.6 CMS deposit (pickup code) master — `5.CMS DEPOSIT MASTER\`

- **File:** `CASH DEPOSIT MASTER.xlsx` — one sheet (`Sheet1`).
- **Format / header:** `.xlsx`, header on row 1, 2 columns: `PICKCODE, CODE`.
- **Row count:** 210 data rows. No effective-from/effective-to columns — flat list only.
- **Sample rows:** `('AX29035','AGT')`, `('AX29036','BPT')`, `('AX29037','DBR')`.
- **210 distinct `CODE` (store) values, 210 distinct `PICKCODE` values — clean 1:1, no duplicate pickcode, no store repeated.**
- **44 of the 210 `PICKCODE` values are NOT in `AX#####` format** — they are plain 3-letter store codes used as the pickup code itself (e.g. `BRD, HZG, PKR, MCR, RNG, UDP, DMB, BAC, DMR, VSH, UBS, AJL, LBK, SVP, SSN, MBI, ARG, KDC, REW, BPC, BGM, LMD, PBA, PKM, SBA, SBP, GCG, JBP, GPG, BDG, LSR, GSD, DRG, RLN, JGD, BSC, BGP, BSK, RSG, COB, DTG, SDH, MAR, KGT`). This is far more than the "one malformed entry (LSR)" cited in both the Word document and CLAUDE.md — see §3.
- All 44 non-`AX` codes still resolve correctly against real CMS transactions (§1.5), so they aren't broken keys — just a different naming convention for a large subset of stores.

### 1.7 MID / TID → store master — `6.MID WISE STORE CODE MASTER\`

- **File:** `MID wise store code.xlsx` — **two sheets**, not one combined master as CLAUDE.md §5 implies:
  - **`Axis mid wise store`** — header row 1, columns `MID, Store Code`. 454 data rows, 453 distinct MIDs → **1 duplicate MID** (`037215021610005`) mapping to **two different stores**: `GLA` and `MBA`. No `TERM_ID` column present at all.
  - **`Paytm Mid wise store code`** — header row 1, columns `MID, Store Code`. 450 data rows, 450 distinct MIDs, no duplicates.
- **No Bajaj MID sheet/mapping anywhere in this file.**
- **Sample rows:** Axis `('037135021530036','SWN')`; Paytm `('CityKa49316478646879','ALK')`.
- **Cross-checked against the actual settlement files:**
  - 1 Axis `MID` from the 20‑Jul EDC file (`037135034530134`) is **not in the master** — a real master-data gap.
  - The one duplicate MID (`037215021610005`) **does** appear in the 20‑Jul Axis file — with no `TERM_ID` column anywhere to disambiguate it, this MID cannot currently be resolved to a single store.
  - 9 Paytm `original_mid` values from the settlement export (all from the small third file, mixed-case, e.g. `cityka49532820809142`) are **not in the master**, case-insensitive — a real master-data gap, not just a casing issue.

### 1.8 Petty cash (PTC) ledger — `4. Petty cash ledger\`

- **File:** `PTC ENTRY - 21st_July'26.csv` — single file.
- **Naming pattern:** date in filename. No subfolder.
- **Format / header:** CSV, **header on row 3** (row 1 is a title line `Cube Name:PETTY CASH REG...`, row 2 is blank, row 3 is the real header). Encoding is `cp1252`, not UTF‑8 — it has a non-UTF-8 byte at position 6841; a loader must open it as `cp1252` or it will crash.
- **Date range:** `Billdate` = `21-JUL-2026` throughout, single date, no gap.
- **Columns (18, exact spelling):**
  `Source Short Name, Billdate, Billno, Ptcmode, Ledger, PTC Head, Sub Ledger, PTC Bill Status, Settlement Status, Terminal, Remarks, Noofprints, Createdon, Createdby, Lastmodifiedon, Lastmodifiedby, PTC Bill Amount SUM, Posted Amount SUM`
- **Row count:** 690 data rows.
- **Sample row (a deposit voucher):**
  `Source Short Name='MMR', Billdate='21-JUL-2026', Billno='SPTC/0361/Jul/26', Ptcmode='PAYMENT', Ledger='CMS-RECD.', PTC Head='Cash/CMS Deposit', PTC Bill Amount SUM='-86910', Posted Amount SUM='-86910'`
- **`PTC Bill Amount SUM` equals `Posted Amount SUM` on all 690 rows** — zero mismatches.
- **179 of 690 rows carry `PTC Head = 'Cash/CMS Deposit'`, and exactly the same 179 rows carry `Ledger = 'CMS-RECD.'` — the two identifiers never disagree in this file** (0 rows with one set but not the other). Total deposit value: ₹1,33,75,381 (negative, as PAYMENT rows).
- **PTC Head breakdown (top of 34 distinct heads):** Cash/CMS Deposit 179 · Cashier Short/Excess 109 · Local Conveyance Charges 59 · Staff Welfare Exps. 43 · Repair‑Other 34 · Scrap Sales 27 · ... (32 more heads, long tail).
- **This file uses the CLAUDE.md "August dump" column layout** (`Billdate`, `Billno`, `Ptcmode`, `Ledger`, `PTC Head`, `PTC Bill Amount SUM` — mixed case) even though it is a **July**-dated file — see §3, this breaks the month-based naming CLAUDE.md uses to distinguish the two known PTC layouts.

---

## 2. Mapping to CLAUDE.md section 5

| Logical field | CLAUDE.md says | Real column found | Match? |
|---|---|---|---|
| POS bill-wise: bill_no | `BILLNO` | `BILLNO` | ✅ Matches |
| POS bill-wise: site_ref | `PSITE_POSBILL_CODE` | `PSITE_POSBILL_CODE` | ✅ Matches |
| POS bill-wise: store | `STORE_NAME` | `STORE_NAME` | ✅ Matches |
| POS bill-wise: date | `BILLDATE` | `BILLDATE` | ✅ Matches |
| POS bill-wise: mop | `MOPDESC` | `MOPDESC` | ✅ Matches |
| POS bill-wise: amount | `BASEAMT` | `BASEAMT` | ✅ Matches |
| POS bill-wise: edc_response | `TPEDCRESPONSE` | `TPEDCRESPONSE` | ✅ Matches |
| POS bill-wise: helper cols | `Column1, Paytm trnx, Axis trnx, Column2, Column3` | **absent entirely** — file has only the 12 core columns | ⚠️ Differs — harmless (CLAUDE.md already treats these as fallback-only), but the loader must not assume they exist |
| POS store-wise: everything | `CUBENAME, STORE_NAME, BILL_MONTH, BILLDATE, MOP, START_DATE, END_DATE, COLLECTION, FINANCIALYEAR, SITECODE, MOP TYPE` | **file not present in this data drop at all** | ❌ Missing — cannot verify any of it |
| PTC: store | July=`STORE_NAME` / Aug=`Source Short Name` | `Source Short Name` | The one file we have is July-dated but matches the **August** column spec, not July |
| PTC: date | July=`BILLDATE` / Aug=`Billdate` | `Billdate` | Same — August-style |
| PTC: voucher_no | July=`BILLNO` / Aug=`Billno` | `Billno` | Same — August-style |
| PTC: mode | July=`PTCMODE` / Aug=`Ptcmode` | `Ptcmode` | Same — August-style |
| PTC: head | July=`PTCHEAD` / Aug=`PTC Head` | `PTC Head` | Same — August-style |
| PTC: ledger | July=`LEDGER` / Aug=`Ledger` | `Ledger` | Same — August-style |
| PTC: amount | July=`POSTED_AMOUNT` / Aug=`PTC Bill Amount SUM` | `PTC Bill Amount SUM` (and `Posted Amount SUM`, identical) | Same — August-style |
| PTC: remarks | July=`REMARKS` / Aug=`Remarks` | `Remarks` | Same — August-style |
| PTC: extra columns | not documented | `Sub Ledger, PTC Bill Status, Settlement Status, Terminal, Noofprints, Createdon, Createdby, Lastmodifiedon, Lastmodifiedby` | ⚠️ Present but undocumented — harmless extras, alias map should just ignore unknown extras rather than only accepting the exact listed set |
| Paytm: 17 "Use" fields | `transaction_id, order_id, transaction_date, time, status, transaction_type, original_mid, Store Code, amount, commission, gst, payout_id, utr_no, settled_amount, settled_date, payment_mode, settlement_type` | all present **except** `Store Code` and `time` (there's no separate `time` column — time is embedded inside `transaction_date`, e.g. `'20-07-2026 19:21:19'`) and `settlement_type` is present as a real column (value `'online_settlement'`) | ⚠️ `Store Code` doesn't exist — must derive via `original_mid` → MID master, same as the doc's own resolution rule says. `time` must be parsed out of `transaction_date`, not read as its own column |
| Paytm: total column count | 103 | 100 | ⚠️ Differs (minor, no fields lost) |
| Paytm: report identity | described as a separate "transaction report" input, distinct from "settlement report" | **the only Paytm file we have already carries `settled_amount`, `settled_date`, `payout_id`, `commission`, `gst` — i.e. it IS the settlement report**, not a plain transaction feed | ⚠️ Structural difference — see §3 |
| Axis EDC: 13 "Use" fields | `RRN, TRAN_DATE, TRAN_TIME, MID, TERM_ID, CARD_TYPE, GROSS_AMT, MDR, GST, NET_AMT, UTR, PROCESS_DATE, Store Code` | all present **except `TERM_ID` and `Store Code`** | ❌ `TERM_ID` is not a column in this file at all (file has `TI` and `TERM_ID` is missing — `TI` looks like a channel/device-type flag, value `'POS'`, not a terminal id). `Store Code` must be derived via `MID` |
| Axis EDC: total column count | 34 | 32 | ⚠️ Differs (minor) |
| Axis CMS settlement: cited columns (A, E, H, J, K, P, W, X, BO) | `TYPE, LOCATIONNAME, DEPDATE, CLGDATE, CRDATE, SLIPNO., SLIPAMOUNT, RECOVEREDAMOUNT, PICKUPPOINTCODE` | same names, **same letter positions** confirmed | ✅ Matches by position, despite total column count differing (67 vs 68) |
| Bajaj Pay | no per-field "Use" list given in CLAUDE.md §5 at all | 81 real columns exist, 0 data rows to validate against | ❌ Gap in CLAUDE.md itself — no logical-field mapping defined yet for Bajaj, and no data to derive one from |
| Pickup code master | `PICKCODE → CODE`, effective-dated, "one malformed entry (LSR)" | `PICKCODE, CODE`, **flat, no effective dates**, **44 non-AX-format entries**, all resolve cleanly | ⚠️ Differs materially — see §3 |
| MID/TID master | one master, `Paytm MID, Axis MID, TERM_ID, Bajaj MID → store code` | **two sheets**, Axis and Paytm only, **no TERM_ID column, no Bajaj MID at all** | ❌ Differs — no way today to disambiguate the one duplicate Axis MID, and no Bajaj mapping exists |

---

## 3. Open items now closed / still missing

**Closed by this inventory:**

1. **PTC ledger head for cash deposits** (Word doc §2 open item, CLAUDE.md correction #2) — confirmed twice over: `PTC Head = "Cash/CMS Deposit"` with `Ledger = "CMS-RECD."` on both the historical August file (checked last session) and this new July‑dated file. **The two identifiers never disagree** in either file — 0 rows where one is present without the other.
2. **Paytm settlement report sample** (Word doc Open Item #2 / Appendix E.4 #3) — now supplied, and it's better than expected: `settled_amount`, `settled_date`, `payout_id`, `commission`, `gst` are all populated on all 17,099 rows. R2's transaction→settlement leg for Paytm is now buildable and testable.
3. **A live example of a REFUND transaction** (Word doc §5.5 said none appeared in the original sample) — one genuine REFUND row is present, with `reference_transaction_id` pointing back to an original sale from 23‑Apr‑2026, giving a real case to build and test the refund-linking rule against.
4. **CMS settlement report sample with pickup codes** (Word doc Open Item #6) — supplied, 365 rows, pickup-code matching confirmed working (185/185 resolve).
5. **Store master / MID-TID master** (Word doc Open Item #7) — partially supplied (Axis + Paytm MID sheets); see gaps below.

**Still genuinely missing or newly raised:**

1. **POS store-wise dump** — not present anywhere in this drop; only the bill-wise dump exists. R3a's cash-sale figure would need to come from bill-wise `BASEAMT` where `MOPDESC='Cash'` instead (the Word doc's Appendix A.1 already notes these are equivalent), but that's a fallback, not what CLAUDE.md's loader currently expects.
2. **Bank statement** — still not supplied (Word doc Open Item #3). R2's third leg (payout → bank credit) and the CMS credit-match against the bank statement both remain untestable.
3. **Bajaj Pay data** — the settlement file supplied has 81 columns but **zero transaction rows**, and CLAUDE.md §5 never defined a field mapping for Bajaj in the first place. Both the data and the spec for this leg are open.
4. **Dedicated refund/reversal/chargeback file** — still not supplied as its own report; only the one incidental REFUND row inside the Paytm settlement export.
5. **Holiday calendar** — not supplied. Needed for every due-date computation (R2 and R3b).
6. **Effective-dated masters** — neither the pickup-code master nor the MID master carries effective-from/to dates; both are flat lists today, so the "reconciling an old date uses the mapping that applied then" rule (Word doc Appendix D.3) has nothing to run on yet.
7. **Master data gaps found directly by cross-checking the files we do have** (new, not previously flagged):
   - 1 Axis MID (`037135034530134`) seen in the 20‑Jul settlement file has no entry in the MID master.
   - 1 Axis MID (`037215021610005`) maps to **two different stores** (`GLA`, `MBA`) in the master, and there is no `TERM_ID` column anywhere to break the tie.
   - 9 Paytm `original_mid` values from the settlement export (all from the smallest of the three files) have no entry in the Paytm MID master, even case-insensitively.
8. **Pickup-code master quality is worse than documented** — 44 of 210 codes (not 1) don't follow the `AX#####` format. They still work as keys today, but this is a much bigger deviation from a clean convention than either source document suggested, and is worth a direct question back to whoever maintains that master rather than assuming it's fine.
9. **Real folder/naming convention still undecided** (Word doc Open Item #11 / Appendix E.4 #6) — none of the six folders here follow the `source/date/file` layout CLAUDE.md §4 specifies. Every file instead sits directly in a numbered source folder with the date embedded inconsistently in the filename (`20.07.2026`, `20th_July'26`, `21st_July'26`) or not at all (Bajaj, Paytm). This looks like a one-off sample drop rather than the live daily structure, but that should be confirmed rather than assumed.
10. **Two unexplained naming oddities worth a direct question:**
    - The CMS received file's sheet is named `CUSTTRNDT24-08-2026577` — the `24-08-2026` doesn't match any date found in the data (which runs 20–22 Jul). Is this just an internal export-run id, safe to ignore?
    - The Paytm settlement export arrives as **three separate zip files** for what looks like the same period. Is that a real batching rule (e.g. per legal entity, per store cluster) the loader needs to know about, or an artifact of how the file was extracted for this sample?
11. **Reason-code list, retention period, how far back to backfill on first run** — all still open exactly as listed in Word doc §14 / Appendix E.4; nothing in these folders speaks to them.

No application code was written in this step.
