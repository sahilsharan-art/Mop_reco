# Cash receivable / Excess credited — diagnostic report

**Status: diagnostic only. No display, rule or schema change was made for this
report.** It answers whether the "Excess credited Rs 80.16 L" tile on
**Summary → MOP RECO_Store Wise** is real money or an artefact, and what the
correct single "cash with the agency, not yet in the bank" figure should be
for July 2026.

Run against **run #1** (started 2026-09-04 11:48:32, status `completed`),
using `mop_reco.store_sheet_source.build(session, run_id, date_from, date_to)`
— the exact function `app/Summary.py` calls — with `date_from = 2026-07-01`,
`date_to = 2026-07-31` and no store filter, which is what the sidebar's empty
From/To/"All stores" resolves to. **This is the same frame the page tiles are
computed from.** All figures below are read from that one `bundle.sheet`
DataFrame (6,076 rows, 196 stores, 31 dates) plus the CMS detail frame
(`store_sheet_source.cms_frame(session)`, 4,593 rows) that feeds its column J.

Diagnostic script: `scratchpad/diag_cash_receivable.py` (not part of the
application; kept for reproducing every number in this report).

---

## 1. Reconcile the three numbers

| Figure | Value | Expression |
|---|---|---|
| sum(K) where K > 0 ("excess credited") | **Rs 80,16,340.00** (63 store-days) | `sheet[sheet.cash_receivable > 0.005]["cash_receivable"].sum()` |
| sum(K) where K < 0 ("cash receivable") | **-Rs 1,45,87,717.00** (155 store-days), shown positive as Rs 1,45,87,717.00 | `sheet[sheet.cash_receivable < -0.005]["cash_receivable"].sum()` |
| net(K), all 6,076 rows | **-Rs 65,71,377.00** | `sheet["cash_receivable"].sum()` |
| total column I (cash deposit) | **-Rs 48,98,98,591.00** | `sheet["cash_deposit"].sum()` |
| total column J (cash received) | **Rs 48,33,27,214.00** | `sheet["cash_received"].sum()` |
| total I + total J | **-Rs 65,71,377.00** | matches net(K) exactly |

**net(K) = total J − total I is confirmed exactly: -65,71,377.00 = -48,98,98,591.00 + 48,33,27,214.00.**
Row-level check: `(sheet.cash_receivable - (sheet.cash_deposit + sheet.cash_received)).abs().max()` = **0.000000** over all 6,076 rows — column K is computed correctly as `I + J` on every row; this is a formula check, not a business check.

**Store set / date range used, and whether it matches the page:** all 196
stores loaded under run #1, 2026-07-01 to 2026-07-31, no store filter — this
is exactly what `Summary.py` shows by default (From/To empty resolves to the
full loaded range; Store defaults to "All stores"). Confirmed by reading
`app/Summary.py`: it calls the identical `store_sheet_source.build(session,
run.id, date_from, date_to)` and reads `bundle.sheet` for every tile.

**On the arithmetic claim ("1.46 Cr − 80.16 L ≈ 65.8 L, the same number split
in two"):** this is arithmetically exact —
`Rs 1,45,87,717.00 − Rs 80,16,340.00 = Rs 65,71,377.00` — but it is **not, by
itself, evidence of anything**. `net(K) = sum(K>0) + sum(K<0)` is a tautology
for *any* distribution of a signed column into its positive and negative
parts; it is true whether every K>0 row is a data-entry artefact or every one
of them is a genuine excess credit. It says nothing about which is true. The
actual question — is the Rs 80.16 L real — is answered in sections 3–4 below,
by testing the *rows*, not the totals.

**On the user's independent check** (deposits Rs 2,48,14,361 vs credits
Rs 1,82,43,109, net Rs 65,71,252): the **net** figure (Rs 65,71,252) agrees
with this report's net(K) of Rs 65,71,377 to within **Rs 125** (0.0002%) —
a second, independent computation landing on essentially the same net gap is
a useful cross-check and this report treats that net as corroborated. But the
two **gross** figures do not match this report's gross totals — deposits
Rs 2,48,14,361 and credits Rs 1,82,43,109 are roughly **1/20th** of this
report's total I (Rs 4,89,898,591) and total J (Rs 4,83,327,214). Whatever
population the independent check summed over (a subset of stores, a subset
of dates, or only the store-days already flagged), it was **not** the whole
July I/J totals — the brief did not specify its scope, so this report cannot
reconcile the two gross figures, only note that both nets land in the same
place.

