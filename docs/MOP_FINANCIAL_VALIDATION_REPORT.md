# MOP Reconciliation - independent financial validation (24 Nov 2026)

Independent means: each calculation was re-done separately from its written rule, in whole paise (integers) or `Decimal`, by plain loops or by SQL on the stored data, and compared with what the application produced. The audit did not reuse the application's own code to produce the expected answer. Server names, addresses and credentials are not written here.

Data: the April to 14 November 2026 run (5,584,483 digital bill lines, 228 business dates, 225 stores), on the development PC, on a copy used for acceptance tests, and in read-only queries against the production database.

## 1. What was and was not validated

| Reconciliation | Verdict |
|---|---|
| **R1** POS bill to gateway transaction | **Validated** (section 2) |
| **R2** Transaction to settlement to bank | **Not implemented**, so there is nothing to validate as a reconciliation. The bank-statement leg has no data. The application correctly shows no R2 result. What was validated is the arithmetic of the gateways' own settlement files, the ground R2 would stand on (section 3) |
| **R3a** Store cash book | **Validated** (section 4), with open differences against the ERP that are explained or waiting for Finance |
| **R3b** CMS deposit to bank credit, per pickup | **Not implemented as originally specified** (one row per pickup and slip, with statuses such as in-transit and credit-without-deposit). What the application has instead is **CMS Reco**, one block per store per deposit date, which was validated (section 5) |
| Float | **Validated** (section 6) |
| Daily store sheet, columns A to N, and the Summary matrix | **Validated** (section 7) |

## 2. R1: POS bills against gateway transactions

* **Oracle.** An independent matcher written from the rules (plain loops, exact amounts) was run beside the engine on 16 hand-worked scenarios and 300 random collision scenarios built to clash (few stores, dates, amounts and references; duplicate references; failed and refund rows; blanks). **All 316 agree.**
* **Rules checked:** a bill needs one successful match; an amount differing by one paisa is not a match and the payment stays unmapped; failed and non-sale rows are outside the pool; status is compared without regard to case; a transaction backs at most one bill (the first bill in order takes it); two raw rows carrying one reference can serve two bills (this happens with Axis); gateways apply in order; a late transaction (7 days) still matches and the gap is recorded; EDC OTHER matches only with exactly one candidate of the same store, amount and date, two candidates is ambiguous and nothing is consumed; a reference-bearing MOP never uses the fallback; refunds and chargebacks link to their original or are named exceptions; every shape of terminal reply (Paytm, Axis with and without a card number, Bajaj flat, malformed, blank, zero) yields the right references.
* **Full run.** The engine's whole-range result equals the previous per-row engine on all 5,584,483 lines (0 differences), and the server's run equals the development run on all 228 dates.
* **Live re-performance** on the stored result: every check holds. Open bill lines 1,866 (1,864 exception bills; the 2 extra lines share a bill and MOP with another line); matched 5,582,617.
* **Defect found:** D-003 (a clear error instead of a pandas crash if given duplicate Paytm ids; not reachable from ingest).
* **Known limit, not a defect:** 82 duplicate-reference gateway lines can be neither matched nor refunded (F-001); a business decision is needed.

## 3. R2 foundation: the gateways' own settlement arithmetic

Checked in the loaded data (whole paise):

| Gateway | Rows | Result |
|---|---|---|
| Paytm | 5,540,794 successful sales | **Exact on every row:** settled = amount - commission - GST (gross Rs 508.30 Cr, charges Rs 39.46 lakh, GST Rs 6.79 lakh). Every settled row has a payout id and a UTR; 67,162 payouts, none with two UTRs; **every credit is exactly T+1**, none before the sale |
| Axis EDC | 200,720 | 200,483 exact; **237 differ** (0.12%): net is not gross - MDR - GST. Net total of the differences Rs -204.66 (absolute Rs 422.66). 14 are a blank GST column on 21 June; the other 223 are mostly under Re 1 and cluster on a few August dates. Cause not established |
| Bajaj | 20,351 successful | 20,028 exact; 323 differ: 95 are one-paisa rounding; **228 are the small id-less rows already known** (net is zero, or amount plus charge), net Rs -15,108 |

