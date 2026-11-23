# MOP Reconciliation: answer to the DBA's hand-over (`mop-ckpost-handover.md`, section 5)

From: the MOP session (server 10.0.1.98, login `ckpost`, `application_name = mop_reco`). Date: 23 Nov 2026.
We discovered all 10 `gold_mop` views ourselves and tested each against data MOP already holds. From now on MOP reads **only** `gold_mop` (and `meta.v_dataset_freshness`).
Before your guidance arrived we had also read the catalogue and `silver.dim_site` once to compare store lists; nothing from `silver`, `meta.app_secrets`, `meta.hub_*` or `meta.customer_check_audit` was read, and nothing was written anywhere.

## 1. Verdict per view

| View | Verdict | Evidence |
|---|---|---|
| `pos_bill_lines` | **usable** | 20 Jul 2026: every one of 40,077 bill x mode combinations agrees with our Excel (amount to the paisa, Paytm / Axis / Bajaj reference parsed from `edc_response` identical, bill time and till operator identical). Only difference: the ERP keeps credit-note lines separate (199 + 199 where Excel had 398) - same amounts. 18 Sep 2026, empty earlier today, is now filled (50,071 lines). The full April-October run is still in progress; we will report any difference by bill. |
| `ptc_vouchers` | **usable** | April-October: 150,947 vouchers on both sides, amounts identical to the paisa, deposit total identical (4,305,732,704.10). Only difference: 11 September vouchers of store HMR show `LEDGER UNASSIGNED` in our Excel and the proper ledger in the ERP (the ERP is the better one). |
| `store_master` | **usable** | Replaces `silver.dim_site` for MOP. We will use `opening_date`, `first_bill_date`, `last_bill_date`. Needed to add the 26 new active stores to MOP's own master. |
| `calendar` | **usable for weekday / weekend / FY only** | `is_holiday` is empty (as you said). Not usable for bank holidays. |
| `tid_master` | **usable with a change: as a cross-check, not as a Bajaj source** | Of 309 terminals: the 82 `Razorpay`-type ids are 15-digit ids that look like our Axis MIDs - 56 of 82 are in our Axis MID master (54 same store, 2 different store), 26 not in ours. The 227 `PAYTM`-type ids: 155 in our Paytm MID master (151 same store, 4 different), 72 not in ours. **None of our 124 Bajaj TIDs is in `tid_master`**, so it cannot map Bajaj terminals. Please confirm what the `Razorpay` type means (are these Axis MIDs?). |
| `payment_heads` | **usable** | Every MOP on October bills and every MOP in our configuration is among the 27 heads. One MOP, `NEPALI RUPPES` (1 line, Rs 12), is typed `CSH` (cash) with settlement applicable - we had it parked as "unclassified". |
| `payment_head_store` | **not tested yet** | Possible use: flag a MOP used at a store where the head is not enabled. Not needed now. |
| `store_settlement_day` | **partly tested** | Used only through the lines view; the meaning of `status` U / C / R and `validation_state` S is still undocumented - please document them. |
| `store_settlement_lines` | **very usable (the most valuable new view)** | Its `CashSummary` lines (Opening, POS Bill, PTC Head) are the ERP's own daily store cash book. Against our computed cash book, month by month April-October, over ~5,000-6,700 store-days a month: **cash sale 100.0% exact, cash deposit 100.0% exact, other petty cash 100.0% exact.** Opening balance 88-89% exact; the rest differ by a constant per-store amount (e.g. store GPB -40,000 on every day of July; JGB +300 every day of October), i.e. a different starting balance, not a daily error. This is the independent confirmation our cash book never had. |
| `bank_postings` | **usable for day-level bank totals only** | October: Axis 8218 + 7647 credits Rs 185.66 Cr; our Paytm payouts alone are Rs 70.85 Cr (38%). No UTR, as you warned, so it cannot match payments one by one. |

## 2. Your questions

1. **Landing place for external files (4.1): YES, wanted** for the Paytm, Axis EDC, Bajaj and Axis CMS reports, the bank statement, the customer-refund workbook and a holiday list. Today these are downloaded by hand and copied to the server; a cut-off file silently changes the figures.
2. **A narrower login `mop_ro` limited to `gold_mop` and `meta.v_dataset_freshness`: YES, recommended.** MOP does not need `silver`, the other gold schemas or any of the sensitive `meta` tables, and supports revoking them from `ckpost`.
3. **More store cash detail (4.3):** the settlement-line view is already valuable. We would like `PSITE_POSSTLMDETAIL` (system amount versus counted amount per payment head) because it is the closest thing to a physical cash count; not yet asked for formally, owner's decision.
4. **Bank vouchers with UTR (gaps item 5):** `bank_postings` has no UTR, so a counts-only check of recent vouchers with a UTR-like reference would tell whether it can ever be a payment-matching source. Otherwise a bank statement file remains the only real source.
5. **Please document** the `store_settlement_day` status codes and what `Razorpay` means in `tid_master`.
6. **Please add** `gold_mop.pos_bill_lines` / `ptc_vouchers` rows with `loaded up to` to the freshness view if not already there (they are: `mop_pos_bill_lines`, `mop_ptc_vouchers` - thank you).

## 3. What MOP does next (no action needed from the DBA)

- Finish the full POS comparison, April to the latest date, and report every difference by bill.
- Use `store_settlement_lines` as the independent check of the cash book, and investigate the per-store opening offsets with Finance.
- Use `tid_master` to review our two MID masters (the 6 stores that disagree, the lower-case codes we found in our own master, the 98 terminals we lack).
- Add the 26 new active stores to MOP's store master (owner's confirmation pending).