---

## 2. Every excess row (K > 0)

63 store-days, sorted by K descending. `pickup_code` is every distinct
`PICKUPPOINTCODE` from the CMS rows (TYPE ∈ {COL, ADJ.CR., ADJ.DB.}) whose
`DEPDATE` falls on that store-date — i.e., what actually fed J that day.

| store | business_date | I (deposit) | J (credit) | K | pickup_code |
|---|---|---:|---:|---:|---|
| ANG | 2026-07-15 | -128,540.00 | 815,638.00 | 687,098.00 | AX32439 |
| KRC | 2026-07-14 | -165,110.00 | 798,820.00 | 633,710.00 | AX31613 |
| GDA | 2026-07-15 | -137,680.00 | 741,760.00 | 604,080.00 | AX28950 |
| KRC | 2026-07-28 | -151,590.00 | 573,943.00 | 422,353.00 | AX31613 |
| ANG | 2026-07-07 | -142,670.00 | 486,870.00 | 344,200.00 | AX32439 |
| APM | 2026-07-28 | -51,770.00 | 375,154.00 | 323,384.00 | AX31918 |
| NTV | 2026-07-28 | -66,716.00 | 349,628.00 | 282,912.00 | AX28970 |
| NTV | 2026-07-04 | -118,685.00 | 362,781.00 | 244,096.00 | AX28970 |
| JRC | 2026-07-06 | -131,400.00 | 371,160.00 | 239,760.00 | AX38736 |
| BRP | 2026-07-21 | -59,800.00 | 287,475.00 | 227,675.00 | AX33249 |
| NTV | 2026-07-21 | -71,500.00 | 290,800.00 | 219,300.00 | AX28970 |
| ANG | 2026-07-09 | -115,020.00 | 284,492.00 | 169,472.00 | AX32439 |
| KRC | 2026-07-23 | -139,235.00 | 303,645.00 | 164,410.00 | AX31613 |
| KRC | 2026-07-20 | -357,850.00 | 521,500.00 | 163,650.00 | AX31613 |
| ANM | 2026-07-28 | -49,250.00 | 209,377.00 | 160,127.00 | AX31911 |
| BRP | 2026-07-03 | -138,756.00 | 290,875.00 | 152,119.00 | AX33249 |
| BRP | 2026-07-08 | -142,833.00 | 279,025.00 | 136,192.00 | AX33249 |
| BHR | 2026-07-29 | -178,300.00 | 303,630.00 | 125,330.00 | AX28943 |
| ANG | 2026-07-04 | -156,730.00 | 276,767.00 | 120,037.00 | AX32439 |
| BRP | 2026-07-13 | 0.00 | 117,132.00 | 117,132.00 | AX33249 |
| RLN | 2026-07-07 | 0.00 | 115,646.00 | 115,646.00 | RLN |
| BRP | 2026-07-27 | -305,046.00 | 405,881.00 | 100,835.00 | AX33249 |
| GBG | 2026-07-09 | -81,324.00 | 180,051.00 | 98,727.00 | AX31984 |
| DRA | 2026-07-24 | -69,200.00 | 164,600.00 | 95,400.00 | AX28947 |
| MNP | 2026-07-18 | -98,900.00 | 194,250.00 | 95,350.00 | AX38738 |
| LAM | 2026-07-27 | -214,600.00 | 303,500.00 | 88,900.00 | AX28959 |
| LMT | 2026-07-13 | 0.00 | 87,100.00 | 87,100.00 | AX28963 |
| PHB | 2026-07-23 | 0.00 | 86,875.00 | 86,875.00 | AX31982 |
| DMB | 2026-07-08 | -73,895.00 | 160,529.00 | 86,634.00 | DMB |
| GRK | 2026-07-04 | -69,820.00 | 155,610.00 | 85,790.00 | AX28954 |
| LMT | 2026-07-27 | -226,000.00 | 311,100.00 | 85,100.00 | AX28963 |
| PDR | 2026-07-04 | -95,068.00 | 179,071.00 | 84,003.00 | AX28971 |
| KDP | 2026-07-18 | -26,329.00 | 108,057.00 | 81,728.00 | AX32435 |
| PKM | 2026-07-10 | -75,855.00 | 155,614.00 | 79,759.00 | PKM |
| BNK | 2026-07-06 | -155,732.00 | 234,093.00 | 78,361.00 | AX38733 |
| SVP | 2026-07-16 | -84,613.00 | 162,268.00 | 77,655.00 | SVP |
| BIA | 2026-07-06 | -392,418.00 | 469,965.00 | 77,547.00 | AX28944 |
| GLA | 2026-07-23 | -42,500.00 | 117,400.00 | 74,900.00 | AX28952 |
| PKM | 2026-07-13 | -197,028.00 | 266,970.00 | 69,942.00 | PKM |
| NTV | 2026-07-30 | -58,100.00 | 127,800.00 | 69,700.00 | AX28970 |
| BIA | 2026-07-20 | -233,728.00 | 303,280.00 | 69,552.00 | AX28944 |
| JMI | 2026-07-23 | -55,169.00 | 112,446.00 | 57,277.00 | AX31939 |
| ARG | 2026-07-18 | 0.00 | 56,003.00 | 56,003.00 | ARG |
| PHB | 2026-07-09 | 0.00 | 52,538.00 | 52,538.00 | AX31982 |
| BIA | 2026-07-17 | -122,780.00 | 173,419.00 | 50,639.00 | AX28944 |
| CNW | 2026-07-10 | -34,765.00 | 82,485.00 | 47,720.00 | AX34422 |
| LTL | 2026-07-27 | -113,500.00 | 158,700.00 | 45,200.00 | AX28965 |
| LTL | 2026-07-13 | 0.00 | 43,400.00 | 43,400.00 | AX28965 |
| CNW | 2026-07-20 | -31,600.00 | 73,450.00 | 41,850.00 | AX34422 |
| BIA | 2026-07-16 | -121,966.00 | 157,524.00 | 35,558.00 | AX28944 |
| BIA | 2026-07-15 | -121,245.00 | 155,645.00 | 34,400.00 | AX28944 |
| BIA | 2026-07-22 | -175,575.00 | 209,228.00 | 33,653.00 | AX28944 |
| BIA | 2026-07-02 | -153,984.00 | 186,756.00 | 32,772.00 | AX28944 |
| VSW | 2026-07-18 | 0.00 | 31,864.00 | 31,864.00 | AX31940 |
| PLB | 2026-07-10 | -35,720.00 | 65,020.00 | 29,300.00 | AX28972 |
| SNP | 2026-07-23 | -53,712.00 | 81,052.00 | 27,340.00 | AX32437 |
| SNP | 2026-07-29 | -8,400.00 | 34,626.00 | 26,226.00 | AX32437 |
| BIA | 2026-07-21 | -94,820.00 | 119,917.00 | 25,097.00 | AX28944 |
| CNW | 2026-07-23 | 0.00 | 18,920.00 | 18,920.00 | AX34422 |
| JRC | 2026-07-07 | -33,800.00 | 33,830.00 | 30.00 | AX38736 |
| JHM | 2026-07-29 | -52,935.00 | 52,945.00 | 10.00 | AX30495 |
| VSW | 2026-07-16 | -40,736.00 | 40,737.00 | 1.00 | AX31940 |
| DMB | 2026-07-23 | -59,012.00 | 59,013.00 | 1.00 | DMB |

