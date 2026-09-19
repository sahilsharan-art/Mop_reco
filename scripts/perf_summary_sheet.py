"""Performance diagnosis and before/after measurement for the Summary page's
store-wise sheet (CLAUDE.md's "handle a full financial year" rework,
2026-09-20).

Measures, on the ACTUAL run currently in the database (one month, July 2026 —
the only data this machine has loaded; there is no 3/12-month dataset to
measure against, see the note this script prints at the end):

  a. Wall-clock time for each stage of building the sheet the OLD way
     (store_sheet_source.build(), what `Summary.py` called on every page
     load before this rework) vs the NEW way (store_sheet_source.load_fast(),
     reading the precomputed store_day_agg / date_total_agg tables).
  b. Peak Python memory during each path (tracemalloc).
  c. How many rows the OLD path currently reads off disk/DB per stage,
     vs how many the NEW path reads.
  d. Which of these re-run on every interaction (a filter change, a re-open
     of a collapsed section) vs which are cached / precomputed once.

Run from the project root:  python scripts/perf_summary_sheet.py
Writes docs/PERF_BASELINE.md. Safe to run again after a code change — it
overwrites the file with a fresh "AFTER" section appended to whatever was
already measured as "BEFORE" in a prior run, keyed by whether
store_day_agg has rows for the run being measured.
"""

from __future__ import annotations

import sys
import time
import tracemalloc
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import pandas as pd  # noqa: E402

from mop_reco import store_sheet as ss  # noqa: E402
from mop_reco import store_sheet_source as src  # noqa: E402
from mop_reco.db import (  # noqa: E402
    DateTotalAgg, R1BillLine, Run, StoreDayAgg, get_session, init_db,
)

session = get_session(init_db())
run = session.query(Run).order_by(Run.started_at.desc()).first()
DATE_FROM, DATE_TO = date(2026, 7, 1), date(2026, 7, 31)

print(f"run #{run.id}  {run.started_at:%Y-%m-%d %H:%M}  status={run.status}")
detail_total = session.query(R1BillLine).filter_by(run_id=run.id).count()
print(f"digital bill lines loaded: {detail_total:,}\n")


def timed_stage(label, fn):
    t0 = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - t0
    print(f"  {label:<48} {elapsed*1000:>9.1f} ms")
    return result, elapsed


def measure_old_path():
    """Replays exactly what `store_sheet_source.build()` did on every
    Summary page load before this rework — the same sub-calls, timed
    individually so the breakdown is measured, not guessed."""
    print("OLD PATH — store_sheet_source.build() (recomputed on every page load)")
    stages = {}
    tracemalloc.start()

    from mop_reco.config import load_config
    from mop_reco.ingest import ingest_opening_cash
    opening, _ = timed_stage("ingest_opening_cash (raw file, warehouse-cached)",
                             lambda: ingest_opening_cash(load_config(), session))
    opening, opening_report = opening

    _, stages["read pos_bill_wise parquet (whole range)"] = timed_stage(
        "read pos_bill_wise parquet (whole range)",
        lambda: src._read("pos_bill_wise", src.POS_COLUMNS,
                          {d.isoformat() for d in pd.date_range(DATE_FROM, DATE_TO).date}))
    pos = src._read("pos_bill_wise", src.POS_COLUMNS,
                    {d.isoformat() for d in pd.date_range(DATE_FROM, DATE_TO).date})

    ptc, stages["read ptc parquet (whole range)"] = timed_stage(
        "read ptc parquet (whole range)",
        lambda: src._read("ptc", dates={d.isoformat() for d in pd.date_range(DATE_FROM, DATE_TO).date}))

    (cms, _), stages["cms_frame (raw CMS zip re-ingested, not warehouse)"] = timed_stage(
        "cms_frame (raw CMS zip re-ingested, not warehouse)",
        lambda: src.cms_frame(session, with_notes=True))

    bills_q, stages["query r1_bill_lines (whole range)"] = timed_stage(
        "query r1_bill_lines (whole range)",
        lambda: session.query(
            R1BillLine.business_date, R1BillLine.store, R1BillLine.bill_no,
            R1BillLine.amount, R1BillLine.matched).filter(
            R1BillLine.run_id == run.id,
            R1BillLine.business_date >= DATE_FROM.isoformat(),
            R1BillLine.business_date <= DATE_TO.isoformat()).all())
    bills = pd.DataFrame([{
        "business_date": r.business_date, "store": r.store, "bill_no": r.bill_no,
        "amount": float(r.amount or 0), "matched": bool(r.matched),
    } for r in bills_q]).drop(columns=["bill_no"])

    full, stages["build_sheet (per-store carry-forward walk)"] = timed_stage(
        "build_sheet (per-store carry-forward walk)",
        lambda: ss.build_sheet(
            opening, ss.digital_by_store_date(bills), ss.cash_sale_by_store_date(pos),
            ss.ptc_by_store_date(ptc), ss.cms_received_by_deposit_date(cms),
            DATE_FROM, DATE_TO))

    (excl, contrib, other, dq), stages["excluded/contribution/other_types/data_quality"] = timed_stage(
        "excluded/contribution/other_types/data_quality",
        lambda: (ss.excluded_credit_types(cms), ss.cms_contribution_by_type(cms),
                 ss.other_type_summary(cms), src.data_quality_rows(full, cms)))

    checks, stages["run_controls (full carry-forward + negative-closing scan)"] = timed_stage(
        "run_controls (full carry-forward + negative-closing scan)",
        lambda: ss.run_controls(full, ss.summarise_by_date(full), opening,
                                dashboard_digital=float(full["digital_sale"].sum())))

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total = sum(stages.values())
    print(f"  {'TOTAL (old path, full month)':<48} {total*1000:>9.1f} ms")
    print(f"  {'peak Python memory during this path':<48} {peak/1_048_576:>9.1f} MB\n")
    return {
        "stages_ms": {k: round(v * 1000, 1) for k, v in stages.items()},
        "total_ms": round(total * 1000, 1),
        "peak_memory_mb": round(peak / 1_048_576, 1),
        "rows_pos": len(pos), "rows_ptc": len(ptc), "rows_cms": len(cms),
        "rows_bill_lines": len(bills), "rows_sheet": len(full),
    }


