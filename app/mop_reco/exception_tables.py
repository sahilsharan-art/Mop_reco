"""The Exceptions tables - ONE definition of their columns, shared by the screen and the export.

The screen and the export used to build their frames separately: the screen gained the candidate
transaction columns, the export did not, and the export then selected the screen's column list
against a frame that did not have them (a raw KeyError at the write step). Everything that
decides what a row of these tables contains now lives here:

  * the declared column schema - data columns, candidate columns, remark columns, and the two
    UI-only affordances (More, Match) that are never data;
  * the frame builders (`bill_frame`, `txn_frame`, `add_pairing`) the page AND the export call;
  * `select()`, which fails with a message naming a declared column that is missing instead of a
    KeyError, and `export_view()`, which shapes a frame for a file (dates DD/MM/YYYY, no UI-only
    columns, ids as text - `export.build_export` stringifies them).

No Streamlit imports (CLAUDE.md section 8).
"""

from __future__ import annotations

import pandas as pd

from . import exception_pairing as ep
from . import queries as q
from .formatting import blank, display_date

# --- the declared schema ----------------------------------------------------------------

BILL_DATA_COLUMNS = ["date", "time", "store", "bill_no", "mop", "amount", "created_by", "status"]
BILL_PAIR_COLUMNS = ["Txn 1", "Amount 1", "Txn 2", "Amount 2"]       # real data: the candidates found
BILL_UI_ONLY = ["More", "Match"]                                     # affordances, never exported
REMARK_COLUMNS = ["remark", "remark_by", "remark_at"]

TXN_COLUMNS = ["gateway", "transaction_id", "date", "time", "store", "amount", "payment_mode", "status"] \
    + REMARK_COLUMNS

# An open bill is shown with its candidates; a closed / auto-resolved bill has none to find.
BILL_BASE_COLUMNS = BILL_DATA_COLUMNS + REMARK_COLUMNS
BILL_SCREEN_COLUMNS = BILL_DATA_COLUMNS + BILL_PAIR_COLUMNS + BILL_UI_ONLY + REMARK_COLUMNS
BILL_EXPORT_COLUMNS = [c for c in BILL_SCREEN_COLUMNS if c not in BILL_UI_ONLY]

KEY = "exception_key"


class ExportSchemaError(ValueError):
    """A declared export column is missing from the frame - named, never a raw KeyError."""


def select(frame: pd.DataFrame, columns: list, what: str) -> pd.DataFrame:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ExportSchemaError(
            f"{what}: declared column(s) {missing} are not in the data this export built "
            f"(it has {list(frame.columns)}). Nothing was written.")
    return frame[columns]


def bill_columns(pairing: bool, screen: bool) -> list:
    """The one place that says which columns a bill table has. `screen=True` includes the UI-only
    More/Match; an export never does."""
    if not pairing:
        return list(BILL_BASE_COLUMNS)
    return list(BILL_SCREEN_COLUMNS if screen else BILL_EXPORT_COLUMNS)


# --- frame builders ----------------------------------------------------------------------

def with_remarks(session, df: pd.DataFrame, key_col: str = KEY) -> pd.DataFrame:
    if df.empty:
        return df
    remarks = q.latest_remarks_full(session, df[key_col].tolist())
    df = df.copy()
    none = ("", "", None)
    df["remark"] = df[key_col].map(lambda k: remarks.get(k, none)[0])
    df["remark_by"] = df[key_col].map(lambda k: remarks.get(k, none)[1] or "")
    df["remark_at"] = df[key_col].map(
        lambda k: remarks.get(k, none)[2].strftime("%d/%m/%Y %H:%M") if remarks.get(k, none)[2] else "")
    return df


def bill_frame(session, run_id: int, page_df: pd.DataFrame) -> pd.DataFrame:
    """One page (or export chunk) of bill exceptions, shaped: remarks and the bill's time and till
    operator looked up for THESE rows only."""
    if page_df.empty:
        return pd.DataFrame(columns=BILL_BASE_COLUMNS + [KEY])
    df = with_remarks(session, page_df)
    df["bill_no"] = df["detail"].map(lambda d: d.get("bill_no") or "—")
    df["mop"] = df["detail"].map(lambda d: d.get("mop") or "—")
    facts = q.unmatched_bill_facts(session, run_id, bill_nos=df["bill_no"].tolist())
    hit = df.apply(lambda r: facts.get((str(r["date"]), str(r["store"]), str(r["bill_no"]), str(r["mop"]))), axis=1)
    df["time"] = hit.map(lambda f: blank(f[0]) if f else "")
    df["created_by"] = hit.map(lambda f: blank(f[1]) if f else "")
    return df[BILL_BASE_COLUMNS + [KEY]].reset_index(drop=True)


def txn_frame(session, page_df: pd.DataFrame) -> pd.DataFrame:
    if page_df.empty:
        return pd.DataFrame(columns=TXN_COLUMNS + [KEY])
    df = with_remarks(session, page_df)
    df["gateway"] = df["detail"].map(lambda d: d.get("gateway") or "—")
    df["transaction_id"] = df["detail"].map(lambda d: d.get("transaction_id") or d.get("bill_no") or "—")
    df["payment_mode"] = df["detail"].map(lambda d: d.get("payment_mode") or "—")
    df["time"] = df["transaction_time"].map(blank)
    return df[TXN_COLUMNS + [KEY]].reset_index(drop=True)


def add_pairing(frame: pd.DataFrame, assignment: "ep.Assignment") -> tuple:
    """Add Txn 1 / Amount 1 / Txn 2 / Amount 2 (data) and More / Match (UI-only) to a `bill_frame`, from
    the GLOBAL assignment (`exception_pairing.assign`) computed over the whole open population - so a
    transaction can appear against at most one bill whatever page or filter is showing. Txn 2 is filled
    only for a genuine split (the two amounts sum to the bill). An ambiguous cluster is left blank and
    flagged in `More`. Amounts are numbers; a screen formats them, a file keeps them."""
    out = frame.copy()
    cands = assignment.candidates
    for n in (1, 2):
        out[f"Txn {n}"] = [cands[k][n - 1].txn_id if len(cands.get(k) or []) >= n else "" for k in out[KEY]]
        out[f"Amount {n}"] = [cands[k][n - 1].amount if len(cands.get(k) or []) >= n else None for k in out[KEY]]
    out["More"] = [assignment.flag(k) or ("split payment" if assignment.kind.get(k) == "split" else "")
                   for k in out[KEY]]
    out["Match"] = ""
    return out, cands


def export_view(frame: pd.DataFrame, columns: list, what: str) -> pd.DataFrame:
    """A chunk shaped for a file: only the declared columns (a missing one is a named error), UI-only
    columns refused by name, dates DD/MM/YYYY. Ids stay strings; `export.build_export` writes them as text."""
    banned = [c for c in columns if c in BILL_UI_ONLY]
    if banned:
        raise ExportSchemaError(f"{what}: {banned} are screen affordances, not data, and cannot be exported.")
    out = select(frame, columns, what).copy()
    if "date" in out.columns:
        out["date"] = out["date"].map(display_date)
    return out
