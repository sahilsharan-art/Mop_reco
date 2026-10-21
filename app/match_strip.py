"""Bills and transactions, split by who matched them - ONE renderer for every place that shows it (2026-10-16).

The Exceptions page shows these as tiles, the Summary page as a table; both are drawn from the SAME
`queries.MatchOverview`, so the tiles and the table can never disagree. The two-sided controls are shown
quiet when they hold and loud (--bad, naming the difference) when they do not:

    bills : open + closed by the engine + closed by a person = total
    txns  : mapped by the engine + mapped by a person + refunded + unmapped = total

and each side is also checked against the exception register (two independent sources).
"""

from __future__ import annotations

import streamlit as st

from dashboard_common import tile
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import money_cr, money_short, money_table


def _bill_notes(ov) -> list:
    """The sentences that make the bill count and the Exceptions header read as one fact."""
    notes = []
    if ov.bills_agree_with_exceptions:
        if ov.merged_bill_lines:
            notes.append(
                f"{fmt_count(ov.bills['open']['count'])} open bill lines = {fmt_count(ov.exception_bills_n)} "
                f"exception bills + {fmt_count(ov.merged_bill_lines)} line(s) that share date, store, bill and "
                "MOP with another line (one exception covers them).")
    else:
        st.error(
            f"**MISMATCH**: {fmt_count(ov.bills['open']['count'])} open bill lines but "
            f"{fmt_count(ov.exception_bills_n)} exception bills + {fmt_count(ov.merged_bill_lines)} merged lines "
            f"= {fmt_count(ov.exception_bills_n + ov.merged_bill_lines)}.")
    return notes


def _txn_notes(ov) -> list:
    notes = []
    if ov.unmapped_agrees_with_exceptions:
        if ov.merged_txn_rows:
            notes.append(
                f"{fmt_count(ov.txns.unmapped_n)} unmapped transactions = {fmt_count(ov.exception_txns_n)} "
                f"exception transactions + {fmt_count(ov.merged_txn_rows)} duplicate-reference row(s) the "
                "register folded into one exception.")
    else:
        st.error(
            f"**MISMATCH**: {fmt_count(ov.txns.unmapped_n)} unmapped transactions but "
            f"{fmt_count(ov.exception_txns_n)} exception transactions + {fmt_count(ov.merged_txn_rows)} merged "
            f"= {fmt_count(ov.exception_txns_n + ov.merged_txn_rows)}.")
    if ov.txns.not_sale_n:
        notes.append(f"{fmt_count(ov.txns.not_sale_n)} refund / chargeback / failed row(s) are not sales and are "
                     "outside this count.")
    return notes


def _controls(ov):
    b = ov.bills
    if ov.bills_ok:
        st.caption(f"✓ bills: open ({fmt_count(b['open']['count'])}) + auto closed ({fmt_count(b['auto']['count'])}) "
                   f"+ manual closed ({fmt_count(b['manual']['count'])}) = total ({fmt_count(b['total']['count'])}).")
    else:
        st.error(f"**MISMATCH** (bills): open + auto closed + manual closed differs from total by "
                 f"{fmt_count(ov.bills_difference)}.")
    t = ov.txns
    if ov.txns_ok:
        st.caption(f"✓ transactions: auto mapped ({fmt_count(t.auto_n)}) + manual mapped ({fmt_count(t.manual_n)}) + "
                   f"refunded ({fmt_count(t.refunded_n)}) + unmapped ({fmt_count(t.unmapped_n)}) = "
                   f"total ({fmt_count(t.total_n)}).")
    else:
        st.error(f"**MISMATCH** (transactions): auto + manual + refunded + unmapped differs from total by "
                 f"{fmt_count(ov.txns_difference)}.")


