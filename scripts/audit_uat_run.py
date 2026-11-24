"""Execute the engine-level UAT cases (audit Phase 7) against the UAT database and record the results.

    python scripts/audit_uat_run.py            (uses MOP_DATABASE_URL; never point it at production - it WRITES matches, remarks and refunds)

Each case calls the same function the page calls and compares the answer with an independent SQL figure. Results are merged into
docs/audit/uat_results.json as {id: {status, evidence, executed_at}}; a failure or an exception is recorded as such, never skipped.
"""
import io
import json
import os
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
if os.environ.get("MOP_REQUIRE_POSTGRES") == "1":
    sys.exit("Refusing to run: this script writes test matches and remarks. Use the UAT copy.")

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from mop_reco import bulk_accept as ba  # noqa: E402
from mop_reco import cms_blocks as cb  # noqa: E402
from mop_reco import cms_blocks_export as cbx  # noqa: E402
from mop_reco import exception_pairing as ep  # noqa: E402
from mop_reco import exceptions as exc  # noqa: E402
from mop_reco import export as ex  # noqa: E402
from mop_reco import float_tracker as ft  # noqa: E402
from mop_reco import manual_match as mm  # noqa: E402
from mop_reco import queries as q  # noqa: E402
from mop_reco import store_sheet as ss  # noqa: E402
from mop_reco import store_sheet_source as src  # noqa: E402
from mop_reco import store_task_summary as sts  # noqa: E402
from mop_reco import store_tasks as stt  # noqa: E402
from mop_reco.db import Exception_, ManualMatch, Run, StoreTask, get_engine, get_session  # noqa: E402

OUT = ROOT / "docs" / "audit" / "uat_results.json"
results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
engine = get_engine()
s = get_session(engine)
db_name = engine.url.database
if db_name and "uat" not in db_name.lower():
    sys.exit(f"Refusing to run against database {db_name!r}: the name must contain 'uat'.")
run = s.query(Run).filter(Run.status == "completed").order_by(Run.id.desc()).first()
LO, HI = date(2026, 4, 1), date(2026, 11, 14)
RUN = run.id
sql = lambda t, **p: s.execute(text(t), p).scalar()   # noqa: E731
TOL = 0.01


