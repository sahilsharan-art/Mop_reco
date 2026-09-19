# MOP inventory and classification — Summary particulars matrix

**SUPERSEDED, 2026-09-19 — the Card vs UPI split documented below (proposed
class CARD/UPI per MOP, and the "Ruling received" section) was WRONG and is
no longer how the matrix works. See `docs/MOP_PAYMENT_MODE.md` for the
correctness fix and the current figures.** The MOP-count/row/value
inventory itself (11 distinct MOPs, their row counts and totals) is still
accurate and kept below for the record. What changed: "PAYTM EDC" is one
shared terminal that accepts BOTH card and UPI (Axis TIDs are mapped onto
Paytm machines for card acceptance), so a MOP-name-based bucket can never
answer "card or UPI" correctly — it put 74% of digital sale under "Card",
not credible for Indian retail and contradicted by the unmapped-
transaction pool itself (479 UPI vs 30 card). Every digital MOP below now
carries `bucket: digital` in `config.yaml` (no card/upi split at that
layer at all, Bajaj's two MOPs excepted — see MOP_PAYMENT_MODE.md for why
Bajaj is different); the real split is decided per bill line from the
matched TRANSACTION's own identity.

Measured 2026-09-18 against the loaded period (1–31 July 2026, run #2),
from the warehouse `pos_bill_wise` Parquet (the same monthly-export data
every other figure on Summary already reads — §6.13: this file carries
zero no-`BILLNO` rows, so every row below is already in the sale universe;
no filtering was applied beyond that).

**Every distinct MOP value found — 11 total, none unclassifiable by name,
but one pair (see OTHER below) cannot be assigned Card vs UPI with
confidence and is not guessed.**

| MOP (verbatim from source) | Rows | Total (Rs) | Proposed class |
|---|---:|---:|---|
| `PAYTM EDC` | 476,428 | 401,261,484.51 | CARD |
| `Cash` | 726,060 | 489,377,871.79 | CASH |
| `PaytmDQR` | 143,894 | 107,548,191.70 | UPI |
| `PAYTM DQR MOREFUN` | 37,311 | 30,548,983.00 | UPI |
| `Credit Note Received` | 51,671 | 26,854,512.00 | CREDIT NOTE RECEIVED |
| `Credit Note Issued` | 52,245 | -26,859,283.00 | CREDIT NOTE ISSUED |
| `EDC OTHER` | 2,061 | 1,894,701.00 | **OTHER — see below** |
| `Bajajpay UPI` | 418 | 333,163.00 | UPI |
| `Gift Voucher` | 15 | 42,500.00 | GIFT VOUCHER REDEEMED |
| `Bajajpay Card` | 33 | 40,725.00 | CARD |
| `EDC OTHER AXIS` | 5 | 2,978.00 | **OTHER — see below** |

Row counts and totals are exact sums over the full loaded month, not a
sample. Every row's `in_sale_universe` flag is `True` — there is nothing
excluded at this step.

## The exact Gift Voucher string

**`Gift Voucher`** — not "Gift Voucher Redeemed". That is the only voucher
MOP present in the data; there is no separate "sold" MOP, confirming the
brief's own description that this is a settlement method, never an inflow.
15 rows, Rs 42,500.00 total, confirmed from the data — not assumed from the
config file's own (differently-worded but same) entry.

## Natural signs — confirmed, not assumed

- **Credit Note Issued is already negative in the source**: -26,859,283.00
  over 52,245 rows. No sign-flip is needed — the brief's own instruction
  ("ADD it, do not subtract it again") is followed literally, adding this
  already-negative figure once.
- **Credit Note Received is already positive**: +26,854,512.00 over 51,671
  rows. No sign-flip needed.

## Ruling received — EDC OTHER + EDC OTHER AXIS are UPI

**The operator's ruling: both are UPI-mode transactions.** Folded into the
UPI column, not left as an unclassified OTHER bucket. Final UPI total for
the period: PaytmDQR + PAYTM DQR MOREFUN + Bajajpay UPI + EDC OTHER +
EDC OTHER AXIS = Rs 14,03,28,016.70. Final Card total is unchanged at
Rs 40,13,02,209.51. Card + UPI = Rs 54,16,30,226.21, exactly matching the
existing Digital sale (D) figure — nothing shrinks, nothing is guessed
beyond what was explicitly ruled.

## Why this needed a ruling in the first place (kept for the record)

**This is the one case this pass cannot classify confidently, per the
brief's own instruction not to fold an unclassifiable MOP into an existing
bucket.** Both MOPs are Tier-2-matched today (§6.3) specifically *because*
the POS payload carried no usable reference at all — the "OTHER" in the
name is the system's own admission that it could not tell which gateway,
let alone which payment rail (card vs UPI), a transaction rode on. Nothing
in the MOP string, the data, or this codebase's existing classification
says definitively "these are card" or "these are UPI" — `mop_classification`
in `config.yaml` already keeps them in their own class, `digital_noref`,
separate from plain `digital`, for exactly this reason.

**This bucket is currently part of Digital sale (D)** — `r1_bill_lines`
carries these 2,066 rows today (confirmed by direct query), and D's
existing Rs 54,16,30,226.21 total is exactly Card (Rs 40,13,02,209.51) +
UPI (Rs 13,84,30,337.70) + this OTHER bucket (Rs 18,97,679.00). Folding
OTHER into neither Card nor UPI, and leaving it out of the matrix
entirely, would make "Total digital" (and therefore "Total sale") Rs
18,97,679.00 SHORT of what Digital sale has always reported for this same
period — a silent shrinkage of a headline total that rule 4 ("nothing
vanishes silently") forbids just as much as rule 5 forbids guessing which
column to put it in.

**A ruling is needed before Total sale can be built**: fold OTHER into
Card, fold it into UPI, add it as its own (unnamed-in-the-brief) column
that still counts toward Total digital, or accept that Total sale will
read Rs 18,97,679.00 lower than every previously-reported figure for this
period, with that gap reported as EDC-OTHER-excluded rather than
unexplained. See the question posed alongside this report.
