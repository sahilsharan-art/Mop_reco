# What MOP still takes from files because the ERP / data platform does not have it

Written 23 Nov 2026 for the DBA (01-DBA session) and the platform owner. Context: MOP Reconciliation (server 10.0.1.98) now reads POS bill lines and
petty-cash vouchers from `gold_mop` through the shared read-only login `ckpost` (thank you - verified, see section 1). The items below are
**everything else MOP uses, still delivered as Excel / CSV / zip files that someone downloads and copies into a folder**. Please tell us, for each,
whether the platform can receive it (and from where), and if so create a read-only view in `gold_mop` and grant `ckpost` SELECT, exactly as for the POS view.

No passwords are in this file. Read-only, by date range, from 10.0.1.98 only, `application_name = mop_reco`, same rules as before.

## 1. Already served by the platform (for context)

| Data | View | Status |
|---|---|---|
| POS bill lines | `gold_mop.pos_bill_lines` | complete; 20 Jul 2026 agrees with our Excel on every bill, amount and payment reference |
| Petty-cash vouchers | `gold_mop.ptc_vouchers` | complete; 20 Jul 2026 agrees on all 628 vouchers |
| Store / site list | `silver.dim_site` | usable; see the two requests in section 4 |

## 2. NOT in the platform - priority 1 (these decide whether a bill was actually paid and the money reached the bank)

These four files are the other half of the reconciliation. They are produced OUTSIDE Citykart (payment companies and the bank), so the platform has to
receive them (portal download, SFTP, API, e-mailed report ...) - we do not know which route is possible, please advise.

| # | Dataset | Who produces it | What MOP reads from it (fields) | Size / frequency |
|---|---|---|---|---|
| 1 | **Paytm transaction + settlement report** | Paytm (settlement export, delivered as zip parts of CSV) | transaction_id, order_id, transaction_date (date AND time), status, transaction_type (ACQUIRING / REFUND / CHARGEBACK / CHARGEBACK_REVERSAL / REVERSAL), reference_transaction_id (for refunds and chargebacks), original_mid, amount, commission, gst, payout_id, utr_no, settled_amount, settled_date, payment_mode (UPI / DEBIT_CARD / CREDIT_CARD). About 100 columns; MOP uses ~16. | about 640,000 rows a month; daily delivery wanted |
| 2 | **Axis EDC (card) settlement report** | Axis Bank | RRN, TRAN_DATE, MID, TERM_ID, CARD_TYPE, GROSS_AMT, MDR, GST, NET_AMT, UTR, PROCESS_DATE (TRAN_TIME is always the placeholder 000000 and is useless) | about 18,000 rows a month |
| 3 | **Bajaj Pay settlement report** | Bajaj Finance | Transaction Id, TID, Transaction Date (date AND time), Transaction Amount, Transaction Status (mixed case), MID, Net Amount, Total MDR | hundreds to a few thousand rows a month, growing |
| 4 | **Axis CMS cash-pickup credit report** | Axis Bank cash management | TYPE (COL / ADJ.CR. / ADJ.DB. / ZPO), DEPDATE, CRDATE, CLGDATE, SLIPNO, SLIPAMOUNT, ADJ, RECOVEREDAMOUNT, LOCATIONNAME, LOCAT, PICKUPPOINTCODE (67 columns; two column names appear twice, so position matters) | about 4,600 rows a month |