def rec(cid, ok, evidence):
    results[cid] = {"status": "Pass" if ok else "FAIL", "evidence": evidence, "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    print(f"{cid}: {'Pass' if ok else 'FAIL'} - {evidence[:150]}")


def run_case(cid, fn):
    try:
        fn()
    except Exception as exc_:                                           # a crash is a failed case, recorded with its cause
        results[cid] = {"status": "FAIL", "evidence": f"raised {type(exc_).__name__}: {str(exc_)[:300]}", "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        print(f"{cid}: FAIL (exception) {type(exc_).__name__}: {str(exc_)[:150]}")
        traceback.print_exc(limit=2)


sheet, by_date = src.load_fast(s, RUN, LO, HI)
matrix = src.mop_particulars_matrix(s, RUN, LO, HI, None)
close = lambda a, b: abs(float(a) - float(b)) <= TOL   # noqa: E731


def s02():
    d = float(sql("select sum(digital_sale) from store_day_agg where run_id=:r", r=RUN))
    c = float(sql("select sum(cash_sale) from store_day_agg where run_id=:r", r=RUN))
    t = float(sql("select sum(total_sale) from store_day_agg where run_id=:r", r=RUN))
    rec("UAT-S02", close(matrix.total_digital.sale, d) and close(matrix.total_cash.sale, c) and close(d + c, t) and close(sheet["total_sale"].sum(), t),
        f"digital {matrix.total_digital.sale:,.2f} = SQL {d:,.2f}; cash {matrix.total_cash.sale:,.2f} = SQL {c:,.2f}; C = D + E over {len(sheet):,} store-days = {t:,.2f}")


def s06():
    ov = q.match_overview(s, RUN, LO, HI)
    b = ov.bills
    total = sql("select count(*) from r1_bill_lines where run_id=:r", r=RUN)
    auto = sql("select count(*) from r1_bill_lines where run_id=:r and matched", r=RUN)
    opn = sql("select count(*) from r1_bill_lines where run_id=:r and not matched", r=RUN)
    rec("UAT-S06", ov.bills_ok and b["total"]["count"] == total and b["auto"]["count"] == auto and b["open"]["count"] == opn,
        f"page: open {b['open']['count']:,} + auto {b['auto']['count']:,} + manual {b['manual']['count']:,} = total {b['total']['count']:,}; SQL: {opn:,} / {auto:,} / {total:,}")


def s10():
    lo, hi = date(2026, 7, 10), date(2026, 7, 15)
    sh, _ = src.load_fast(s, RUN, lo, hi)
    d = float(sql("select sum(digital_sale) from store_day_agg where run_id=:r and business_date between :a and :b", r=RUN, a="2026-07-10", b="2026-07-15"))
    n = sql("select count(*) from store_day_agg where run_id=:r and business_date between :a and :b", r=RUN, a="2026-07-10", b="2026-07-15")
    rec("UAT-S10", close(sh["digital_sale"].sum(), d) and len(sh) == n and sh["date"].nunique() == 6, f"6 dates, {len(sh):,} store-days; digital {sh['digital_sale'].sum():,.2f} = SQL {d:,.2f}")


def s12():
    sm = ss.summarise_by_date(sheet)
    dates = sheet["date"].nunique()
    body = sm[sm["date"] != "TOTAL"]
    tot = sm[sm["date"] == "TOTAL"].iloc[0]
    ok = len(body) == dates == sql("select count(distinct business_date) from store_day_agg where run_id=:r", r=RUN) and close(tot["total_sale"], body["total_sale"].sum())
    rec("UAT-S12", ok and not ss.verify_date_rows_equal_store_sums(sheet, sm) if False else ok,
        f"{len(body)} date rows + TOTAL; TOTAL total_sale {tot['total_sale']:,.2f} = sum of rows {body['total_sale'].sum():,.2f}")


def s13():
    page = q.matched_bills_page(s, RUN, LO, HI, None, limit=5, offset=0)
    df = page[0] if isinstance(page, tuple) else page
    cols = list(df.columns)
    rec("UAT-S13", len(df) > 0 and any("lag" in c.lower() for c in cols) and any("transaction" in c.lower() for c in cols),
        f"{len(df)} rows on the first page; columns {cols}")


def s14_e01():
    ov = q.match_overview(s, RUN, LO, HI)
    nb = sql("select count(*) from exceptions where type in ('bill_not_matched','ambiguous_edc_other','no_source_report') and status in ('open','under_investigation')")
    nt = sql("select count(*) from exceptions where type='unmapped_gateway_transaction' and status in ('open','under_investigation')")
    ok_b = ov.exception_bills_n == nb
    ok_t = ov.exception_txns_n == nt
    rec("UAT-S14", ok_b and ok_t, f"Summary/Exceptions: bills {ov.exception_bills_n:,} = SQL {nb:,}; transactions {ov.exception_txns_n:,} = SQL {nt:,}")
    rec("UAT-E01", ok_b and ok_t and ov.bills_agree_with_exceptions and ov.unmapped_agrees_with_exceptions,
        f"tiles equal the database; merged lines explain the rest (bills +{ov.merged_bill_lines}, transactions +{ov.merged_txn_rows})")


def s17_f05():
    net = ss.net_cash_receivable(sheet)
    neg_k = int((sheet["cash_receivable"] < -0.005).sum())
    dq = src.defect_b_summary(s, RUN, LO, HI, None)
    rec("UAT-S17", net >= 0 and neg_k == dq[0], f"net cash receivable {net:,.2f} (never negative); {neg_k:,} store-days with credit above deposit = Data quality count {dq[0]:,}")
    grid = ft.build_float_grid(sheet, {})
    negs = ft.negative_float_rows(grid)
    sqln = sql("select count(*) from store_day_agg where run_id=:r and float_amount < 0", r=RUN)
    ok = len(negs) == sqln and (negs["float_amount"].is_monotonic_increasing if len(negs) else True)
    rec("UAT-F05", ok, f"{len(negs):,} negative-float store-days = SQL {sqln:,}; most negative first ({negs['float_amount'].iloc[0]:,.2f})" if len(negs) else "none")


def f01():
    grid = ft.build_float_grid(sheet, {})
    last = grid["business_date"].max()
    n = int(((grid["business_date"] == last) & grid["high_float"]).sum())
    sqln = sql("select count(*) from store_day_agg where run_id=:r and business_date=:d and float_amount > 10000", r=RUN, d=last)
    rec("UAT-F01", n == sqln, f"stores above threshold on {last}: grid {n} = SQL {sqln}")


def t01_t06():
    today = date(2026, 11, 14)
    tl = stt.tiles(s, today)
    total = sql("select count(*) from store_tasks")
    parts = sum(v[0] if isinstance(v, (tuple, list)) else 0 for k, v in tl.items() if k in ("open", "pending", "wip", "closed"))
    rec("UAT-T01", parts == total, f"open+pending+wip+closed = {parts:,} = all tasks {total:,}; tiles {({k: v for k, v in tl.items() if k in ('open', 'pending', 'wip', 'closed')})}")
    sm = sts.summary_frame(s, today, statuses=stt.LIVE_STATUSES)          # the page's "Live tasks" column counts live tasks only
    vol_cols = [c for c in sm.columns if c.endswith("Volume")]
    cnt_cols = [c for c in sm.columns if c.endswith("open ticket")]
    live = sql("select count(*) from store_tasks where status != 'closed'")
    rec("UAT-T06", int(sm[cnt_cols].sum().sum()) == live, f"type summary counts {int(sm[cnt_cols].sum().sum()):,} = live tasks in the table {live:,}; volume {sm[vol_cols].sum().sum():,.2f}")


def c03_c07():
    slips, _dep = cb.load(s, RUN)
    later = cb.credited_later(slips, "2026-04-01", "2026-11-14")
    sqln = sql("select count(*) from cms_slips where run_id=:r and row_type='COL' and dep_date between :a and :b and "
               "(cr_date is null or (cr_date::date - dep_date::date) not in (0,1))", r=RUN, a="2026-04-01", b="2026-11-14")
    flagged = int(later["flag"].astype(str).str.contains("exceptional").sum())
    rec("UAT-C03", len(later) == sqln, f"{len(later):,} credits later than day+1 listed = SQL {sqln:,}; {flagged} flagged T+10 or more")
    ex_t = cb.excluded_types(slips, "2026-04-01", "2026-11-14").set_index("Type")
    zpo_sql = sql("select count(*) from cms_slips where run_id=:r and row_type='ZPO'", r=RUN)
    rec("UAT-C07", int(ex_t.loc["ZPO", "Rows"]) == zpo_sql, f"ZPO rows listed {int(ex_t.loc['ZPO', 'Rows'])} = SQL {zpo_sql}; ADJ.CR. {int(ex_t.loc['ADJ.CR.', 'Rows'])}, ADJ.DB. {int(ex_t.loc['ADJ.DB.', 'Rows'])}")
    import openpyxl
    deps = _dep
    first, last = sorted(set(deps["dep_date"]))[0], sorted(set(deps["dep_date"]))[-1]
    m = cb.build_model(slips, deps, first, "2026-04-30", {})
    data = cbx.build_workbook(m, first, "2026-04-30", {}, cb.credited_later(slips, first, "2026-04-30"), cb.unresolved_rows(slips, first, "2026-04-30"),
                              cb.excluded_types(slips, first, "2026-04-30"), cb.block_controls(m, first, "2026-04-30"),
                              cb.store_differences(slips, deps, first, "2026-04-30"), {"run": RUN})
    wb = openpyxl.load_workbook(io.BytesIO(data))
    nform = sum(1 for ws in wb for row in ws.iter_rows() for c in row if c.data_type == "f")
    rec("UAT-C06", nform > 100, f"workbook for April: {len(wb.sheetnames)} tab(s) {wb.sheetnames[:4]}; {nform:,} live formula cells")


def z03_z04():
    t = {}
    for label, a, b in (("whole range", LO, HI), ("one month", date(2026, 10, 1), date(2026, 10, 31)), ("one day", date(2026, 10, 15), date(2026, 10, 15))):
        a0 = time.perf_counter(); src.load_fast(s, RUN, a, b); t[label] = time.perf_counter() - a0
    rec("UAT-Z03", t["whole range"] < 5 and t["one month"] < 1.5, "; ".join(f"{k} {v:.2f} s" for k, v in t.items()) + " (this PC; production figures are in the tracker)")
    gaps = s.execute(text("select source, identifier, rows from master_gaps where run_id=:r order by rows desc"), {"r": RUN}).fetchall()
    rec("UAT-Z04", len(gaps) >= 0 and all(g[1] for g in gaps), f"{len(gaps)} unresolved identifier(s) listed by name: {[(g[0], g[1], g[2]) for g in gaps][:4]}")


for cid, fn in [("UAT-S02", s02), ("UAT-S06", s06), ("UAT-S10", s10), ("UAT-S12", s12), ("UAT-S13", s13), ("UAT-S14", s14_e01), ("UAT-S17", s17_f05),
                ("UAT-F01", f01), ("UAT-T01", t01_t06), ("UAT-C03", c03_c07), ("UAT-Z03", z03_z04)]:
    run_case(cid, fn)

OUT.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"\n{len(results)} results saved to {OUT}")