**Count: 63. Total K (excess): Rs 80,16,340.00.**

Expression: `excess = sheet[sheet.cash_receivable > 0.005].sort_values("cash_receivable", ascending=False)`.

---

## 3. Adjacent-date test (same store, D-1 and D+1 only)

For every excess row, `K(D-1)` and `K(D+1)` are the same store's `cash_receivable`
on the calendar day before/after, read from the same `sheet` (`sheet.set_index(["store","date"])["cash_receivable"]`).
"Fully offset" = a neighbour with `K < 0` and `|K(D) + K(neighbour)| ≤ Rs 100`;
"partially offset" = a negative neighbour that reduces the magnitude but not
within Rs 100; "unexplained" = no negative neighbour on either side.

| store | date | K(D-1) | K(D) | K(D+1) | verdict |
|---|---|---:|---:|---:|---|
| ANG | 2026-07-15 | -176,155.00 | 687,098.00 | 0.00 | partially offset by D-1 (residual 510,943.00) |
| KRC | 2026-07-14 | -633,710.00 | 633,710.00 | 0.00 | fully offset by D-1 |
| GDA | 2026-07-15 | -604,080.00 | 604,080.00 | 0.00 | fully offset by D-1 |
| KRC | 2026-07-28 | -422,353.00 | 422,353.00 | 0.00 | fully offset by D-1 |
| ANG | 2026-07-07 | -344,200.00 | 344,200.00 | -169,472.00 | fully offset by D-1 |
| APM | 2026-07-28 | -323,384.00 | 323,384.00 | 0.00 | fully offset by D-1 |
| NTV | 2026-07-28 | -282,912.00 | 282,912.00 | -69,700.00 | fully offset by D-1 |
| NTV | 2026-07-04 | -244,096.00 | 244,096.00 | 0.00 | fully offset by D-1 |
| JRC | 2026-07-06 | 0.00 | 239,760.00 | 30.00 | unexplained |
| BRP | 2026-07-21 | -227,675.00 | 227,675.00 | 0.00 | fully offset by D-1 |
| NTV | 2026-07-21 | -219,300.00 | 219,300.00 | 0.00 | fully offset by D-1 |
| ANG | 2026-07-09 | -169,472.00 | 169,472.00 | 0.00 | fully offset by D-1 |
| KRC | 2026-07-23 | -164,410.00 | 164,410.00 | 0.00 | fully offset by D-1 |
| KRC | 2026-07-20 | 0.00 | 163,650.00 | 0.00 | unexplained |
| ANM | 2026-07-28 | -160,127.00 | 160,127.00 | 0.00 | fully offset by D-1 |
| BRP | 2026-07-03 | -152,119.00 | 152,119.00 | 0.00 | fully offset by D-1 |
| BRP | 2026-07-08 | -136,192.00 | 136,192.00 | 0.00 | fully offset by D-1 |
| BHR | 2026-07-29 | -125,330.00 | 125,330.00 | 0.00 | fully offset by D-1 |
| ANG | 2026-07-04 | -120,037.00 | 120,037.00 | 0.00 | fully offset by D-1 |
| BRP | 2026-07-13 | 0.00 | 117,132.00 | 0.00 | unexplained |
| RLN | 2026-07-07 | 0.00 | 115,646.00 | -115,646.00 | fully offset by D+1 |
| BRP | 2026-07-27 | 0.00 | 100,835.00 | 0.00 | unexplained |
| GBG | 2026-07-09 | -98,727.00 | 98,727.00 | 0.00 | fully offset by D-1 |
| DRA | 2026-07-24 | -95,400.00 | 95,400.00 | 0.00 | fully offset by D-1 |
| MNP | 2026-07-18 | -95,350.00 | 95,350.00 | 0.00 | fully offset by D-1 |
| LAM | 2026-07-27 | 0.00 | 88,900.00 | 0.00 | unexplained |
| LMT | 2026-07-13 | 0.00 | 87,100.00 | 0.00 | unexplained |
| PHB | 2026-07-23 | 0.00 | 86,875.00 | -86,875.00 | fully offset by D+1 |
| DMB | 2026-07-08 | -86,634.00 | 86,634.00 | 0.00 | fully offset by D-1 |
| GRK | 2026-07-04 | 0.00 | 85,790.00 | 0.00 | unexplained |
| LMT | 2026-07-27 | 0.00 | 85,100.00 | 0.00 | unexplained |
| PDR | 2026-07-04 | -84,003.00 | 84,003.00 | 0.00 | fully offset by D-1 |
| KDP | 2026-07-18 | -81,728.00 | 81,728.00 | 0.00 | fully offset by D-1 |
| PKM | 2026-07-10 | -79,754.00 | 79,759.00 | -69,942.00 | fully offset by D-1 |
| BNK | 2026-07-06 | 0.00 | 78,361.00 | 0.00 | unexplained |
| SVP | 2026-07-16 | -77,655.00 | 77,655.00 | 0.00 | fully offset by D-1 |
| BIA | 2026-07-06 | 0.00 | 77,547.00 | 0.00 | unexplained |
| GLA | 2026-07-23 | -74,900.00 | 74,900.00 | 0.00 | fully offset by D-1 |
| PKM | 2026-07-13 | 0.00 | 69,942.00 | 0.00 | unexplained |
| NTV | 2026-07-30 | -69,700.00 | 69,700.00 | 0.00 | fully offset by D-1 |
| BIA | 2026-07-20 | 0.00 | 69,552.00 | 25,097.00 | unexplained |
| JMI | 2026-07-23 | -57,277.00 | 57,277.00 | 0.00 | fully offset by D-1 |
| ARG | 2026-07-18 | 0.00 | 56,003.00 | -56,003.00 | fully offset by D+1 |
| PHB | 2026-07-09 | 0.00 | 52,538.00 | -52,538.00 | fully offset by D+1 |
| BIA | 2026-07-17 | 35,558.00 | 50,639.00 | 0.00 | unexplained |
| CNW | 2026-07-10 | -47,720.00 | 47,720.00 | 0.00 | fully offset by D-1 |
| LTL | 2026-07-27 | 0.00 | 45,200.00 | 0.00 | unexplained |
| LTL | 2026-07-13 | 0.00 | 43,400.00 | 0.00 | unexplained |
| CNW | 2026-07-20 | -41,850.00 | 41,850.00 | 0.00 | fully offset by D-1 |
| BIA | 2026-07-16 | 34,400.00 | 35,558.00 | 50,639.00 | unexplained |
| BIA | 2026-07-15 | 0.00 | 34,400.00 | 35,558.00 | unexplained |
| BIA | 2026-07-22 | 25,097.00 | 33,653.00 | 0.00 | unexplained |
| BIA | 2026-07-02 | 0.00 | 32,772.00 | 0.00 | unexplained |
| VSW | 2026-07-18 | 0.00 | 31,864.00 | -31,864.00 | fully offset by D+1 |
| PLB | 2026-07-10 | -29,300.00 | 29,300.00 | 0.00 | fully offset by D-1 |
| SNP | 2026-07-23 | -27,340.00 | 27,340.00 | 0.00 | fully offset by D-1 |
| SNP | 2026-07-29 | -26,226.00 | 26,226.00 | 0.00 | fully offset by D-1 |
| BIA | 2026-07-21 | 69,552.00 | 25,097.00 | 33,653.00 | unexplained |
| CNW | 2026-07-23 | 0.00 | 18,920.00 | -18,920.00 | fully offset by D+1 |
| JRC | 2026-07-07 | 239,760.00 | 30.00 | 0.00 | unexplained |
| JHM | 2026-07-29 | 0.00 | 10.00 | 0.00 | unexplained |
| VSW | 2026-07-16 | 0.00 | 1.00 | 0.00 | unexplained |
| DMB | 2026-07-23 | 0.00 | 1.00 | 0.00 | unexplained |