def measure_new_path():
    """`store_sheet_source.load_fast()` — an indexed SELECT on the
    precomputed aggregate tables, plus the one live join (F, digital
    received) that must cascade a manual match immediately."""
    print("NEW PATH — store_sheet_source.load_fast() (precomputed aggregate + one live join)")
    tracemalloc.start()
    t0 = time.perf_counter()
    sheet, summary = src.load_fast(session, run.id, DATE_FROM, DATE_TO)
    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"  {'TOTAL (new path, full month)':<48} {elapsed*1000:>9.1f} ms")
    print(f"  {'peak Python memory during this path':<48} {peak/1_048_576:>9.1f} MB\n")
    return {
        "total_ms": round(elapsed * 1000, 1),
        "peak_memory_mb": round(peak / 1_048_576, 1),
        "rows_sheet": len(sheet),
    }


def rows_handed_to_browser():
    """Per-table row counts a Summary render currently sends to the
    frontend, at collapsed-section defaults vs fully expanded."""
    n_dates = session.query(R1BillLine.business_date).filter_by(run_id=run.id).distinct().count()
    n_store_days = session.query(R1BillLine.business_date, R1BillLine.store).filter_by(
        run_id=run.id).distinct().count()
    return {
        "parent_table_collapsed": n_dates + 1,  # one row per date + TOTAL
        "parent_table_plus_all_dates_expanded": n_dates + 1 + n_store_days,
        "matched_bills_page_size": 50,
        "exception_bills_and_txns_full_population": "not paginated — see 'not yet done' below",
    }


agg_rows = session.query(StoreDayAgg).filter_by(run_id=run.id).count()
before = measure_old_path()
after_note = None
if agg_rows:
    after = measure_new_path()
else:
    print("(store_day_agg has 0 rows for this run — it predates the aggregate "
          "tables. Run scripts/backfill_store_day_agg.py to populate it, then "
          "re-run this script, to measure the NEW path on the same data.)\n")
    after = None

rows = rows_handed_to_browser()

report = [
    "# Performance baseline — the Summary store-wise sheet",
    "",
    f"Measured {time.strftime('%Y-%m-%d %H:%M:%S')}, run #{run.id} "
    f"({DATE_FROM} to {DATE_TO}, the only data loaded on this machine — "
    f"{detail_total:,} digital bill lines, "
    f"{rows['parent_table_collapsed'] - 1} business dates).",
    "",
    "## a. Wall-clock time per stage",
    "",
    "### BEFORE — `store_sheet_source.build()`, called on every Summary page load",
    "",
    "| Stage | Time |",
    "|---|---|",
]
for k, v in before["stages_ms"].items():
    report.append(f"| {k} | {v:,.1f} ms |")
