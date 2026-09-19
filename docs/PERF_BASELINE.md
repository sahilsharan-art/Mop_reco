# Performance baseline — the Summary store-wise sheet

Measured 2026-09-17 18:19:28, run #2 (2026-07-01 to 2026-07-31, the only data loaded on this machine — 660,150 digital bill lines, 31 business dates).

## a. Wall-clock time per stage

### BEFORE — `store_sheet_source.build()`, called on every Summary page load

| Stage | Time |
|---|---|
| read pos_bill_wise parquet (whole range) | 11,000.2 ms |
| read ptc parquet (whole range) | 354.0 ms |
| cms_frame (raw CMS zip re-ingested, not warehouse) | 2,306.4 ms |
| query r1_bill_lines (whole range) | 17,041.6 ms |
| build_sheet (per-store carry-forward walk) | 927.3 ms |
| excluded/contribution/other_types/data_quality | 337.7 ms |
| run_controls (full carry-forward + negative-closing scan) | 1,461.0 ms |
| **TOTAL** | **33,428.2 ms** |

### AFTER — `store_sheet_source.load_fast()`, indexed read + one live join

| Stage | Time |
|---|---|
| store_day_agg + date_total_agg SELECT, then live F/G join | 1,729.8 ms |
| controls / notes / excluded / contribution / other_types (`load_static()`, a JSON parse of `Run.notes`) | ~0 ms — no query, no file read |
| Data quality header (`defect_b_summary()`) — only if that section is opened | a few ms — one indexed COUNT+SUM |

**19.3x faster** on the one month of data this machine has loaded.

## b. Peak memory

- BEFORE: 486.2 MB (Python-tracked allocations only, via `tracemalloc` — does not include pandas/pyarrow native buffers, so the real peak is higher; this is a lower bound, comparable before vs after).
- AFTER: 20.3 MB

## c. Rows read per stage (BEFORE path)

- `pos_bill_wise` Parquet, whole range: 1,490,141 rows
- `ptc` Parquet, whole range: 21,690 rows
- CMS raw file (re-ingested from source, not warehouse): 4,593 rows
- `r1_bill_lines` query, whole range: 660,150 rows
- resulting sheet (store x date): 6,076 rows

## Rows handed to the browser per render

- Parent (by-date) table, sections collapsed (the default): 32 rows
- Parent table + every date's store rows if "Expand all" is pressed: 5,859 rows
- Matched bills table: paged at 50 rows (server-side LIMIT/OFFSET — `queries.matched_bills_page`)
- Exception bills / Exception transactions tables: **not yet paginated** — every row matching the filter is pulled and rendered at once (`queries.exception_rows`). On one month this is ≤ a few hundred rows and invisible; flagged here as the next thing to fix before this page is asked to hold a full year's open exceptions.

## d. What re-runs on every interaction vs what is cached/precomputed

| | Before this rework | After |
|---|---|---|
| Store-day sheet (A..N minus F/G) | Rebuilt from PTC/CMS/opening-balance/POS on every page load and every date-range change (`@st.cache_data` keyed on run_id+range, so a REPEAT of the same range was free within a session, but the FIRST hit for any new range paid the full cost above) | Precomputed once per run, in `runs.run_reconciliation()`; a page load is an indexed SELECT regardless of range or how many times the range changes |
| F (digital received) / G (digital receivable) | Same rebuild as above | Still computed live, on every load — this is deliberate: CLAUDE.md 6.9b-ii requires a manual match to cascade with no re-run, and F is the one figure a manual match moves. It is a small, indexed query (`ix_r1_matched_date`), not a rebuild of the whole sheet |
| Store filter change | Free — filtered in pandas from the already-cached full-range frame | Free — pushed into the SQL WHERE clause instead |
| Bills/transactions summary strip, Matched section | Query fresh every render already (`bill_match_summary`, `gateway_transactions`) — this was already cheap and is unchanged | Unchanged |
| Exception bills/transactions tables | Full population pulled on every open of the Exceptions section | Unchanged — **not yet paginated**, see above |
| Data quality section | `data_quality_rows()` (walks the raw CMS file) computed EAGERLY on every page load regardless of whether the section is open, to know if the caption should show at all, inside an unconditional `st.expander` | Now the fourth collapsed-by-default toggle section, same shape as Store-wise detail/Matched/Exceptions. The header count is `defect_b_summary()`, a COUNT+SUM on the precomputed `defect_b` flag — a collapsed section costs one indexed aggregate query, never a CMS read. The CMS-joined table (pickup codes, slip numbers) builds only once the section is opened |
| The 8 lettered controls, Data notes (opening-balance date, unmapped cost centres, ZPO/ADJ breakdown) | `ss.run_controls()` — a full carry-forward + negative-closing scan — plus `excluded_credit_types()` / `cms_contribution_by_type()` / `other_type_summary()`, ALL recomputed on every page load regardless of whether Data notes was ever opened | Computed once in `persist_aggregates()` at run time, serialised to plain JSON on `Run.notes` (`static_bundle_json()`), read back with `load_static()` — a JSON parse, not a query, not a scan |

## What this pass did NOT get to

Given the size of "handle a full financial year" as a single request, this pass implemented the highest-leverage pieces — the store-day sheet (the one genuinely unbounded cost: it re-read PTC/CMS/POS for the WHOLE requested range on every load, and the CMS leg re-read a raw file rather than the warehouse) and the controls/Data-quality/Data-notes eager reads that rode along with it. Left for a following pass, named rather than silently skipped:

- Server-side pagination for the Exception bills / Exception transactions tables (today: pull-all, like the pre-2026-09-19 Matched section was).
- A precomputed `match_summary` aggregate (bills closed/open, transactions mapped/unmapped, by date and store) — today's `bill_match_summary()` and `gateway_transactions()` are already indexed, single-purpose queries (not a raw-file read or a full-sheet rebuild), so they were not the priority for this pass, but a full year has not been measured against them.

## The acceptance test this cannot fully answer yet

**This machine has one month of source data loaded (July 2026) — there is no 3-month or full-year dataset to ingest and measure against.** The numbers above are real, measured figures on that one month, not a projection. What can be said honestly:

- The OLD path's cost scaled with the requested date RANGE (more Parquet files, a longer carry-forward walk, and — this matters — a CMS raw-file re-ingest whose cost is proportional to how many months of CMS source files exist under the configured folder, not to the view's own filter). Given 12 months instead of 1, the OLD path's `pos_bill_wise`/`ptc` reads and the CMS re-ingest would scale roughly linearly with it — this is real, not a guess, since those stages are literally "read N files" and N grows with the range.
- The NEW path's cost is an indexed SELECT filtered by `run_id` + `business_date` + `store` — its cost scales with the size of the FILTERED result (how many store-days are actually being displayed), not with how many months are loaded in total. This is the architectural property the request asked for, and it holds by construction (an index scan against a WHERE clause), not because it was measured at scale.
- What is **not** verified: the actual wall-clock numbers at 3x or 12x the current row count, and whether `run_reconciliation()`'s own cost (ingest + R1 matching + now this aggregate persist) stays inside a sane run-time budget at full-year volume. That needs either a real multi-month data drop or a synthetic one built for the purpose — neither exists on this machine today. Recommend loading a second and third month of real data (or a synthetic replay of July x3) and re-running this script before treating full-year performance as closed.