### Summary of the ±1-day-only test

| Bucket | Rows | Rupees |
|---|---:|---:|
| Fully offset (D-1 or D+1, same store) | 38 | **Rs 57,64,812.00** |
| Partially offset (same store) | 1 | Rs 6,87,098.00 (of which Rs 1,76,155 is explained, Rs 5,10,943 is not) |
| Unexplained within ±1 day, same store | 24 | Rs 15,64,430.00 |
| **Total** | **63** | **Rs 80,16,340.00** |

**72% of the Rs 80.16 L (Rs 57.65 L) is fully explained by a same-store,
single-day timing shift** — a deposit on day D with no matching credit that
day, and a credit on the adjacent day with little or no deposit of its own,
net to (near) zero across the two days. That is a genuine date-attribution
pattern, consistent with the CMS file's own `DEPDATE` for these slips lagging
(or leading) the store's actual PTC deposit date by exactly one day — **this
is what the diagnosis suspects it is: mechanically a timing artefact, not new
money.** The remaining 28% (Rs 22.51 L across 1 partial + 24 unexplained rows)
is **not** explained by a same-store ±1-day shift alone — see section 4 for
part of that, and the verdict for what is left after it.

---

## 4. Cross-store contamination check

### 4a. AX31918 (APM / MMR) — the documented, already-fixed defect A