Why they matter: matching a bill to its payment (R1), checking the gateway paid the right amount net of charges on time (R2), and checking that cash handed to the agency reached the bank (R3b). Today a person
downloads each file monthly and copies it to the server; a missing or cut-off file silently changes the figures (the November POS file was cut off at Excel's row limit).

## 3. NOT in the platform - priority 2 and 3

| # | Dataset | Used for | Notes |
|---|---|---|---|
| 5 | **Bank statement credits** (UTR, value date, amount) | R2 level 3: payout -> bank credit | Never supplied; MOP cannot yet prove money reached the bank |
| 6 | **Card-terminal (MID) -> store mapping**: Paytm MID, Axis MID, and Bajaj TID | assigning a gateway transaction to a store when there is no bill behind it | Held as a hand-maintained master in MOP today. Is there an authoritative source (finance / the payment companies' merchant lists)? |
| 7 | **CMS pickup code -> store** (and the location split where one code serves two stores, e.g. AX31918 = APM and MMR) | assigning a bank cash credit to a store | Hand-maintained in MOP today |
| 8 | **Opening cash balance by store** (Tally ledger "Cash Drawer", cost centre = store) | the start of each store's cash book | One-off as at 1 July 2026, then derived; a later physical-cash figure would also help |
| 9 | **Customer refunds paid** (finance team workbook: transaction number, Zoho ticket, customer name, store, transaction date, process date, amount) | closing "payment received but no bill" exceptions that were refunded to the customer | Held in the finance team's Excel today |
| 10 | **Public-holiday calendar** | due dates ("first working day after") | None supplied |
| 11 | **CMS credit cycle per store (T+0 or T+1)** | whether a deposit is late | In no master today |

## 4. Small requests on what the platform already has

1. **`silver.dim_site`: please confirm the status values.** MOP sees 26 ACTIVE stores that its own master lacks (the new stores of Sep-Nov: CTA, DBY, MSW, CPA, MIC, PDH, AKN, DLT, KDR, AAC, SAH, JVC, GWH, PAR, MCN, NEG, GWG, MKG, GWL, BWR, NAH, SBW, BRN, DBH, CHB, LMS). Their `last_bill_date` reads 2000-01-01, so it is not
   a reliable "last sale" - is there a better column? Is `opening_date` the real opening date?
2. **Two codes MOP holds that are not in `dim_site`:** `KGP` (replaced by MKG) and `FARUKHNAGAR` (looks malformed). Which is right?
3. **One name differs:** MCR is `(MADHEPURA-2)` in MOP and `(MADHEPURA-2-MCR)` in the platform.
4. **The `bronze` schema is hidden from `ckpost`.** Does it already hold any of items 1 to 11 above (for example gateway files that Pulse or Finance load)? If so, please expose a view.
5. **`meta.v_freshness`:** please add rows for `gold_mop.pos_bill_lines` and `gold_mop.ptc_vouchers` so MOP can read "loaded up to" and refuse to reconcile a day that is not yet complete.

## 5. What we need back

For each numbered item: **can the platform have it (yes / no / later), from which source, how often, and who owns it**; for the ones it can, the view name and a `GRANT SELECT` to `ckpost`.
Nothing is read until the owner agrees and the grant exists. MOP will confirm each new view against the files it already has (the same side-by-side proof used for POS and petty cash) before it stops using the file.

## 6. Added 24 Nov 2026 (audit): five vouchers where `store_settlement_lines` disagrees with `ptc_vouchers`

MOP compared its cash book with the platform's `store_settlement_lines` (CashSummary) for April to November. It agrees except for five store-days in April to October, and on each the platform's **settlement summary** holds a different figure from the platform's **own voucher** for one petty-cash voucher. MOP follows the vouchers.

| Store | Date | Head | `ptc_vouchers` | `store_settlement_lines` |
|---|---|---|---|---|
| JGB | 04/04/2026 | Printing & Stationery (SPTC/0017/Apr/26) | -782 | -482 |
| SKP | 02/05/2026 | Repair-Computer (SPTC/0172/May/26) | -190 | -290 |
| MCR | 14/07/2026 | Local Conveyance Charges (SPTC/0429/Jul/26) | -50 | -100 |
| MDB | 11/07/2026 | Cashier Short/Excess (SPTC/0411/Jul/26) | -17 (PAYMENT) | +17 |
| MBR | 19/04/2026 | Staff Welfare Exps. (SPTC/0065/Apr/26) | -125 | -100 |

Question: which is the correct figure, and why does the summary differ (a voucher edited after settlement? a different rule for Cashier Short/Excess?). No action is needed from MOP either way; it is raised so the two views can be made to agree.
