# Baseline - April 2026 to September 2026 (Run #15)

Generated from Run #15 (started 2026-10-17 11:22:30.329679, finished 2026-10-17 12:02:32.616783, status `completed`). The regression baseline: every figure below is reproduced exactly by a re-run of the same inputs.

**April 2026 to the end of the months Run #11 covered are compared with Run #11 in the section 'Did the earlier months move?' below.**

## Per-month totals

| Month | Bill lines | Total sale | Digital sale | Cash sale | Matched | Open bills | Unmapped txns (by reference) | Unmapped txns (sale basis) | Store-days | Opening | Closing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| April 2026 | 705,394.0 | Rs 1,370,886,118.00 | Rs 699,491,765.86 | Rs 671,394,352.14 | 705,202.0 | 192.0 | 576.0 | 570 | 5,302.0 | Rs 40,472,370.54 | Rs 23,085,522.68 |
| May 2026 | 744,914.0 | Rs 1,374,313,694.00 | Rs 710,320,916.74 | Rs 663,992,777.26 | 741,030.0 | 3,884.0 | 4,242.0 | 4,220 | 5,765.0 | Rs 23,085,522.68 | Rs 46,477,109.86 |
| June 2026 | 754,998.0 | Rs 1,369,755,847.00 | Rs 691,001,007.30 | Rs 678,754,839.70 | 754,694.0 | 304.0 | 615.0 | 603 | 5,770.0 | Rs 46,477,109.86 | Rs 22,691,882.46 |
| July 2026 | 660,150.0 | Rs 1,031,008,098.00 | Rs 541,630,226.21 | Rs 489,377,871.79 | 659,950.0 | 200.0 | 576.0 | 547 | 6,026.0 | Rs 22,691,882.46 | Rs 16,344,516.60 |
| August 2026 | 838,614.0 | Rs 1,351,789,726.00 | Rs 708,545,462.00 | Rs 643,244,264.00 | 838,386.0 | 228.0 | 722.0 | 790 | 6,281.0 | Rs 16,344,516.60 | Rs 19,332,560.60 |
| September 2026 | 661,431.0 | Rs 1,001,896,656.00 | Rs 523,883,320.56 | Rs 478,013,335.44 | 661,115.0 | 316.0 | 963.0 | 935 | 6,391.0 | Rs 19,332,560.60 | Rs 13,533,516.65 |
| **TOTAL** | **4,365,501** | **Rs 7,499,650,139.00** | **Rs 3,874,872,698.67** | **Rs 3,624,777,440.33** | **4,360,377** | **5,124** | **7,694** | | **35,535** | Rs 40,472,370.54 | Rs 13,533,516.65 |

Unmapped transactions are shown on two bases: *by reference* (the original strip - a row counts as mapped when a bill matched its reference) and *sale basis* (SUCCESS/ACQUIRING rows the engine did not consume - the exception population). See CLAUDE.md 6.23.

## The five R3a controls, per month

(a) no negative closing - (b) no closing over the Rs 2,00,000 threshold at month end - (c) no no-deposit run of 3+ working days - (d) the estate identity holds exactly - (e) the carry-forward chain is unbroken

### April 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 46 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 23 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 20 run(s) |
| (d) estate identity holds exactly | YES | difference Rs 0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

### May 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 62 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 108 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 32 run(s) |
| (d) estate identity holds exactly | YES | difference Rs 0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

### June 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 60 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 19 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 15 run(s) |
| (d) estate identity holds exactly | YES | difference Rs -0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

### July 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 62 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 3 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 11 run(s) |
| (d) estate identity holds exactly | YES | difference Rs 0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

### August 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 62 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 8 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 15 run(s) |
| (d) estate identity holds exactly | YES | difference Rs -0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

### September 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 105 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 3 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 38 run(s) |
| (d) estate identity holds exactly | YES | difference Rs 0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

## Cash chain - month opening against the prior month's closing, per store

