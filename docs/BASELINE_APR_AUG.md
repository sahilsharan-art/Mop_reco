# Baseline — April to August 2026 (Run #11)

Generated 2026-09-26 from Run #11 (started 2026-09-24 18:36:52.738790, finished 2026-09-25 10:57:09.016150, status `completed`). This is the regression baseline for the performance rewrite: the rewritten pipeline must reproduce every figure below EXACTLY.

## Run #11 timings (the numbers section 2 must beat)

| Stage | Seconds |
|---|---|
| r1_match | 56,050.25 |
| persist_results | 1,786.49 |
| manual_overlay | 367.95 |
| ingest_paytm | 222.21 |
| ingest_pos_bill_wise | 157.25 |
| persist_store_day_agg | 88.46 |
| ingest_axis_edc | 82.68 |
| persist_digital_mode_agg | 46.25 |
| persist_mop_day_agg | 13.45 |
| exception_register | 2.99 |
| ingest_bajaj | 0.63 |
| **TOTAL** | **58,818.61 (980.3 min)** |

r1_match alone: 56,050.2s (934.2 min) against a documented ~40.5-minute baseline for this scale (CLAUDE.md §6.14) — **unexplained, roughly 23x slower than expected, not yet diagnosed.**

## Per-month totals

| Month | Bill lines | Total sale | Digital sale | Cash sale | Matched | Open bills | Unmapped txns | Store-days | Opening | Closing |
|---|---|---|---|---|---|---|---|---|---|---|
| April 2026 | 705,394 | Rs 1,370,886,118.00 | Rs 699,491,765.86 | Rs 671,394,352.14 | 705,202 | 192 | 576 | 6,210 | Rs 40,472,370.54 | Rs 23,085,522.68 |
| May 2026 | 744,914 | Rs 1,374,313,694.00 | Rs 710,320,916.74 | Rs 663,992,777.26 | 741,030 | 3,884 | 4,242 | 6,417 | Rs 23,085,522.68 | Rs 46,477,109.86 |
| June 2026 | 754,998 | Rs 1,369,755,847.00 | Rs 691,001,007.30 | Rs 678,754,839.70 | 754,694 | 304 | 615 | 6,210 | Rs 46,477,109.86 | Rs 22,691,882.46 |
| July 2026 | 660,150 | Rs 1,031,008,098.00 | Rs 541,630,226.21 | Rs 489,377,871.79 | 659,952 | 198 | 571 | 6,417 | Rs 22,691,882.46 | Rs 16,344,516.60 |
| August 2026 | 838,614 | Rs 1,351,789,726.00 | Rs 708,545,462.00 | Rs 643,244,264.00 | 838,386 | 228 | 721 | 6,417 | Rs 16,344,516.60 | Rs 19,332,560.60 |
| **TOTAL** | **3,704,070** | **Rs 6,497,753,483.00** | **Rs 3,350,989,378.11** | **Rs 3,146,764,104.89** | **3,699,264** | **4,806** | **6,725** | **31,671** | Rs 40,472,370.54 | Rs 19,332,560.60 |

## The five R3a controls, per month

(a) no negative closing · (b) no closing over the Rs 2,00,000 high-cash threshold at month end · (c) no no-deposit run of 3+ working days · (d) the estate identity holds exactly · (e) the carry-forward chain is unbroken

### April 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 46 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 23 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 20 run(s) |
| (d) estate identity holds exactly | YES | difference Rs 0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

**April FAILS at least one control.** See the diagnosis section below.

### May 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 62 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 108 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 32 run(s) |
| (d) estate identity holds exactly | YES | difference Rs 0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

**May FAILS at least one control.** See the diagnosis section below.

### June 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 60 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 19 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 15 run(s) |
| (d) estate identity holds exactly | YES | difference Rs -0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

**June FAILS at least one control.** See the diagnosis section below.

### July 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 62 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 3 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 11 run(s) |
| (d) estate identity holds exactly | YES | difference Rs -0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

**July FAILS at least one control.** See the diagnosis section below.

