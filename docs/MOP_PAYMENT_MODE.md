# Card vs UPI — the correctness fix (2026-09-19)

Measured against the loaded period (1–31 July 2026, run #2).

## The bug

The particulars matrix's first version (2026-09-18) split UPI vs Card from
the POS **MOP name**, via `config.yaml`'s `mop_classification.*.bucket`
field. That put **Card at Rs 40,13,02,209.51 (74.1% of digital)** and
**UPI at Rs 14,03,28,016.70 (25.9%)** — not credible for Indian retail, and
directly contradicted by the unmapped-transaction pool itself (479 unmapped
UPI transactions against 30 unmapped card transactions).

**Root cause, confirmed against the data, not assumed:** `"PAYTM EDC"` is
one shared terminal that accepts BOTH card and UPI — Axis TIDs are mapped
onto Paytm machines for card acceptance. The MOP-x-payment_mode cross-tab
below shows this directly: the single MOP `PAYTM EDC` carries 17,774
Axis-card rows, 379 Paytm credit-card rows, 13,483 Paytm debit-card rows,
**and** 444,782 Paytm-UPI rows. A MOP name cannot answer "card or UPI" —
only the matched transaction's own identity can.

## The fix

Card vs UPI is now decided **per matched bill line, from the gateway
transaction's own identity** — never the POS MOP — via
`mop_matrix.classify_digital_mode(gateway, payment_mode, mop)`:

| Gateway | Rule | Why |
|---|---|---|
| **axis** | always `card` | The Axis EDC report is settled card transactions only, by design (CLAUDE.md §5) — confirmed: every `CARD_TYPE` value seen is a card scheme, never UPI. |
| **paytm** | from the transaction's own `payment_mode`: `UPI`→upi; `DEBIT_CARD`/`CREDIT_CARD`→card; `VC`/`MC`/`MD`/`RC`→card (kept as a forward-compatible allowance — see "Values found" below, none of these four appeared on a Paytm row in this data) | `payment_mode` is populated on every SUCCESS+ACQUIRING row and is exactly the settlement channel (CLAUDE.md §5). |
| **bajaj** | from the bill's own MOP: `Bajajpay Card`→card, `Bajajpay UPI`→upi | The one narrow, deliberate exception — see below. |
| anything else / no match | `unclassified` | Never guessed. |

### Why Bajaj reads the MOP, and this is not the same bug

CLAUDE.md §6.2 already documents that Bajaj's own settlement fields
(`Card Scheme`/`txn_type`) are unreliable for this split — confirmed again
here: `Card Scheme` reads `'NA'` on 536 of 573 raw rows, and is known to
read `UPI`/`SALE-UPI` even on card MOPs. But `"Bajajpay Card"` and
`"Bajajpay UPI"` are **two distinct, separately-posted POS MOPs** — not one
shared terminal like Paytm EDC. There is no ambiguity for the MOP to paper
over here: reading it is reading a fact, not inferring one. This is judged
a fundamentally different situation from the Paytm EDC bug, not a second
instance of it, and is documented as such rather than silently applied.
`config.yaml`'s bucket field for every digital MOP — including both Bajaj
ones — was changed to a single `digital` bucket regardless, so that no MOP
anywhere carries a `bucket: card`/`bucket: upi` value any more; the Bajaj
decision lives only inside `classify_digital_mode()`.

### Unmatched bills — a third column, "Unclassified"

A digital bill line with **no** matched transaction has no known payment
mode. It is never guessed into Card or UPI — it is its own column,
**Unclassified**, and its Collected is 0 by construction, so its
Receivable is the whole of that bill's amount. This is why Card and UPI
each carry a Receivable of exactly 0: every rupee in either bucket is, by
definition, a matched transaction. The whole of the digital receivable (G)
now concentrates visibly into Unclassified, rather than being invisibly
spread across Card and UPI the way the old MOP-name split did.

## 1. The new figures

**Engine-matched population only** (before any manual-match cascade —
this is the population the cross-tab below is built from):

| | Value (Rs) | % of Total digital |
|---|---:|---:|
| Card | 42,460,926.00 | 7.84% |
| UPI | 498,942,605.21 | 92.12% |
| Unclassified (200 open/ambiguous bills) | 226,695.00 | 0.04% |
| **Total digital** | **541,630,226.21** | **100.00%** |

`Card + UPI + Unclassified = 42,460,926.00 + 498,942,605.21 + 226,695.00 =
541,630,226.21` — to the rupee, the existing Total digital figure. **UPI is
the larger by a wide margin (92.1% vs 7.8%)**, as expected for Indian
retail and consistent with the unmapped-transaction pool's own 479-UPI vs
30-card split.

**As the live app shows it right now** (the same figures, plus the two
currently-active manual matches on `SB51/026938/0726` and
`SB51/026939/0726` — Rs 5.00 each, both confirmed UPI — which the app's
own cascade rule moves out of Unclassified immediately, with no re-run,
CLAUDE.md 6.9b-ii item 1): Card **Rs 4,24,60,926.00 (4.1%)**, UPI
**Rs 49,89,42,615.21 (48.4%)**, Unclassified **Rs 2,26,685.00 (0.0%)**,
summing to the same Rs 54,16,30,226.21. (The page's own "% to total" is
against Total *sale*, not Total *digital*, which is why these percentages
read lower than the table above.)

**Control asserted and shown on the page:** "UPI + Card + Unclassified =
Total digital" — holds, difference 0.00. Unclassified (Rs 2,26,685–
2,26,695) is exactly the open-bills population, not materially larger, so
there is no sign of a matched line failing to pick up its transaction's
payment mode.