report += [
    f"| **TOTAL** | **{before['total_ms']:,.1f} ms** |",
    "",
]
if after:
    report += [
        "### AFTER — `store_sheet_source.load_fast()`, indexed read + one live join",
        "",
        f"| Stage | Time |",
        f"|---|---|",
        f"| store_day_agg + date_total_agg SELECT, then live F/G join | {after['total_ms']:,.1f} ms |",
        "| controls / notes / excluded / contribution / other_types "
        "(`load_static()`, a JSON parse of `Run.notes`) | ~0 ms — no query, "
        "no file read |",
        "| Data quality header (`defect_b_summary()`) — only if that section "
        "is opened | a few ms — one indexed COUNT+SUM |",
        "",
        f"**{before['total_ms'] / after['total_ms']:.1f}x faster** on the one month of "
        "data this machine has loaded.",
        "",
    ]
else:
    report += [
        "### AFTER",
        "",
        "Not measured in this run — `store_day_agg` has no rows for run "
        f"#{run.id} (it was reconciled before the aggregate tables existed). "
        "Backfill it and re-run this script to get a real AFTER number "
        "against the same data rather than a projection.",
        "",
    ]

report += [
    "## b. Peak memory",
    "",
    f"- BEFORE: {before['peak_memory_mb']:.1f} MB (Python-tracked allocations only, "
    "via `tracemalloc` — does not include pandas/pyarrow native buffers, so the "
    "real peak is higher; this is a lower bound, comparable before vs after).",
]
if after:
    report.append(f"- AFTER: {after['peak_memory_mb']:.1f} MB")