| Boundary | Opening | Prior month's closing | Difference | Stores that break |
|---|---|---|---|---|
| May 2026 1 | Rs 23,085,522.68 | Rs 23,085,522.68 | Rs 0.00 | none |
| June 2026 1 | Rs 46,477,109.86 | Rs 46,477,109.86 | Rs 0.00 | none |
| July 2026 1 | Rs 22,691,882.46 | Rs 22,691,882.46 | Rs 0.00 | none |
| August 2026 1 | Rs 16,344,516.60 | Rs 16,344,516.60 | Rs 0.00 | none |
| September 2026 1 | Rs 19,332,560.60 | Rs 19,332,560.60 | Rs 0.00 | none |

## Whole-range estate identity

opening 40,472,370.54 + cash sale 3,624,777,440.33 + net PTC -37,512,819.12 - deposits 3,614,203,475.10 = expected closing 13,533,516.65 vs actual 13,533,516.65 - difference **-0.00**

## Did the earlier months move? Run #11 against Run #15

| Month | Metric | Run #11 | Run #15 | Same |
|---|---|---|---|---|
| April 2026 | bill_lines | 705,394.00 | 705,394.00 | yes |
| April 2026 | total_sale | 1,370,886,118.00 | 1,370,886,118.00 | yes |
| April 2026 | digital_sale | 699,491,765.86 | 699,491,765.86 | yes |
| April 2026 | cash_sale | 671,394,352.14 | 671,394,352.14 | yes |
| April 2026 | matched | 705,202.00 | 705,202.00 | yes |
| April 2026 | open_bills | 192.00 | 192.00 | yes |
| April 2026 | open_value | 240,758.00 | 240,758.00 | yes |
| April 2026 | unmapped_txns_by_reference | 576.00 | 576.00 | yes |
| April 2026 | store_days | 6,210.00 | 5,302.00 | **NO** |
| April 2026 | opening | 40,472,370.54 | 40,472,370.54 | yes |
| April 2026 | closing | 23,085,522.68 | 23,085,522.68 | yes |
| May 2026 | bill_lines | 744,914.00 | 744,914.00 | yes |
| May 2026 | total_sale | 1,374,313,694.00 | 1,374,313,694.00 | yes |
| May 2026 | digital_sale | 710,320,916.74 | 710,320,916.74 | yes |
| May 2026 | cash_sale | 663,992,777.26 | 663,992,777.26 | yes |
| May 2026 | matched | 741,030.00 | 741,030.00 | yes |
| May 2026 | open_bills | 3,884.00 | 3,884.00 | yes |
| May 2026 | open_value | 3,606,580.00 | 3,606,580.00 | yes |
| May 2026 | unmapped_txns_by_reference | 4,242.00 | 4,242.00 | yes |
| May 2026 | store_days | 6,417.00 | 5,765.00 | **NO** |
| May 2026 | opening | 23,085,522.68 | 23,085,522.68 | yes |
| May 2026 | closing | 46,477,109.86 | 46,477,109.86 | yes |
| June 2026 | bill_lines | 754,998.00 | 754,998.00 | yes |
| June 2026 | total_sale | 1,369,755,847.00 | 1,369,755,847.00 | yes |
| June 2026 | digital_sale | 691,001,007.30 | 691,001,007.30 | yes |
| June 2026 | cash_sale | 678,754,839.70 | 678,754,839.70 | yes |
| June 2026 | matched | 754,694.00 | 754,694.00 | yes |
| June 2026 | open_bills | 304.00 | 304.00 | yes |
| June 2026 | open_value | 375,357.00 | 375,357.00 | yes |
| June 2026 | unmapped_txns_by_reference | 615.00 | 615.00 | yes |
| June 2026 | store_days | 6,210.00 | 5,770.00 | **NO** |
| June 2026 | opening | 46,477,109.86 | 46,477,109.86 | yes |
| June 2026 | closing | 22,691,882.46 | 22,691,882.46 | yes |
| July 2026 | bill_lines | 660,150.00 | 660,150.00 | yes |
| July 2026 | total_sale | 1,031,008,098.00 | 1,031,008,098.00 | yes |
| July 2026 | digital_sale | 541,630,226.21 | 541,630,226.21 | yes |
| July 2026 | cash_sale | 489,377,871.79 | 489,377,871.79 | yes |
| July 2026 | matched | 659,950.00 | 659,950.00 | yes |
| July 2026 | open_bills | 200.00 | 200.00 | yes |
| July 2026 | open_value | 226,695.00 | 226,695.00 | yes |
| July 2026 | unmapped_txns_by_reference | 573.00 | 576.00 | **NO** |
| July 2026 | store_days | 6,417.00 | 6,026.00 | **NO** |
| July 2026 | opening | 22,691,882.46 | 22,691,882.46 | yes |
| July 2026 | closing | 16,344,516.60 | 16,344,516.60 | yes |
| August 2026 | bill_lines | 838,614.00 | 838,614.00 | yes |
| August 2026 | total_sale | 1,351,789,726.00 | 1,351,789,726.00 | yes |
| August 2026 | digital_sale | 708,545,462.00 | 708,545,462.00 | yes |
| August 2026 | cash_sale | 643,244,264.00 | 643,244,264.00 | yes |
| August 2026 | matched | 838,386.00 | 838,386.00 | yes |
| August 2026 | open_bills | 228.00 | 228.00 | yes |
| August 2026 | open_value | 236,548.00 | 236,548.00 | yes |
| August 2026 | unmapped_txns_by_reference | 721.00 | 722.00 | **NO** |
| August 2026 | store_days | 6,417.00 | 6,281.00 | **NO** |
| August 2026 | opening | 16,344,516.60 | 16,344,516.60 | yes |
| August 2026 | closing | 19,332,560.60 | 19,332,560.60 | yes |