### August 2026
| Control | Holds | Detail |
|---|---|---|
| (a) no negative closing | **NO** | 62 store-day(s) negative |
| (b) no closing > Rs 2,00,000 at month end | **NO** | 8 store(s) over threshold |
| (c) no no-deposit run >=3 working days | **NO** | 15 run(s) |
| (d) estate identity holds exactly | YES | difference Rs -0.00 |
| (e) carry-forward chain unbroken | YES | 0 break(s) |

**August FAILS at least one control.** See the diagnosis section below.

## Cash chain — five openings, four boundary checks

| Boundary | Opening | vs prior month's closing | Difference |
|---|---|---|---|
| April 1 (from April opening-balance FILE) | Rs 40,472,370.54 | — (seed) | — |
| May 1 | Rs 23,085,522.68 | Rs 23,085,522.68 | Rs 0.00 OK |
| June 1 | Rs 46,477,109.86 | Rs 46,477,109.86 | Rs 0.00 OK |
| July 1 | Rs 22,691,882.46 | Rs 22,691,882.46 | Rs 0.00 OK |
| August 1 | Rs 16,344,516.60 | Rs 16,344,516.60 | Rs 0.00 OK |

## Computed 01-Jul opening vs. the July opening-balance file

- Computed (carried forward from the April opening file through June): **Rs 22,691,882.46**
- July opening-balance file's own total (now a CHECK, not an input): **Rs 22,808,219.46**
- Difference: **Rs -116,337.00** — reported, not reconciled away; the two disagree by this much.

## Whole-range estate identity (Apr 1 - Aug 31)

opening 40,472,370.54 + cash sale 3,146,764,104.89 + net PTC -31,191,748.83 - deposits 3,136,712,166.00 = expected closing 19,332,560.60 vs actual 19,332,560.60 — difference **0.00** (HOLDS)

Carry-forward breaks across the whole range: **0**

## Known open findings (not fixed as part of this baseline)

1. **292 negative-closing store-days across the whole range, and EVERY SINGLE ONE traces to
   just 2 of the estate's 207 stores** (confirmed by direct query — `neg['store'].nunique() == 2`).
   This is not five separate monthly defects; it is the SAME two chronically-broken stores
   persisting, via the carry-forward chain, for the entire 5 months once they first go negative
   in April — every other store's April-August book is clean.
   - **`GPB`** (a real store in the master): opening Rs 2,934 on 1 April, drained by real PTC
     expense with zero recorded cash sale and zero CMS deposit from day one — negative on
     **all 153 of 153** loaded dates. Either a genuine cash-sale data gap for this store or a
     real defect; not yet chased against the raw files.
   - **`LBL_DUMMY`** (NOT in the store master at all; a real, separate `LBL` store exists):
     negative on **139 of 153** dates, starting from a single 15-April event where
     `cash_sale = -41337`. Traced to `store_sheet.cash_sale_by_store_date()`, which groups on
     the POS file's raw `STORE_NAME` string and never validates it against the store master —
     unlike R1's Axis MID resolver, there is no "unmapped store, reported by name" path for
     R3a's own store dimension. This looks like a real rule-5 gap, not just bad April data.
   - The (b) high-cash-threshold and (c) no-deposit-run failures shown per month above are
     NOT caused by these two stores (both run chronically negative, never high-cash, and
     never register a positive `cash_sale` day for the no-deposit-run check to count) — they
     are separate, ordinary findings across the other 205 stores and have not been broken
     down further in this pass.
2. **A real discrepancy between this chained run and July's own standalone acceptance
   numbers.** CLAUDE.md §7 records July run alone (seeded from July's OWN opening-balance
   file) as **0** negative-closing store-days. This run — July as month 4 of a chain seeded
   only from April's opening file — shows **62**. The two GPB/LBL_DUMMY stores above are
   almost certainly why: carried forward from April, their broken opening never gets the
   chance to be corrected by July's own (different) opening figure the standalone run used.
   Consistent with the Rs 1,16,337 aggregate gap found above — individual stores can differ
   by more than the aggregate difference suggests, if they partly cancel out.
3. **r1_match took 15.6 hours (56,050s) vs. the ~40.5-minute figure CLAUDE.md's own §6.14
   note records for this scale — unexplained, needs its own diagnosis separate from the
   vectorization work in section 2.**