`ingest_axis_cms()` (in `app/mop_reco/ingest.py`) is called by
`store_sheet_source.cms_frame()` — **the same function the Summary page
uses** — and it resolves every pickup code via
`masters.resolve_pickup_store(session, pickup_code, location, business_date)`,
which consults the `pickup_location` master (LOCAT-based split) for any code
registered there, and today that is **only** `AX31918`. **Confirmed: the
split is applied on this exact code path**, not bypassed for the page.

| | rows | store resolved |
|---|---:|---|
| AX31918, TYPE=COL | 44 | MMR: 25, APM: 19 |

No AX31918 row is unresolved (`location_unresolved`/`unmapped`) in this data —
every one of the 44 rows carries a recognised `LOCAT` and resolves cleanly.

Bare `pickup_code` master alone (ignoring location) resolves `AX31918` to a
single store: **MMR** (`resolve_store(session, "pickup_code", "AX31918",
date_to)` → `store='MMR', status='resolved'`).

| | I (actual, WITH split) | J (actual, WITH split) | K (actual) |
|---|---:|---:|---:|
| APM | -19,33,458.00 | 19,33,458.00 | **0.00** |
| MMR | -29,30,660.00 | 29,30,650.00 | **-10.00** |

| | I (unchanged) | J (simulated, WITHOUT split — all AX31918 → MMR) | K (simulated) |
|---|---:|---:|---:|
| APM | -19,33,458.00 | 0.00 | **-19,33,458.00** (a large phantom receivable) |
| MMR | -29,30,660.00 | 48,64,108.00 | **+19,33,448.00** (a large phantom excess) |