## 2. Payment_mode distribution by gateway (engine-matched population)

| Gateway | Raw value | Count | Value (Rs) |
|---|---|---:|---:|
| axis | `CARD_TYPE` (always → card) | 17,854 | 24,281,319.00 |
| bajaj | `Card Scheme`/MOP (→ card or upi by MOP) | 451 | 373,888.00 |
| paytm | `CREDIT_CARD` | 379 | 504,999.00 |
| paytm | `DEBIT_CARD` | 13,548 | 17,633,883.00 |
| paytm | `UPI` | 627,718 | 498,609,442.21 |

Every Paytm `payment_mode` value present is exactly the three the column
dictionary already names (CLAUDE.md §5: `{UPI, DEBIT_CARD, CREDIT_CARD}`)
— confirmed, not assumed: no other value, and no blank, appears anywhere
in the SUCCESS+ACQUIRING matching pool (13 raw CHARGEBACK rows do carry a
blank `payment_mode`, but CHARGEBACK is not ACQUIRING and never enters the
matching pool at all — see `r1_bill_txn.successful_acquiring()`).

## 3. MOP × payment_mode cross-tab (matched lines only, count then value)

**Count:**

| MOP | AXIS-CARD | BAJAJ | CREDIT_CARD | DEBIT_CARD | UPI |
|---|---:|---:|---:|---:|---:|
| Bajajpay Card | 0 | 33 | 0 | 0 | 0 |
| Bajajpay UPI | 0 | 418 | 0 | 0 | 0 |
| EDC OTHER | 79 | 0 | 0 | 65 | 1,731 |
| EDC OTHER AXIS | 1 | 0 | 0 | 0 | 2 |
| PAYTM DQR MOREFUN | 0 | 0 | 0 | 0 | 37,311 |
| **PAYTM EDC** | **17,774** | 0 | **379** | **13,483** | **444,782** |
| PaytmDQR | 0 | 0 | 0 | 0 | 143,892 |

**Value (Rs):**

| MOP | AXIS-CARD | BAJAJ | CREDIT_CARD | DEBIT_CARD | UPI |
|---|---:|---:|---:|---:|---:|
| Bajajpay Card | 0.00 | 40,725.00 | 0.00 | 0.00 | 0.00 |
| Bajajpay UPI | 0.00 | 333,163.00 | 0.00 | 0.00 | 0.00 |
| EDC OTHER | 107,895.00 | 0.00 | 0.00 | 74,338.00 | 1,499,416.00 |
| EDC OTHER AXIS | 528.00 | 0.00 | 0.00 | 0.00 | 1,896.00 |
| PAYTM DQR MOREFUN | 0.00 | 0.00 | 0.00 | 0.00 | 30,548,983.00 |
| **PAYTM EDC** | **24,172,896.00** | 0.00 | **504,999.00** | **17,559,545.00** | **359,015,439.51** |
| PaytmDQR | 0.00 | 0.00 | 0.00 | 0.00 | 107,543,707.70 |

`PAYTM EDC` is the proof: one MOP, four different payment channels, in
material volume on every one of them.

## 4. Payment_mode values found that were not in the given list

- **Paytm:** none. Every value is one of `UPI`/`DEBIT_CARD`/`CREDIT_CARD`.
- **Axis `CARD_TYPE`** (used only for traceability — Axis is classified
  `card` unconditionally, regardless of this value): `VC` (8,856 raw rows),
  `MC` (4,299), `RC` (3,739), `VD` (470), `RD` (398), `MD` (108). **`VD`
  and `RD` were not in the given list** (`VC, MC, MD, RC`) — almost
  certainly Visa Debit and RuPay Debit, still unambiguously card, but
  reported here rather than silently assumed, per instruction.
- **Bajaj `Card Scheme`** (also not used for classification — Bajaj is
  classified from the bill's own MOP, per CLAUDE.md §6.2 — reported for
  completeness only): `NA` (536 raw rows — the majority, confirming the
  field cannot be trusted), `VISA` (19), `RUPAY` (12), `MASTER` (5),
  `DINERCLUB` (1). None of these match the given list's spelling either,
  but since this field is never consulted for the actual split, it has no
  bearing on Card vs UPI.

## Performance

`persist_digital_mode_agg()` runs once per run, in memory, over the run's
own `bill_lines` DataFrame (no extra query) — measured on the full
660,150-row month: well under a second added to the run's own timing.
`_digital_mode_sales()` (the page-load read) is one indexed `GROUP BY` over
`digital_mode_agg` plus a small manual-match adjustment (bounded by the
handful of currently-active manual matches, never the whole month) —
measured at a few milliseconds, no different in shape from the existing
`_mop_bucket_sales()` it sits beside.
