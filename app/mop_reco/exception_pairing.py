"""Pair open exception bills with the unmapped transactions that probably paid them (2026-10-14).

The system FINDS the candidates so nobody types a transaction number it can find itself:
unmapped transactions in the SAME store for the SAME amount (exact, to the paisa), within a
configurable window of the bill - first +/- `window_minutes` (needs a time on both sides), then
+/- `window_days`. Nearest in time first. Time only ORDERS the candidates and narrows the
search; it never decides a match - a person (or the one-click Match, which runs the full manual
match validation) does (CLAUDE.md 6.3).

The pool of unmapped transactions is the engine's own open `unmapped_gateway_transaction`
exceptions (one cheap query, a few thousand rows - never a gateway file), less anything a person
has since matched or refunded. Pure of Streamlit (section 8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .db import Exception_, ManualMatch, TransactionRefund

DEFAULT_WINDOW_MINUTES = 10
DEFAULT_WINDOW_DAYS = 2
MAX_SHOWN = 2
NOON = "12:00:00"


def window_settings(config_raw: dict) -> tuple:
    section = (config_raw or {}).get("exception_pairing") or {}
    return (int(section.get("window_minutes", DEFAULT_WINDOW_MINUTES)),
            int(section.get("window_days", DEFAULT_WINDOW_DAYS)))


def unmapped_pool(session) -> pd.DataFrame:
    """Open / under-investigation unmapped transactions that no person has since matched or
    refunded. Columns: gateway, txn_id, store, amount, date, time, exception_key."""
    rows = (session.query(Exception_.exception_key, Exception_.business_date, Exception_.store,
                          Exception_.amount, Exception_.detail, Exception_.transaction_time)
            .filter(Exception_.type == "unmapped_gateway_transaction",
                    Exception_.status.in_(("open", "under_investigation"))).all())
    taken = {(g.lower(), r) for g, r in session.query(ManualMatch.gateway, ManualMatch.transaction_ref)
             .filter(ManualMatch.lifecycle == "active", ManualMatch.status == "matched_manual").all()}
    taken |= {(g.lower(), r) for g, r in session.query(ManualMatch.gateway_2, ManualMatch.transaction_ref_2)
              .filter(ManualMatch.lifecycle == "active", ManualMatch.status == "matched_manual",
                      ManualMatch.transaction_ref_2.isnot(None)).all()}
    taken |= {(g.lower(), r) for g, r in session.query(TransactionRefund.gateway,
                                                       TransactionRefund.transaction_ref).all()}
    out = []
    for key, bdate, store, amount, detail, ttime in rows:
        try:
            d = json.loads(detail) if detail else {}
        except ValueError:
            d = {}
        gw, tid = (d.get("gateway") or "").lower(), d.get("transaction_id")
        if not gw or not tid or (gw, tid) in taken or not store:
            continue
        out.append({"gateway": gw, "txn_id": str(tid), "store": store, "amount": round(float(amount), 2),
                    "date": bdate, "time": ttime or "", "exception_key": key})
    pool = pd.DataFrame(out, columns=["gateway", "txn_id", "store", "amount", "date", "time", "exception_key"])
    return pool.drop_duplicates(["gateway", "txn_id"]).reset_index(drop=True)




@dataclass
class Candidate:
    gateway: str
    txn_id: str
    amount: float
    date: str
    time: str
    tier: int            # 1 = within the minutes window (both times known), 2 = within the days window
    delta_minutes: float

    @property
    def label(self) -> str:
        return {1: "within minutes", 2: "within days"}[self.tier]


DEFAULT_MOP_FAMILY = {
    "paytm": ("PAYTM EDC", "PaytmDQR", "PAYTM DQR MOREFUN"),
    "axis": ("EDC OTHER AXIS",),
    "bajaj": ("Bajajpay UPI", "Bajajpay Card"),
}
UNTIMED_BASE = 720.0     # minutes - "about half a day, unknown" when either side has no time of day


def mop_family(config_raw: dict) -> dict:
    section = ((config_raw or {}).get("exception_pairing") or {}).get("mop_family")
    return {g: tuple(v) for g, v in (section or DEFAULT_MOP_FAMILY).items()}


def _stamp(day: pd.Series, clock: pd.Series) -> pd.Series:
    clock = clock.where(clock.astype(str).str.len() > 0, NOON).fillna(NOON)
    return pd.to_datetime(day.astype(str) + " " + clock.astype(str), errors="coerce")


_EDGE_COLUMNS = ["bill_key", "txn_id", "gateway", "p_amount", "p_date", "p_time", "tier", "delta", "mop_rank", "store"]


def _edges(bills: pd.DataFrame, pool: pd.DataFrame, window_minutes, window_days, family: dict) -> pd.DataFrame:
    """Every (bill, transaction) pair that is a possible candidate: same store, EXACT amount, within the
    day window. `delta` is the time apart in minutes when both sides have a time of day; otherwise it is
    UNTIMED_BASE plus whole days - deliberately blunt, because a date with no clock says nothing about
    minutes."""
    if bills.empty or pool.empty:
        return pd.DataFrame(columns=_EDGE_COLUMNS)
    b = bills.copy()
    if "mop" not in b.columns:
        b["mop"] = ""
    b["amount"] = b["amount"].astype(float).round(2)
    b["b_timed"] = b["time"].fillna("").astype(str).str.len() > 0
    b["b_at"] = _stamp(b["date"], b["time"].fillna(""))
    p = pool.rename(columns={"date": "p_date", "time": "p_time", "amount": "p_amount"}).copy()
    p["p_timed"] = p["p_time"].fillna("").astype(str).str.len() > 0
    p["p_at"] = _stamp(p["p_date"], p["p_time"].fillna(""))
    m = b.merge(p, left_on=["store", "amount"], right_on=["store", "p_amount"], how="inner")
    if m.empty:
        return pd.DataFrame(columns=_EDGE_COLUMNS)
    m["day_gap"] = (pd.to_datetime(m["p_date"]) - pd.to_datetime(m["date"])).abs().dt.days
    m = m[m["day_gap"] <= int(window_days)].copy()
    if m.empty:
        return pd.DataFrame(columns=_EDGE_COLUMNS)
    timed = m["b_timed"] & m["p_timed"]
    m["delta"] = (m["p_at"] - m["b_at"]).abs().dt.total_seconds() / 60.0
    m.loc[~timed, "delta"] = UNTIMED_BASE + m.loc[~timed, "day_gap"] * 1440.0
    m["tier"] = (timed & (m["delta"] <= float(window_minutes))).map({True: 1, False: 2})
    m["mop_rank"] = [0 if mp in family.get(g, ()) else 1 for mp, g in zip(m["mop"], m["gateway"])]
    return m[_EDGE_COLUMNS]


# --- the global assignment -----------------------------------------------------------------

@dataclass
class _Edge:
    bill: str
    txns: tuple
    key: tuple


def _resolve(edges: list, blocked: set = None):
    """Assign transactions to bills so that EACH TRANSACTION GOES TO AT MOST ONE BILL (and each bill takes
    one edge). Edges are taken best key first; edges sharing a key are a tie group, and where members of a
    tie group share a bill or a transaction NOBODY in that component gets anything - they come back as
    an ambiguous cluster. Never decided by row order.

    Returns (assigned {bill: txns}, clusters [{"bills": [...], "txns": [...]}], used txns)."""
    edges = sorted(edges, key=lambda e: (e.key, e.bill, e.txns))
    assigned: dict = {}
    clusters: list = []
    done_bills: set = set()
    used: set = set(blocked or ())
    i = 0
    while i < len(edges):
        e = edges[i]
        if e.bill in done_bills or any(t in used for t in e.txns):
            i += 1
            continue
        j, group = i, []
        while j < len(edges) and edges[j].key == e.key:
            g = edges[j]
            if g.bill not in done_bills and not any(t in used for t in g.txns):
                group.append(g)
            j += 1
        parent: dict = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for g in group:
            for t in g.txns:
                parent[find(("b", g.bill))] = find(("t", t))
        comps: dict = {}
        for g in group:
            comps.setdefault(find(("b", g.bill)), []).append(g)
        for comp in comps.values():
            if len(comp) == 1:
                assigned[comp[0].bill] = comp[0].txns
                done_bills.add(comp[0].bill)
                used.update(comp[0].txns)
            else:
                bills_c = sorted({g.bill for g in comp})
                txns_c = sorted({t for g in comp for t in g.txns})
                clusters.append({"bills": bills_c, "txns": txns_c})
                done_bills.update(bills_c)
                used.update(txns_c)
        i = j
    return assigned, clusters, used


@dataclass
class Assignment:
    candidates: dict = field(default_factory=dict)   # bill_key -> [Candidate]   (1 = exact, 2 = a genuine split)
    kind: dict = field(default_factory=dict)         # bill_key -> "exact" | "split"
    ambiguous: dict = field(default_factory=dict)    # bill_key -> cluster id
    clusters: dict = field(default_factory=dict)     # cluster id -> {"bills": [...], "txns": [...]}

    def flag(self, bill_key: str) -> str:
        cid = self.ambiguous.get(bill_key)
        if cid is None:
            return ""
        c = self.clusters[cid]
        return f"ambiguous - {len(c['bills'])} bills, {len(c['txns'])} transactions"


class DuplicateSuggestion(AssertionError):
    """A transaction was offered to more than one bill - the build must fail, not ship it."""


def assert_unique(a: Assignment) -> None:
    """No transaction id appears more than once across Txn 1 and Txn 2 over the whole population, and
    none that sits in an ambiguous cluster is also suggested to anyone."""
    seen: dict = {}
    for bill, cands in a.candidates.items():
        for c in cands:
            if c.txn_id in seen:
                raise DuplicateSuggestion(
                    f"transaction {c.txn_id} is suggested more than once (bills {seen[c.txn_id]} and {bill})")
            seen[c.txn_id] = bill
    held = {t for c in a.clusters.values() for t in c["txns"]}
    clash = held & set(seen)
    if clash:
        raise DuplicateSuggestion(f"transaction(s) {sorted(clash)[:3]} are both suggested and in an ambiguous cluster")


def assign(bills: pd.DataFrame, pool: pd.DataFrame, window_minutes=DEFAULT_WINDOW_MINUTES,
           window_days=DEFAULT_WINDOW_DAYS, family: dict = None) -> Assignment:
    """The candidate set for a WHOLE open population, as one global assignment.

    `bills`: bill_key, store, amount, date, time, mop - the ENTIRE open population, never a page or a
    filter, or the answer would depend on what is on screen.

    1. EXACT: same store, exact amount, within the window. A transaction is offered to at most one bill.
       A contest is won by the smallest time difference, then by the bill whose MOP belongs to the
       transaction's gateway; a still-tied contest gives nobody the transaction - an ambiguous cluster,
       flagged and left blank.
    2. SPLIT: a bill with no exact candidate at all may have TWO free transactions whose amounts sum
       exactly to the bill (a split payment). Same constraints.
    The result is asserted: no transaction id appears twice anywhere."""
    family = family or DEFAULT_MOP_FAMILY
    out = Assignment()
    if bills.empty or pool.empty:
        return out
    bills = bills.drop_duplicates("bill_key")
    pool = pool.drop_duplicates(["gateway", "txn_id"])
    info = {r.txn_id: r for r in pool.itertuples(index=False)}
    edges_df = _edges(bills, pool, window_minutes, window_days, family)

    def cand(txn_id, tier, delta):
        r = info[txn_id]
        return Candidate(r.gateway, r.txn_id, float(r.amount), r.date, r.time or "", tier, float(delta))

    # ---- 1. exact
    exact = [_Edge(r.bill_key, (r.txn_id,), (round(r.delta, 3), r.mop_rank))
             for r in edges_df.itertuples(index=False)]
    tiers = {(r.bill_key, r.txn_id): (int(r.tier), float(r.delta)) for r in edges_df.itertuples(index=False)}
    assigned, clusters, used = _resolve(exact)
    for bill, txns in assigned.items():
        tier, delta = tiers[(bill, txns[0])]
        out.candidates[bill] = [cand(txns[0], tier, delta)]
        out.kind[bill] = "exact"
    blocked_of: dict = {}
    for c in clusters:
        cid = len(out.clusters)
        out.clusters[cid] = c
        for b in c["bills"]:
            out.ambiguous[b] = cid
        for t in c["txns"]:
            blocked_of[t] = cid
    # a bill whose candidates all sit in an ambiguous cluster belongs to that cluster
    for r in edges_df.itertuples(index=False):
        if r.bill_key not in out.candidates and r.bill_key not in out.ambiguous and r.txn_id in blocked_of:
            cid = blocked_of[r.txn_id]
            out.ambiguous[r.bill_key] = cid
            out.clusters[cid]["bills"].append(r.bill_key)

    # ---- 2. split: two free transactions that sum exactly to a bill that has no exact candidate at all
    has_edge = set(edges_df["bill_key"]) if not edges_df.empty else set()
    free = pool[~pool["txn_id"].isin(used)]
    leftovers = bills[~bills["bill_key"].isin(has_edge)]
    split_edges, pair_info = [], {}
    if not free.empty and not leftovers.empty:
        by_store = {st: g for st, g in free.groupby("store")}
        for b in leftovers.itertuples(index=False):
            g = by_store.get(b.store)
            target = round(float(b.amount) * 100)
            if g is None or len(g) < 2 or target <= 0:
                continue
            b_timed = bool(b.time)
            b_at = pd.Timestamp(f"{b.date} {b.time or NOON}")
            by_amt: dict = {}
            for r in g.itertuples(index=False):
                gap = abs((pd.Timestamp(r.date) - pd.Timestamp(b.date)).days)
                if gap > int(window_days):
                    continue
                delta = (abs((pd.Timestamp(f"{r.date} {r.time}") - b_at).total_seconds()) / 60.0
                         if (b_timed and r.time) else UNTIMED_BASE + gap * 1440.0)
                by_amt.setdefault(round(float(r.amount) * 100), []).append((r, delta))
            seen_pairs = set()
            for cents, items in by_amt.items():
                for r, delta in items:
                    for r2, delta2 in by_amt.get(target - cents, []):
                        pair = tuple(sorted((r.txn_id, r2.txn_id)))
                        if r2.txn_id == r.txn_id or pair in seen_pairs:
                            continue
                        seen_pairs.add(pair)
                        rank = sum(0 if (b.mop in family.get(x.gateway, ())) else 1 for x in (r, r2))
                        split_edges.append(_Edge(b.bill_key, pair, (round(max(delta, delta2), 3), rank)))
                        pair_info[(b.bill_key, pair)] = {r.txn_id: delta, r2.txn_id: delta2}
    if split_edges:
        s_assigned, s_clusters, _ = _resolve(split_edges, blocked=used)
        for bill, pair in s_assigned.items():
            ordered = sorted(pair, key=lambda t: (-info[t].amount, t))
            out.candidates[bill] = [cand(t, 2, pair_info[(bill, pair)][t]) for t in ordered]
            out.kind[bill] = "split"
        for c in s_clusters:
            cid = len(out.clusters)
            out.clusters[cid] = c
            for b in c["bills"]:
                out.ambiguous[b] = cid
    assert_unique(out)
    return out


def open_population(session, run_id: int) -> pd.DataFrame:
    """EVERY open bill the Exceptions tab works on (open or under investigation) - the population the
    global assignment must be computed over, whatever filter is on screen."""
    from . import queries as q
    rows = (session.query(Exception_.exception_key, Exception_.business_date, Exception_.store, Exception_.amount,
                          Exception_.detail)
            .filter(Exception_.type.in_(q.NOT_RECEIVED_TYPES),
                    Exception_.status.in_(("open", "under_investigation"))).all())
    out = []
    for key, bdate, store, amount, detail in rows:
        try:
            d = json.loads(detail) if detail else {}
        except ValueError:
            d = {}
        out.append({"bill_key": key, "store": store, "amount": float(amount), "date": bdate,
                    "bill_no": d.get("bill_no"), "mop": d.get("mop")})
    bills = pd.DataFrame(out, columns=["bill_key", "store", "amount", "date", "bill_no", "mop"])
    if bills.empty:
        bills["time"] = []
        return bills
    facts = q.unmatched_bill_facts(session, run_id, bill_nos=bills["bill_no"].tolist())
    bills["time"] = [(facts.get((r.date, r.store, r.bill_no, r.mop)) or ("", ""))[0] or ""
                     for r in bills.itertuples()]
    return bills


def count_summary(bills: pd.DataFrame, a: Assignment) -> dict:
    """Counts AND values: exact (one candidate), split (two that sum to the bill), ambiguous, none."""
    amt = dict(zip(bills["bill_key"], bills["amount"].astype(float)))
    keys = set(amt)
    exact = {k for k, v in a.kind.items() if v == "exact" and k in keys}
    split = {k for k, v in a.kind.items() if v == "split" and k in keys}
    amb = {k for k in a.ambiguous if k in keys} - exact - split
    none = keys - exact - split - amb
    val = lambda ks: round(sum(amt[k] for k in ks), 2)  # noqa: E731
    return {"bills": len(keys), "exact": len(exact), "exact_value": val(exact), "split": len(split),
            "split_value": val(split), "ambiguous": len(amb), "ambiguous_value": val(amb), "none": len(none),
            "none_value": val(none), "suggested_ids": len({c.txn_id for v in a.candidates.values() for c in v}),
            "clusters": len(a.clusters)}


_SECOND_COLUMNS = ["bill_no", "store", "bill_date", "amount", "matched_txn", "second_gateway", "second_txn_id",
                   "second_date", "second_time"]


def second_transaction_still_open(session, pool: pd.DataFrame, window_days=DEFAULT_WINDOW_DAYS) -> pd.DataFrame:
    """Bills a person has matched (one transaction) that STILL have another unmapped transaction of the
    same store and amount nearby: a genuine duplicate payment. The bill stays closed, the second
    transaction stays open, and this list is where a person decides - refund it (Refund upload) or find
    the second bill. Nothing is forced to 1:1."""
    rows = (session.query(ManualMatch).filter(ManualMatch.lifecycle == "active",
                                              ManualMatch.status == "matched_manual").all())
    if not rows or pool.empty:
        return pd.DataFrame(columns=_SECOND_COLUMNS)
    bills = pd.DataFrame([{"bill_key": r.bill_key, "store": r.store, "amount": float(r.amount or 0),
                           "date": r.business_date, "time": "", "mop": r.mop, "bill_no": r.bill_no,
                           "matched_txn": r.transaction_ref} for r in rows])
    edges = _edges(bills, pool, 0, window_days, DEFAULT_MOP_FAMILY)
    meta = {r.bill_key: r for r in bills.itertuples(index=False)}
    out = []
    for e in edges.itertuples(index=False):
        b = meta[e.bill_key]
        if e.txn_id == b.matched_txn:
            continue
        out.append({"bill_no": b.bill_no, "store": b.store, "bill_date": b.date, "amount": b.amount,
                    "matched_txn": b.matched_txn, "second_gateway": e.gateway, "second_txn_id": e.txn_id,
                    "second_date": e.p_date, "second_time": e.p_time})
    return pd.DataFrame(out, columns=_SECOND_COLUMNS)
