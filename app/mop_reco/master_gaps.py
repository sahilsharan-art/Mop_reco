"""The standing list of identifiers no master resolves - one place to send to the people who own the masters.

Written by the run (`persist`) from the data it just read; the Masters page reads it and adds, live, whether each
identifier is in the CURRENT master (`current_status`), because masters are dated by when they were loaded (CLAUDE.md
6.12): an identifier added to a master AFTER a business date is not applied to that date, so a run can report it
unresolved even though the master now holds it. Both facts are shown - nothing is guessed, nothing is inferred.

No Streamlit imports (CLAUDE.md section 8).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from . import masters
from .db import CmsSlip, MasterGap

SOURCE_LABEL = {"paytm_mid": "Paytm MID", "axis_mid": "Axis MID", "pickup_code": "CMS pickup code",
                "cms_bnk": "CMS - BNK pseudo-store"}


def _gateway_gaps(files: dict, id_col: str, amount_col: str, date_from: str, date_to: str) -> pd.DataFrame:
    frames = []
    for day, path in sorted(files.items()):
        if not (date_from <= day <= date_to):
            continue
        d = pd.read_parquet(path, columns=[id_col, "store", amount_col])
        d = d[d["store"].isna()]
        if not d.empty:
            d = d.assign(day=day)
            frames.append(d)
    if not frames:
        return pd.DataFrame(columns=["identifier", "rows", "value", "first_seen", "last_seen"])
    d = pd.concat(frames, ignore_index=True)
    d[amount_col] = pd.to_numeric(d[amount_col], errors="coerce").fillna(0.0)
    g = (d.groupby(id_col).agg(rows=(amount_col, "size"), value=(amount_col, "sum"),
                               first_seen=("day", "min"), last_seen=("day", "max")).reset_index()
         .rename(columns={id_col: "identifier"}))
    return g


def persist(session, run_id: int, date_from: str, date_to: str, paytm_files: dict, axis_files: dict) -> dict:
    """Replace this run's gap rows. Paytm and Axis from the warehouse files the run read (rows with no store), the
    CMS gaps from the slips the run stored."""
    session.query(MasterGap).filter(MasterGap.run_id == run_id).delete()
    out = []
    for source, files, id_col, amt in (("paytm_mid", paytm_files, "original_mid", "amount"),
                                       ("axis_mid", axis_files, "MID", "GROSS_AMT")):
        for r in _gateway_gaps(files, id_col, amt, date_from, date_to).itertuples(index=False):
            out.append(MasterGap(run_id=run_id, source=source, identifier=str(r.identifier), location=None,
                                 rows=int(r.rows), value=float(r.value), first_seen=r.first_seen,
                                 last_seen=r.last_seen))
    cms = pd.read_sql(CmsSlip.__table__.select().where(CmsSlip.run_id == run_id, CmsSlip.row_type == "COL"),
                      session.bind)
    if not cms.empty:
        cms["amount"] = cms["amount"].astype(float)
        for source, store_value in (("pickup_code", "UNRESOLVED"), ("cms_bnk", "BNK")):
            w = cms[cms["store"] == store_value]
            if w.empty:
                continue
            g = (w.groupby(["pickup_code", "location"], dropna=False)
                 .agg(rows=("amount", "size"), value=("amount", "sum"), first_seen=("dep_date", "min"),
                      last_seen=("dep_date", "max")).reset_index())
            for r in g.itertuples(index=False):
                out.append(MasterGap(run_id=run_id, source=source, identifier=str(r.pickup_code),
                                     location=None if pd.isna(r.location) else str(r.location), rows=int(r.rows),
                                     value=float(r.value), first_seen=r.first_seen, last_seen=r.last_seen))
    session.add_all(out)
    session.commit()
    return {"rows": len(out), "by_source": pd.Series([g.source for g in out]).value_counts().to_dict() if out else {}}


def current_status(session, source: str, identifier: str, location) -> tuple:
    """(is_in_current_master, store, master_version_loaded_at). 'Current' = the newest stored version, whatever its
    load date - the lookup for a date after the newest load."""
    when = datetime(2999, 1, 1)
    if source == "pickup_code":
        r = masters.resolve_pickup_store(session, identifier, location, when)
        name = "pickup_code"
    elif source == "cms_bnk":
        return True, "BNK", None            # BNK is a master entry, kept as its own pseudo-store
    else:
        r = masters.resolve_store(session, source, identifier, when)
        name = {"paytm_mid": "mid_paytm", "axis_mid": "mid_axis"}[source]
    version = masters._find_version_asof(session, name, when)
    loaded = str(version.loaded_at)[:16] if version is not None else None
    return (r.status == "resolved"), (r.store if r.status == "resolved" else None), loaded


def frame(session, run_id: int) -> pd.DataFrame:
    """The standing list for the page and the CSV: the run's facts plus the live 'in the current master' columns."""
    rows = session.query(MasterGap).filter(MasterGap.run_id == run_id).all()
    out = []
    for g in rows:
        in_master, store, loaded = current_status(session, g.source, g.identifier, g.location)
        out.append({"source": SOURCE_LABEL.get(g.source, g.source), "code": g.identifier, "location": g.location or "",
                    "rows": g.rows, "value": round(float(g.value), 2), "first_seen": g.first_seen or "",
                    "last_seen": g.last_seen or "",
                    "in_current_master": "yes" if in_master else "no",
                    "current_master_store": store or "", "current_master_loaded": loaded or ""})
    df = pd.DataFrame(out, columns=["source", "code", "location", "rows", "value", "first_seen", "last_seen",
                                    "in_current_master", "current_master_store", "current_master_loaded"])
    return df.sort_values(["source", "value"], ascending=[True, False]).reset_index(drop=True) if len(df) else df