**7 metric(s) moved: [('2026-04', 'store_days', np.int64(6210), np.int64(5302)), ('2026-05', 'store_days', np.int64(6417), np.int64(5765)), ('2026-06', 'store_days', np.int64(6210), np.int64(5770)), ('2026-07', 'unmapped_txns_by_reference', np.int64(573), np.int64(576)), ('2026-07', 'store_days', np.int64(6417), np.int64(6026)), ('2026-08', 'unmapped_txns_by_reference', np.int64(721), np.int64(722)), ('2026-08', 'store_days', np.int64(6417), np.int64(6281))]** The five controls are identical month by month.

## What moved between Run #11 and Run #15, and why (all explained)

* **Store-days (all five months): 31,671 -> 29,144, i.e. 2,527 fewer rows.** Every one of the 2,527 rows is all-zero in every money column.
  Run #11 gave each store a row for every date back to the start of the range; a store the balance file does not know (it opened
  inside the range) now has no row before its first day of activity (CLAUDE.md 6.9d). 33 stores, mostly HMR, BHI, STR, DTG, COB,
  SDH, RKL, BBR, MAR, BSK, BGP, RSG, KGT, JGD. The ten stores that first showed as zero rows in the draft of this run (CHB, CPA, CTA,
  MKG, NEG, SBW, AAC, DLT, KDR, NLD) are among them and now start on their first trading day (CPA 02/09, CTA 03/09, MKG 01/09,
  NEG 02/09, SBW 11/09, CHB 14/09, NLD 14/09, AAC/KDR 26/09, DLT 30/09). Every row both runs hold agrees on total sale, digital sale,
  cash sale, opening, deposit, expense/income and closing: 0 of 29,144 rows differ, maximum difference Rs 0.00.
* **Unmapped transactions by reference: July 573 -> 576, August 721 -> 722.** Four CHARGEBACK_REVERSAL rows dated in July/August
  sit inside September's Paytm file (their ids encode 1 October); the old by-reference strip counts non-sale rows. On the sale basis
  (the exception population) Apr-Aug is identical: 570 / 4,220 / 603 / 547 / 790 and the same rupees.
* Open bill lines Apr-Aug: 4,808, Rs 46,85,938 in both runs; no bill opened or closed.

## Known open findings carried forward

The two chronic store findings in `BASELINE_APR_AUG.md` (GPB, LBL_DUMMY: 292 negative-closing store-days across the first five months), the 82 duplicate-reference gateway lines, and the unmapped-master list are unchanged by adding September; see that file, CLAUDE.md 6.23 and the Masters page.