def render_tiles(ov, key: str = "match-ribbon"):
    """Tiles (Exceptions page). At most four to a row."""
    b, t = ov.bills, ov.txns
    with st.container(key=key):
        st.markdown("**Bills**")
        c1, c2, c3, c4 = st.columns(4)
        tile(c1, "OPEN (UNMATCHED)", fmt_count(b["open"]["count"]),
             state="attention" if b["open"]["count"] else "clean", note=money_short(b["open"]["value"]),
             tooltip=f"Rs {money_table(b['open']['value'])} - no transaction matched yet")
        tile(c2, "CLOSED - AUTO MATCHED", fmt_count(b["auto"]["count"]), state="clean",
             note=money_short(b["auto"]["value"]), tooltip=f"Rs {money_table(b['auto']['value'])} - matched by the engine")
        tile(c3, "CLOSED - MANUALLY MATCHED", fmt_count(b["manual"]["count"]), state="clean",
             note=money_short(b["manual"]["value"]),
             tooltip=f"Rs {money_table(b['manual']['value'])} - matched by a person (inline or upload)")
        tile(c4, "TOTAL BILLS", fmt_count(b["total"]["count"]), state="neutral", note=money_short(b["total"]["value"]),
             tooltip=f"Rs {money_table(b['total']['value'])}")
        for note in _bill_notes(ov):
            st.caption(note)

        st.markdown("**Transactions**")
        r1, r2, r3 = st.columns(3)
        tile(r1, "MAPPED TO BILL - AUTO", fmt_count(t.auto_n), state="clean", note=money_short(t.auto_v),
             tooltip=f"Rs {money_table(t.auto_v)} - consumed by the engine")
        tile(r2, "MAPPED TO BILL - MANUAL", fmt_count(t.manual_n), state="clean", note=money_short(t.manual_v),
             tooltip=f"Rs {money_table(t.manual_v)} - matched by a person")
        tile(r3, "REFUNDED TO CUSTOMER", fmt_count(t.refunded_n), state="neutral", note=money_short(t.refunded_v),
             tooltip=f"Rs {money_table(t.refunded_v)} - never a bill match, never revenue")
        s1, s2, _ = st.columns(3)
        tile(s1, "UNMAPPED (OPEN)", fmt_count(t.unmapped_n), state="attention" if t.unmapped_n else "clean",
             note=money_short(t.unmapped_v), tooltip=f"Rs {money_table(t.unmapped_v)} - money received, no bill")
        tile(s2, "TOTAL TRANSACTIONS", fmt_count(t.total_n), state="neutral", note=money_short(t.total_v),
             tooltip=f"Rs {money_table(t.total_v)} - sale transactions")
        for note in _txn_notes(ov):
            st.caption(note)
        _controls(ov)


def static_table_html(rows: list, columns=("", "Count", "Value (Rs Cr)")) -> str:
    """A presentation table as plain HTML (no st.dataframe: no column menu, toolbar, index or resize handles).
    `rows` = [(label, count, value)]; the LAST row is the total and is drawn bold."""
    head = "".join(f"<th>{c}</th>" for c in columns)
    body = []
    for i, (label, count, value) in enumerate(rows):
        cls = " class='mop-static-total'" if i == len(rows) - 1 else ""
        body.append(f"<tr{cls}><td>{label}</td><td>{count}</td><td>{value}</td></tr>")
    return (f"<div class='mop-static-wrap'><table class='mop-static'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def bills_rows(ov) -> list:
    b = ov.bills
    return [("Open (unmatched)", fmt_count(b["open"]["count"]), money_cr(b["open"]["value"])),
            ("Closed - auto matched", fmt_count(b["auto"]["count"]), money_cr(b["auto"]["value"])),
            ("Closed - manually matched", fmt_count(b["manual"]["count"]), money_cr(b["manual"]["value"])),
            ("Total", fmt_count(b["total"]["count"]), money_cr(b["total"]["value"]))]


def txn_rows(ov) -> list:
    t = ov.txns
    return [("Mapped to bill - auto", fmt_count(t.auto_n), money_cr(t.auto_v)),
            ("Mapped to bill - manual", fmt_count(t.manual_n), money_cr(t.manual_v)),
            ("Refunded to customer", fmt_count(t.refunded_n), money_cr(t.refunded_v)),
            ("Unmapped (open)", fmt_count(t.unmapped_n), money_cr(t.unmapped_v)),
            ("Total", fmt_count(t.total_n), money_cr(t.total_v))]


def render_table(ov):
    """The same figures as a table (Summary page): the Bills and Transactions blocks. Static HTML, not a data grid -
    nobody sorts, filters or edits these two small blocks."""
    bcol, tcol = st.columns(2)
    with bcol:
        st.markdown("**BILLS**")
        st.markdown(static_table_html(bills_rows(ov)), unsafe_allow_html=True)
        for note in _bill_notes(ov):
            st.caption(note)
    with tcol:
        st.markdown("**TRANSACTIONS**")
        st.markdown(static_table_html(txn_rows(ov)), unsafe_allow_html=True)
        for note in _txn_notes(ov):
            st.caption(note)
    _controls(ov)