**Conclusion: defect A is fixed on this code path, and it is working.** With
the split applied, APM's net K for the whole month is exactly Rs 0.00 and
MMR's is -Rs 10 (immaterial). APM does appear once in the section-2 excess
list (28 July, K=+323,384) — the adjacent-date test in section 3 shows this
is **fully offset by APM's own 27 July** (K=-323,384), i.e. an ordinary
same-store T+1 timing lag, not a contamination symptom. **Without** the
split, APM would show a phantom Rs 19.33 L receivable and MMR a phantom
Rs 19.33 L excess, all year — exactly the kind of artefact the brief is
worried about, and exactly what the pickup_location master fix already
prevents.

### 4b. A second, already-open defect, now precisely quantified: BLA / BIA

CLAUDE.md already documents this as unresolved: *"BIA's own code AX28944
carries TWO SLIPAMOUNT rows instead of the usual one... while BLA's code
AX28945 carries no row at all that day"*, on 8 named July dates. This
diagnostic confirms it and, for the first time, puts a rupee figure on it.

BIA appears in the section-2 excess list on **exactly these 8 dates**:
02, 06, 15, 16, 17, 20, 21, 22 July — the same 8 dates CLAUDE.md already
names. On every one of those same 8 dates, BLA shows a deposit with **zero**
credit that day, and the amount matches BIA's excess **to the rupee**:

| date | BLA deposit (I) | BLA credit (J) | BIA excess (K) | match |
|---|---:|---:|---:|---|
| 2026-07-02 | -32,772.00 | 0.00 | 32,772.00 | exact |
| 2026-07-06 | -77,547.00 | 0.00 | 77,547.00 | exact |
| 2026-07-15 | -34,400.00 | 0.00 | 34,400.00 | exact |
| 2026-07-16 | -35,558.00 | 0.00 | 35,558.00 | exact |
| 2026-07-17 | -50,639.00 | 0.00 | 50,639.00 | exact |
| 2026-07-20 | -69,552.00 | 0.00 | 69,552.00 | exact |
| 2026-07-21 | -25,097.00 | 0.00 | 25,097.00 | exact |
| 2026-07-22 | -33,653.00 | 0.00 | 33,653.00 | exact |
| **Total** | **-3,59,218.00** | **0.00** | **3,59,218.00** | |

Both stores already have their own, separately-registered pickup codes
(BIA → `AX28944`, BLA → `AX28945` — confirmed by reading which codes credit
each of them anywhere in July), so this is **not** a pickup_location
master gap; the raw CMS file itself carries BLA's slip under BIA's code on
these 8 dates. **This is real money, correctly deposited by BLA, credited to
the wrong store** — Rs 3,59,218 of the "excess" is BIA's books showing
someone else's cash, and the same Rs 3,59,218 sits as a phantom receivable
on BLA's books. It nets to zero at the estate level (which is exactly why it
was invisible in the old single "net K" figure), but it is **wrong at the
store level on both sides**, right now, unfixed.

### 4c. Two further, previously undocumented instances of the same pattern