What this does **not** show: that the money reached the bank. No bank statement with reference numbers exists in any source (the ERP's bank postings carry none), so a missing bank credit cannot be told from a late one. **R2 must not be reported as complete.**

## 4. R3a: store cash book

Formula checked: closing = opening + cash sale - cash deposit + net petty cash (the deposit head excluded from petty cash), opening of a day = closing of the day before.

* **Whole-paise reference** of the sheet and cash book against the engine, 89 tests (80 of them random cases): carry-forward, a store that starts mid-range, a negative closing (shown, never floored), an untouched store keeps its balance, POS rows without a bill number are not sales.
* **Live, on all 45,580 store-days:** C = D + E, G = D - F, K = I - J, M = H + E - I + L, N = H - I all hold; opening of each day equals the closing of the day before; the sheet's digital sale equals the sum of the bill-line amounts (Rs 5,219,060,086.49); cash sale Rs 4,778,779,914.41.
* **Against the ERP's own cash book** (44,337 store-days, 227 stores): cash sale, deposits and other petty cash agree on every store-day except as follows.
  * **November: net Rs -10.29 Cr on 3,142 store-days.** Explained: our November POS file is cut off at Excel's row limit (209,757 of 1,187,732 bill numbers missing); the ERP is complete. Reading POS from the ERP removes it (to be confirmed after the next run).
  * **Opening on 1 April: 21 stores, Rs 40,460.75 gross** (Rs 40,000 is one store). Constant afterwards. Needs Finance to say which opening is right.
  * **Five single days in April to October, net Rs -209.** Traced: on each, MOP equals the sum of the actual vouchers, which equal the ERP's voucher list; the ERP's *settlement summary* holds a different figure for one voucher. Reported to the DBA; closed for MOP.
* **Petty cash vouchers:** 150,947 vouchers April to October identical to the paisa against the ERP; deposits Rs 4,305,732,704.10 identical. Voucher counts per date agree except four November dates (1 to 2 vouchers edited after the file dump).
* **Negative closing balances** (control (d)): 1,643 store-days, Rs -17.11 Cr in total. An impossible book, mostly the cut-off November sales and a few stores with deposits but no sales (the code replacements NLD to NEG and KGP to MKG removed the largest cases). It is reported, not hidden.
* **Defect found and fixed:** D-001, see section 5.

## 5. CMS Reco (R3b as implemented)

* **Whole-paise reference** of every block (opening + deposit - credited same day - credited next day = closing) and the chain, 88 tests including credits two days late, credits before the deposit, credits with no date, adjustments and ZPO outside the grid, pseudo-stores listed separately, and a 30-day chain that must end at exactly zero.
* **Live:** the three structural controls hold over 225 stores x 228 days; 2,684 credits later than the next day are listed (12 flagged T+10 or more); 744 ZPO, 13 ADJ.CR. and 13 ADJ.DB. rows are listed outside the grid and match SQL.
* **D-001 (P1) found and fixed.** The key that decides which of two monthly CMS files wins left out the row TYPE, so a collection in one file and its adjustment or reversal in a later file looked like one re-exported row and only the newer survived. **5 store-days (AMG, BNK, JMI, MTR, PBM) had cash received wrong, net Rs 7,22,748 understated**, found independently twice (a recomputation of the sheet, and by comparing the slow and fast paths of the same data). Fixed; six tests, one documents the old behaviour. **Not yet in the stored run**, so the stored cash received is still wrong for those 5 store-days until the next run.

## 6. Float

Float = opening - deposit (not the closing balance). Independent streak, threshold boundary (exactly Rs 10,000.00 is not high, Rs 10,000.01 is), a per-store override, negative float kept and listed, no leak of a streak between stores, and four years of weekend and second/fourth-Saturday rules by arithmetic: 81 tests. Live: the grid equals the stored sheet on all 45,580 store-days; 2,982 store-days have a deposit larger than the opening (combined float -Rs 45.10 Cr, shown, never floored); 6 stores above the threshold on 14 November.

## 7. Daily sheet and Summary

* The matrix (Total sale, UPI, Card, Total digital, Total cash): Sale - Collected = Receivable in every column; UPI + Card = Total digital; the whole range, one month, six days and one store each equal SQL to the paisa.
* **D-011 (P2) found and fixed** in acceptance testing: after a manual or bulk match, the matrix still counted the bill as receivable while the store-wise sheet did not. After the fix both read the same.
* Provisional UPI (open bills counted as UPI until matched) is reported on the page, never hidden.

## 8. Differences that remain, and none is suppressed

| Item | Amount | State |
|---|---|---|
| Cut-off November POS file | 209,757 of 1,187,732 bill numbers; Rs 13.36 Cr of payments shown as unmapped; closing balance Rs -1.23 Cr overall | Flagged by a red banner on every page. Fixed by reading POS from the ERP (separate decision) |
| 21 stores' 1 April opening | Rs 40,460.75 | Needs Finance |
| 82 duplicate-reference gateway lines | listed as unmapped | Needs a business decision |
| 5 store-days of cash received (D-001) | Rs 7,22,748 | Fixed in the code, not yet in the stored run |
| Axis 237 rows and Bajaj 228 rows where net is not gross - charges | Rs -204.66 and Rs -15,108 | Observation for the gateway files; no action in MOP |
| Credit above deposit (Defect B) | 554 store-days, Rs 9.42 Cr | Listed on Float; a posting-date or shared-pickup-code issue, not a balance |

## 9. What this validation did not do

R2 as a reconciliation and bank credits (no data); R3b at pickup-and-slip grain (not built); the reasonableness of MDR and GST rates (only the arithmetic); the completeness of the gateway files themselves against the gateways' portals; any figure for a period after 14 November.