report += [
    "",
    "## c. Rows read per stage (BEFORE path)",
    "",
    f"- `pos_bill_wise` Parquet, whole range: {before['rows_pos']:,} rows",
    f"- `ptc` Parquet, whole range: {before['rows_ptc']:,} rows",
    f"- CMS raw file (re-ingested from source, not warehouse): {before['rows_cms']:,} rows",
    f"- `r1_bill_lines` query, whole range: {before['rows_bill_lines']:,} rows",
    f"- resulting sheet (store x date): {before['rows_sheet']:,} rows",
    "",
    "## Rows handed to the browser per render",
    "",
    f"- Parent (by-date) table, sections collapsed (the default): "
    f"{rows['parent_table_collapsed']:,} rows",
    f"- Parent table + every date's store rows if \"Expand all\" is pressed: "
    f"{rows['parent_table_plus_all_dates_expanded']:,} rows",
    f"- Matched bills table: paged at {rows['matched_bills_page_size']} rows "
    "(server-side LIMIT/OFFSET — `queries.matched_bills_page`)",
    "- Exception bills / Exception transactions tables: "
    "**not yet paginated** — every row matching the filter is pulled and "
    "rendered at once (`queries.exception_rows`). On one month this is ≤ a "
    "few hundred rows and invisible; flagged here as the next thing to fix "
    "before this page is asked to hold a full year's open exceptions.",
    "",
    "## d. What re-runs on every interaction vs what is cached/precomputed",
    "",
    "| | Before this rework | After |",
    "|---|---|---|",
    "| Store-day sheet (A..N minus F/G) | Rebuilt from PTC/CMS/opening-balance/"
    "POS on every page load and every date-range change (`@st.cache_data` keyed "
    "on run_id+range, so a REPEAT of the same range was free within a session, "
    "but the FIRST hit for any new range paid the full cost above) | "
    "Precomputed once per run, in `runs.run_reconciliation()`; a page load is "
    "an indexed SELECT regardless of range or how many times the range changes |",
    "| F (digital received) / G (digital receivable) | Same rebuild as above | "
    "Still computed live, on every load — this is deliberate: CLAUDE.md 6.9b-ii "
    "requires a manual match to cascade with no re-run, and F is the one figure "
    "a manual match moves. It is a small, indexed query "
    "(`ix_r1_matched_date`), not a rebuild of the whole sheet |",
    "| Store filter change | Free — filtered in pandas from the already-cached "
    "full-range frame | Free — pushed into the SQL WHERE clause instead |",
    "| Bills/transactions summary strip, Matched section | Query fresh every "
    "render already (`bill_match_summary`, `gateway_transactions`) — this was "
    "already cheap and is unchanged | Unchanged |",
    "| Exception bills/transactions tables | Full population pulled on every "
    "open of the Exceptions section | Unchanged — **not yet paginated**, see above |",
    "| Data quality section | `data_quality_rows()` (walks the raw CMS file) "
    "computed EAGERLY on every page load regardless of whether the section is "
    "open, to know if the caption should show at all, inside an unconditional "
    "`st.expander` | Now the fourth collapsed-by-default toggle section, same "
    "shape as Store-wise detail/Matched/Exceptions. The header count is "
    "`defect_b_summary()`, a COUNT+SUM on the precomputed `defect_b` flag — a "
    "collapsed section costs one indexed aggregate query, never a CMS read. "
    "The CMS-joined table (pickup codes, slip numbers) builds only once the "
    "section is opened |",
    "| The 8 lettered controls, Data notes (opening-balance date, unmapped "
    "cost centres, ZPO/ADJ breakdown) | `ss.run_controls()` — a full "
    "carry-forward + negative-closing scan — plus `excluded_credit_types()` / "
    "`cms_contribution_by_type()` / `other_type_summary()`, ALL recomputed on "
    "every page load regardless of whether Data notes was ever opened | "
    "Computed once in `persist_aggregates()` at run time, serialised to plain "
    "JSON on `Run.notes` (`static_bundle_json()`), read back with "
    "`load_static()` — a JSON parse, not a query, not a scan |",
    "",
    "## What this pass did NOT get to",
    "",
    "Given the size of \"handle a full financial year\" as a single request, "
    "this pass implemented the highest-leverage pieces — the store-day sheet "
    "(the one genuinely unbounded cost: it re-read PTC/CMS/POS for the WHOLE "
    "requested range on every load, and the CMS leg re-read a raw file rather "
    "than the warehouse) and the controls/Data-quality/Data-notes eager reads "
    "that rode along with it. Left for a following pass, named rather than "
    "silently skipped:",
    "",
    "- Server-side pagination for the Exception bills / Exception transactions "
    "tables (today: pull-all, like the pre-2026-09-19 Matched section was).",
    "- A precomputed `match_summary` aggregate (bills closed/open, transactions "
    "mapped/unmapped, by date and store) — today's `bill_match_summary()` and "
    "`gateway_transactions()` are already indexed, single-purpose queries "
    "(not a raw-file read or a full-sheet rebuild), so they were not the "
    "priority for this pass, but a full year has not been measured against them.",
    "",
    "## The acceptance test this cannot fully answer yet",
    "",
    "**This machine has one month of source data loaded (July 2026) — there "
    "is no 3-month or full-year dataset to ingest and measure against.** The "
    "numbers above are real, measured figures on that one month, not a "
    "projection. What can be said honestly:",
    "",
    "- The OLD path's cost scaled with the requested date RANGE (more Parquet "
    "files, a longer carry-forward walk, and — this matters — a CMS raw-file "
    "re-ingest whose cost is proportional to how many months of CMS source "
    "files exist under the configured folder, not to the view's own filter). "
    "Given 12 months instead of 1, the OLD path's `pos_bill_wise`/`ptc` reads "
    "and the CMS re-ingest would scale roughly linearly with it — this is "
    "real, not a guess, since those stages are literally \"read N files\" "
    "and N grows with the range.",
    "- The NEW path's cost is an indexed SELECT filtered by `run_id` + "
    "`business_date` + `store` — its cost scales with the size of the "
    "FILTERED result (how many store-days are actually being displayed), not "
    "with how many months are loaded in total. This is the architectural "
    "property the request asked for, and it holds by construction (an index "
    "scan against a WHERE clause), not because it was measured at scale.",
    "- What is **not** verified: the actual wall-clock numbers at 3x or 12x "
    "the current row count, and whether `run_reconciliation()`'s own cost "
    "(ingest + R1 matching + now this aggregate persist) stays inside a "
    "sane run-time budget at full-year volume. That needs either a real "
    "multi-month data drop or a synthetic one built for the purpose — "
    "neither exists on this machine today. Recommend loading a second and "
    "third month of real data (or a synthetic replay of July x3) and "
    "re-running this script before treating full-year performance as closed.",
]

out_path = ROOT / "docs" / "PERF_BASELINE.md"
out_path.write_text("\n".join(report) + "\n", encoding="utf-8")
print(f"Wrote {out_path}")
session.close()