Because the BLA/BIA mechanism is "same day, exact rupee match, different
store" rather than a same-store timing lag, the adjacent-date test in
section 3 correctly reports these as "unexplained" (it only looks at the
*same* store on the *next*/*previous* day). Searching same-day, all-store,
for any negative K within Rs 100 of an unexplained excess row's K found two
more exact (Rs 0 residual) matches:

| Excess row | Matching receivable, same day | Residual |
|---|---|---:|
| JRC, 2026-07-06, K=+2,39,760.00 | JBP, K=-2,39,760.00 | Rs 0.00 |
| GRK, 2026-07-04, K=+85,790.00 | GKT, K=-85,790.00 | Rs 0.00 |

Both JBP and GKT have their own, separately-registered pickup codes (`JBP`
and `AX30494` respectively) that credit them normally on every other date —
the same shape as BLA/BIA: a correctly-registered store whose slip landed
under a different store's code on one specific day. **These were not asked
for in the brief's AX31918 question, but they use the identical diagnostic
method (an exact same-day rupee match against a different store's
receivable) and are reported here because they bear directly on how much of
the Rs 80.16 L is a genuine excess versus a misattribution.**

**Section 4 total confirmed-as-misattribution (not a genuine excess, not
simply a timing lag either): Rs 3,59,218 (BLA/BIA) + Rs 2,39,760 (JBP/JRC) +
Rs 85,790 (GKT/GRK) = Rs 6,84,768.**

---

## 5. Other candidate causes

**(a) Is any CMS row counted twice?**

- Duplicate-slip check — grouping the 4,593-row CMS frame this page reads by
  `(PICKUPPOINTCODE, SLIPNO, TYPE, DEPDATE, SLIPAMOUNT)` and looking for any
  group with more than one row: **none found.** Every slip in the frame this
  page reads is unique on that key.
- Is the warehouse's dated-only read (`_read("axis_cms")`) ever concatenated
  with the full source-file read (`ingest_axis_cms()`) — the double-read the
  module's own docstring describes wanting to avoid? Reading
  `store_sheet_source.cms_frame()` directly: it computes `dated = _read(...)`
  but only **returns** it as a fallback inside the `except` branch; the
  success path returns `everything` (the full re-ingested file) **alone**.
  `dated` is computed and discarded on every normal run — wasted work, not a
  double count. **No.**
- ZPO rows: `everything` already contains all 4,593 rows including the 106
  undated ZPO ones (confirmed: `cms.shape[0] == 4593`, ZPO rows present),
  read once. **No double-read of ZPO either.**

**(b) Are ADJ.CR. rows landing in J, and where?**

Yes — all 3 July `ADJ.CR.` rows feed J via the `ADJ` column, as designed
(`ss.CREDIT_TYPE_SOURCE["ADJ.CR."] == ("ADJ", 1)`), never via `SLIPAMOUNT`:

| store | business_date | SLIPNO | ADJ (feeds J) | SLIPAMOUNT (NOT used for J) |
|---|---|---|---:|---:|
| NDM | 2026-07-03 | 90084730 | 103.00 | 47,400.00 |
| MSR | 2026-07-27 | 45706086 | 7.00 | 1,27,722.00 |
| BAR | 2026-07-16 | 2285121 | 8.00 | 95,760.00 |

None of NDM, MSR or BAR appears in the section-2 excess list — their K on
those dates is not positive, so ADJ.CR. is not a contributor to the disputed
Rs 80.16 L. (This matches CLAUDE.md's existing figure: 3 rows, Rs 118 total
from `ADJ`, immaterial next to Rs 80.16 L.)

**(c) Credits landing on a store-date with NO deposit at all (I=0, J>0)** —
the purest form of the artefact, since there is nothing on the deposit side
to even partially explain the credit:

| date | store | I | J | K |
|---|---|---:|---:|---:|
| 2026-07-07 | RLN | 0.00 | 1,15,646.00 | 1,15,646.00 |
| 2026-07-09 | PHB | 0.00 | 52,538.00 | 52,538.00 |
| 2026-07-13 | BRP | 0.00 | 1,17,132.00 | 1,17,132.00 |
| 2026-07-13 | LMT | 0.00 | 87,100.00 | 87,100.00 |
| 2026-07-13 | LTL | 0.00 | 43,400.00 | 43,400.00 |
| 2026-07-18 | ARG | 0.00 | 56,003.00 | 56,003.00 |
| 2026-07-18 | VSW | 0.00 | 31,864.00 | 31,864.00 |
| 2026-07-23 | CNW | 0.00 | 18,920.00 | 18,920.00 |
| 2026-07-23 | PHB | 0.00 | 86,875.00 | 86,875.00 |

9 rows, Rs 6,09,478.00. Every one of these already appears in section 3
classified either "fully offset" (RLN, PHB×2, ARG, VSW, CNW — each has a
matching negative K the adjacent day) or "unexplained" (BRP-13, LMT-13,
LTL-13). None is a NEW row beyond what sections 3–4 already found; this
check confirms the same rows from a different angle (I=0 is simply the
extreme case of "the deposit came on a different day than the credit").

**(d) Is the T+0/T+1 per-store CMS setting applied when attributing a
credit?** Reading `mop_reco.store_sheet.cms_received_by_deposit_date()`
directly — its own module docstring states the rule and the code matches it:

```python
def cms_received_by_deposit_date(cms):
    ...
    deposit_date = pd.to_datetime(subset["DEPDATE"], errors="coerce")
    ...
    subset["date"] = pd.to_datetime(subset["DEPDATE"], ...).dt.strftime("%Y-%m-%d")
```

**No** — `config.yaml`'s `settlement_cycles.cms.cycle_days` / `per_store`
(the T+0/T+1 setting) is never read anywhere in this function or in
`store_sheet_source.py`. J is attributed to the CMS row's own raw `DEPDATE`
field, unconditionally, for every store — which is the documented, deliberate
design (CLAUDE.md §6.9d: "Column J belongs to the DEPOSIT date, never the
credit date"). This is **not a bug**: `DEPDATE` (the agency's own deposit
date on the slip) is already meant to be the settlement-cycle-adjusted date,
so a *further* T+0/T+1 adjustment on top of it would be double-adjusting.
What sections 3–4 show is that `DEPDATE` itself is sometimes one day off
from the store's actual PTC deposit date (or, in section 4, the wrong
store's code entirely) — a data problem in the source file's own field, not
a missed setting in this code.

---

## 6. Verdict

**The Rs 80.16 L "excess credited" figure is real in total (it is not a
calculation bug — column K is `I + J` on every row with zero row-level
error, and it is computed from the same CMS data, the same defect-A split
and the same DEPDATE rule the rest of this system already relies on), but
it is composed almost entirely of two artefacts and not of genuine
agency over-crediting:**

| Component | Rupees | % of Rs 80,16,340 |
|---|---:|---:|
| Same-store, ±1-day timing shift (section 3, fully + the explained part of the partial) | Rs 59,40,967 | 74.1% |
| Cross-store misattribution, same day, confirmed to the rupee (section 4: BLA/BIA + JBP/JRC + GKT/GRK) | Rs 6,84,768 | 8.5% |
| Unexplained by any test in this report | Rs 13,90,605 | 17.3% |

**82.7% of the disputed figure is explained as an artefact of date or store
attribution, not new money the agency invented.** The remaining 17.3%
(Rs 13.9 L, 15 store-days — see section 3's "unexplained" rows minus the
three pairs identified in section 4) was not resolved by a ±1-day,
same-store test or a same-day, all-store exact-match test; it may be a
wider (T+2/T+3) timing lag not tested here, further undocumented store-code
mixups like the two found in 4c, or genuine excess. This report does not
claim to have explained it — it is reported as open, not rounded away.

**What "cash with the agency, not yet in the bank" should read for July,**
if the two confirmed artefacts above were corrected (not a code change here,
only what the corrected arithmetic would show):

- Genuine receivable (K<0 unaffected by any finding here): **Rs 1,45,87,717**
  stays as it is — nothing in this report disputes the receivable side.
- Genuine excess, after removing the Rs 59,40,967 timing-shift artefact and
  the Rs 6,84,768 confirmed cross-store misattribution from the current
  Rs 80,16,340: **≈ Rs 13,90,605** (and even that residual is not yet proven
  real — see above).
- Net position, unchanged by any of this (the two artefact classes cancel
  within themselves and were always inside the net): **-Rs 65,71,377**, i.e.
  the estate has, on net, **Rs 65,71,377 more deposited than credited** as at
  31 July — real cash in transit, consistent with July ending on a Friday
  and CLAUDE.md's own prior note that 85 of the month's non-zero-K days are
  T+1-tail deposits with no credit at all yet.

**Recommended next step (not executed here — diagnostic only):** register
the two newly-found pairs (JBP/JRC via whatever pickup code is shared, and
GKT/GRK likewise) the same way `AX31918` was registered for APM/MMR, once
the pickup codes actually responsible for the mislabelled slips are
identified from the raw CMS export — and treat the Rs 13.9 L residual as a
genuinely open item pending a wider-window timing check.
