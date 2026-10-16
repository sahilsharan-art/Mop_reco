"""Read-side queries for the console. No Streamlit imports.

Every number a view shows comes from here, so it can be tested. All of it reads
precomputed rows written by a run (CLAUDE.md 6.14) — never a raw file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import case, func, or_

from .db import (
    Exception_,
    ManualMatch,
    ManualMatchEvent,
    R1BillLine,
    ReconciliationResult,
    Remark,
    Run,
)
from .exceptions import OPEN_STATUSES, severity_for

MATCHED_TIERS = ("Tier 1", "Matched (EDC OTHER)")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _apply_bill_filters(query, run_id, date_from=None, date_to=None, store=None):
    query = query.filter(R1BillLine.run_id == run_id)
    if date_from is not None:
        query = query.filter(R1BillLine.business_date >= str(date_from))
    if date_to is not None:
        query = query.filter(R1BillLine.business_date <= str(date_to))
    if store:
        query = query.filter(R1BillLine.store == store)
    return query


def _apply_exception_filters(query, date_from=None, date_to=None, store=None,
                             open_only=True):
    if date_from is not None:
        query = query.filter(Exception_.business_date >= str(date_from))
    if date_to is not None:
        query = query.filter(Exception_.business_date <= str(date_to))
    if store:
        query = query.filter(Exception_.store == store)
    if open_only:
        query = query.filter(Exception_.status.in_(OPEN_STATUSES))
    return query


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    digital_lines: int = 0
    matched: int = 0
    unmatched: int = 0
    digital_value: float = 0.0
    matched_value: float = 0.0
    unmatched_value: float = 0.0
    unmapped_txn_count: int = 0
    unmapped_txn_value: float = 0.0
    open_exceptions: int = 0
    open_exception_value: float = 0.0

    @property
    def match_rate(self) -> float:
        return (self.matched / self.digital_lines) if self.digital_lines else 0.0

    @property
    def is_clean(self) -> bool:
        return self.unmatched == 0 and self.open_exceptions == 0


def verdict(session, run_id, date_from=None, date_to=None, store=None) -> Verdict:
    """The five headline figures, and the numbers behind the verdict line."""
    row = _apply_bill_filters(
        session.query(
            func.count(R1BillLine.id),
            func.sum(case((R1BillLine.matched.is_(True), 1), else_=0)),
            func.sum(R1BillLine.amount),
            func.sum(case((R1BillLine.matched.is_(True), R1BillLine.amount), else_=0)),
            func.sum(case((R1BillLine.matched.is_(False), R1BillLine.amount), else_=0)),
        ),
        run_id, date_from, date_to, store,
    ).one()

    lines, matched, value, matched_value, unmatched_value = row
    v = Verdict(
        digital_lines=int(lines or 0),
        matched=int(matched or 0),
        unmatched=int((lines or 0) - (matched or 0)),
        digital_value=float(value or 0),
        matched_value=float(matched_value or 0),
        unmatched_value=float(unmatched_value or 0),
    )

    # A manual match cascades here too (2026-09-13) — the moment a bill is
    # matched manually, it must count as matched on this page immediately,
    # not only after a re-run. Bounded by the (small) set of active manual
    # matches in scope, never a scan of the month's bill lines: bill_no is
    # globally unique (§5), so the only extra query is "of these bill_nos,
    # which are STILL engine-unmatched" — the ones already engine-matched are
    # superseded overlay rows and already counted above.
    manual_scope = (session.query(ManualMatch.bill_no)
                    .filter(ManualMatch.lifecycle == "active"))
    if date_from is not None:
        manual_scope = manual_scope.filter(ManualMatch.business_date >= str(date_from))
    if date_to is not None:
        manual_scope = manual_scope.filter(ManualMatch.business_date <= str(date_to))
    if store:
        manual_scope = manual_scope.filter(ManualMatch.store == store)
    manual_bill_nos = {b for (b,) in manual_scope.all() if b}
    if manual_bill_nos:
        still_open = _apply_bill_filters(
            session.query(func.count(R1BillLine.id), func.sum(R1BillLine.amount)),
            run_id, date_from, date_to, store,
        ).filter(R1BillLine.matched.is_(False),
                 R1BillLine.bill_no.in_(manual_bill_nos)).one()
        extra_count = int(still_open[0] or 0)
        extra_value = float(still_open[1] or 0)
        v.matched += extra_count
        v.matched_value += extra_value
        v.unmatched -= extra_count
        v.unmatched_value -= extra_value

    exc = _apply_exception_filters(
        session.query(func.count(Exception_.id), func.sum(Exception_.amount)),
        date_from, date_to, store,
    ).one()
    v.open_exceptions = int(exc[0] or 0)
    v.open_exception_value = float(exc[1] or 0)

    unmapped = _apply_exception_filters(
        session.query(func.count(Exception_.id), func.sum(Exception_.amount)),
        date_from, date_to, store,
    ).filter(Exception_.type == "unmapped_gateway_transaction").one()
    v.unmapped_txn_count = int(unmapped[0] or 0)
    v.unmapped_txn_value = float(unmapped[1] or 0)

    return v


def verdict_sentence(v: Verdict, range_label: str) -> str:
    """One plain sentence, before any table."""
    from .formatting import count as fmt_count
    from .formatting import money_short, percent

    if v.digital_lines == 0:
        return f"{range_label}: nothing loaded for this selection."

    rate = percent(v.matched, v.digital_lines)
    if v.is_clean:
        return (f"{range_label}: {rate} matched, {money_short(v.digital_value)} of digital "
                f"sale fully reconciled. Nothing open.")

    # Never "Rs X at risk" over all exception types — see risk_split() for why
    # adding money-not-received to money-received-but-unbilled means nothing.
    return (
        f"{range_label}: {rate} matched. "
        f"{money_short(v.unmatched_value)} not received across "
        f"{fmt_count(v.unmatched)} bills raised. "
        f"{fmt_count(v.unmapped_txn_count)} gateway "
        f"{'payment' if v.unmapped_txn_count == 1 else 'payments'} "
        f"({money_short(v.unmapped_txn_value)}) "
        f"{'has' if v.unmapped_txn_count == 1 else 'have'} no bill behind "
        f"{'it' if v.unmapped_txn_count == 1 else 'them'}."
    )


# ---------------------------------------------------------------------------
# The two risk figures — deliberately NOT one number
#
# Adding these together produces a figure that means nothing, because they
# point in opposite directions:
#   not_received  — a bill was raised and no payment was found. Money we did
#                   NOT get. This is the exposure.
#   received_not_billed — a successful gateway payment has no bill behind it.
#                   Money we DID get but never recorded. An accounting gap,
#                   not a loss.
# A third group is neither: a reversal whose original sale is outside the
# loaded range. It is reported on its own rather than folded into either.
# ---------------------------------------------------------------------------

# Exception types that mean "a bill was raised and the payment was not found".
NOT_RECEIVED_TYPES = ("bill_not_matched", "ambiguous_edc_other", "no_source_report")
# Exception types that mean "the payment arrived with no bill behind it".
RECEIVED_NOT_BILLED_TYPES = ("unmapped_gateway_transaction",)


@dataclass
class RiskSplit:
    not_received_count: int = 0
    not_received_value: float = 0.0
    received_not_billed_count: int = 0
    received_not_billed_value: float = 0.0
    other_count: int = 0
    other_value: float = 0.0

    @property
    def total_count(self) -> int:
        return self.not_received_count + self.received_not_billed_count + self.other_count


def risk_split(session, date_from=None, date_to=None, store=None) -> RiskSplit:
    """The two opposite figures, counted separately, plus whatever is neither."""
    rows = _apply_exception_filters(
        session.query(Exception_.type, func.count(Exception_.id),
                      func.sum(Exception_.amount)),
        date_from, date_to, store,
    ).group_by(Exception_.type).all()

    split = RiskSplit()
    for kind, n, value in rows:
        n, value = int(n or 0), float(value or 0)
        if kind in NOT_RECEIVED_TYPES:
            split.not_received_count += n
            split.not_received_value += value
        elif kind in RECEIVED_NOT_BILLED_TYPES:
            split.received_not_billed_count += n
            split.received_not_billed_value += value
        else:
            split.other_count += n
            split.other_value += value
    return split


# Reasons a bill was not matched, in the order they are applied. Each bill gets
# exactly ONE reason, so the totals never overlap — a duplicate awaiting IT
# deletion must never sit inside the same number as a genuinely missing payment.
REASON_LABEL = {
    "reference_present_txn_not_found":
        "Reference present, transaction not found",
    "ambiguous_multiple_candidates":
        "EDC OTHER — more than one candidate",
    "suspected_duplicate_bill":
        "Suspected duplicate bill",
    "no_reference_at_pos":
        "No reference captured at POS",
    "amount_mismatch":
        "Amount mismatch",
}

REASON_EXPOSURE = {
    "reference_present_txn_not_found":
        "Genuine exposure — the bill carries a reference the gateway cannot "
        "account for. Chase the gateway.",
    "ambiguous_multiple_candidates":
        "Needs an operator — more than one transaction fits, and time never "
        "decides a match.",
    "suspected_duplicate_bill":
        "Probably not a loss — an identical bill on the same store and date "
        "did match, so an IT deletion is most likely pending.",
    "no_reference_at_pos":
        "A capture failure at the till. Needs the store, not the gateway.",
    "amount_mismatch":
        "Only the difference is at risk, not the whole bill.",
}

REASON_ORDER = ("reference_present_txn_not_found", "ambiguous_multiple_candidates",
                "no_reference_at_pos", "suspected_duplicate_bill", "amount_mismatch")


def not_received_by_store(session, run_id, date_from=None, date_to=None,
                          store=None) -> pd.DataFrame:
    """Amount not received per STORE, biggest first — who to call.

    The reason breakdown answered "what kind of problem is this"; it did not
    answer "which store do I ring". This does. The reason survives as a column
    on the bill rows underneath (see `not_received_bills`), because whether a
    bill is a suspected duplicate or a genuinely missing payment still matters
    — just not as the primary cut.

    `oldest_bill_date` is the age of the problem: a store whose oldest
    unmatched bill is three weeks old is a different call from one whose oldest
    is yesterday.

    A manual match cascades here too (2026-09-13) — a manually-matched bill
    is excluded from "not received" the moment it is recorded, via
    `_active_manual_bill_nos()`, never only after a re-run.
    """
    manual_bill_nos = _active_manual_bill_nos(session, date_from, date_to, store)
    query = _apply_bill_filters(
        session.query(
            R1BillLine.store,
            func.count(R1BillLine.id),
            func.sum(R1BillLine.amount),
            func.min(R1BillLine.business_date),
            func.max(R1BillLine.business_date),
        ),
        run_id, date_from, date_to, store,
    ).filter(R1BillLine.matched.is_(False))
    if manual_bill_nos:
        query = query.filter(~R1BillLine.bill_no.in_(manual_bill_nos))
    rows = query.group_by(R1BillLine.store).all()

    df = pd.DataFrame([{
        "store": store_code,
        "bills": int(count or 0),
        "not_received": float(value or 0),
        "oldest_bill_date": oldest,
        "newest_bill_date": newest,
    } for store_code, count, value, oldest, newest in rows])
    if df.empty:
        return df
    return df.sort_values("not_received", ascending=False).reset_index(drop=True)


def not_received_bills(session, run_id, store, date_from=None,
                       date_to=None) -> pd.DataFrame:
    """The unmatched bills behind one store, each carrying its reason.

    Loaded only when a store is expanded — 205 bills across 40-odd stores is
    small, but querying every store's bills up front to render a collapsed
    table would be work nobody asked for. A manual match excludes a bill here
    the same way it does in `not_received_by_store` — cascades immediately.
    """
    reasons = _reason_by_bill_key(session, run_id, date_from, date_to, store)
    manual_bill_nos = _active_manual_bill_nos(session, date_from, date_to, store)
    query = _apply_bill_filters(
        session.query(R1BillLine), run_id, date_from, date_to, store,
    ).filter(R1BillLine.matched.is_(False))
    if manual_bill_nos:
        query = query.filter(~R1BillLine.bill_no.in_(manual_bill_nos))
    rows = query.order_by(R1BillLine.amount.desc()).all()

    return pd.DataFrame([{
        "business_date": r.business_date,
        "bill_no": r.bill_no,
        "mop": r.mop,
        "amount": float(r.amount or 0),
        "reason": REASON_LABEL.get(reasons.get(r.id), "—"),
        "paytm_ref": r.paytm_ref,
        "axis_ref": r.axis_ref,
        "bajaj_ref": r.bajaj_ref,
    } for r in rows])


def _reason_by_bill_key(session, run_id, date_from, date_to, store) -> dict:
    """{bill_line.id: reason} using the same precedence as
    unmatched_by_reason, so the two can never disagree."""
    unmatched = _apply_bill_filters(
        session.query(R1BillLine.id, R1BillLine.business_date, R1BillLine.store,
                      R1BillLine.amount, R1BillLine.match_tier,
                      R1BillLine.paytm_ref, R1BillLine.axis_ref,
                      R1BillLine.bajaj_ref),
        run_id, date_from, date_to, store,
    ).filter(R1BillLine.matched.is_(False)).all()
    if not unmatched:
        return {}

    keys = {(r.store, r.business_date, r.amount) for r in unmatched}
    twins = session.query(R1BillLine.store, R1BillLine.business_date,
                          R1BillLine.amount).filter(
        R1BillLine.run_id == run_id,
        R1BillLine.matched.is_(True),
        R1BillLine.store.in_({k[0] for k in keys}),
        R1BillLine.business_date.in_({k[1] for k in keys}),
        R1BillLine.amount.in_({k[2] for k in keys}),
    ).distinct().all()
    matched_keys = {(t.store, t.business_date, t.amount) for t in twins} & keys

    def blank(value):
        return value is None or str(value).strip() in ("", "nan", "None")

    out = {}
    for r in unmatched:
        if not (blank(r.paytm_ref) and blank(r.axis_ref) and blank(r.bajaj_ref)):
            out[r.id] = "reference_present_txn_not_found"
        elif r.match_tier == "Ambiguous":
            out[r.id] = "ambiguous_multiple_candidates"
        elif (r.store, r.business_date, r.amount) in matched_keys:
            out[r.id] = "suspected_duplicate_bill"
        else:
            out[r.id] = "no_reference_at_pos"
    return out


def unmatched_by_reason(session, run_id, date_from=None, date_to=None,
                        store=None) -> pd.DataFrame:
    """Why each unmatched bill is unmatched — one reason per bill, no overlap.

    Reads only stored bill-line columns, never a raw or warehouse file (§6.14),
    and changes no matching logic: it labels a decision R1 already made.

    Precedence, and why:
      1. the bill carries a gateway reference  -> the reference is the story,
         whatever else is true of the bill. This must outrank the duplicate
         test, or a real defect hides inside "probably not a loss".
      2. Tier 2 found more than one candidate  -> ambiguous, an operator decides
      3. no reference, but an identical bill on the same store/date/amount DID
         match -> suspected duplicate (§6.8), IT deletion pending
      4. no reference and no matched twin      -> a capture failure at the till
    """
    unmatched = _apply_bill_filters(
        session.query(R1BillLine.business_date, R1BillLine.store, R1BillLine.amount,
                      R1BillLine.match_tier, R1BillLine.paytm_ref,
                      R1BillLine.axis_ref, R1BillLine.bajaj_ref),
        run_id, date_from, date_to, store,
    ).filter(R1BillLine.matched.is_(False)).all()

    if not unmatched:
        return pd.DataFrame(columns=["reason", "label", "bills", "value", "exposure"])

    # The duplicate test asks whether a MATCHED bill exists on the same
    # store+date+amount. Never pull the whole month to answer that: ask the
    # database, restricted to the handful of keys the unmatched bills actually
    # use. Fetching all 660,150 lines to build this set took 8.5s — a page-load
    # budget of under a second (§6.14) does not survive it.
    keys = {(r.store, r.business_date, r.amount) for r in unmatched}
    matched_keys = set()
    if keys:
        twins = session.query(R1BillLine.store, R1BillLine.business_date,
                              R1BillLine.amount).filter(
            R1BillLine.run_id == run_id,
            R1BillLine.matched.is_(True),
            R1BillLine.store.in_({k[0] for k in keys}),
            R1BillLine.business_date.in_({k[1] for k in keys}),
            R1BillLine.amount.in_({k[2] for k in keys}),
        ).distinct().all()
        # The three INs return a superset (any combination of those stores,
        # dates and amounts), so intersect back to the exact tuples.
        matched_keys = {(t.store, t.business_date, t.amount) for t in twins} & keys

    def blank(value):
        return value is None or str(value).strip() in ("", "nan", "None")

    counts = {}
    for r in unmatched:
        if not (blank(r.paytm_ref) and blank(r.axis_ref) and blank(r.bajaj_ref)):
            reason = "reference_present_txn_not_found"
        elif r.match_tier == "Ambiguous":
            reason = "ambiguous_multiple_candidates"
        elif (r.store, r.business_date, r.amount) in matched_keys:
            reason = "suspected_duplicate_bill"
        else:
            reason = "no_reference_at_pos"
        bills, value = counts.get(reason, (0, 0.0))
        counts[reason] = (bills + 1, value + float(r.amount or 0))

    df = pd.DataFrame([
        {"reason": reason, "label": REASON_LABEL[reason],
         "bills": counts[reason][0], "value": counts[reason][1],
         "exposure": REASON_EXPOSURE[reason]}
        for reason in REASON_ORDER if reason in counts
    ])
    return df


# ---------------------------------------------------------------------------
# Manual matching — system and human results are NEVER blended
#
# A single "99.99% matched" that counts human assertions is a number that
# flatters itself. Every figure below keeps the three apart:
#   system    the engine matched it
#   manual    a person matched it to a transaction that was actually found
#   asserted  a person asserted it with no gateway row behind it — NOT a match
# ---------------------------------------------------------------------------

@dataclass
class MatchSplit:
    lines: int = 0
    value: float = 0.0
    system: int = 0
    system_value: float = 0.0
    manual: int = 0
    manual_value: float = 0.0
    asserted: int = 0
    asserted_value: float = 0.0

    @property
    def unmatched(self) -> int:
        return self.lines - self.system - self.manual - self.asserted

    @property
    def unmatched_value(self) -> float:
        return self.value - self.system_value - self.manual_value - self.asserted_value

    @property
    def system_rate(self) -> float:
        """The engine's own rate — the only one that is not affected by a
        human decision, and therefore the only one comparable across runs."""
        return self.system / self.lines if self.lines else 0.0

    @property
    def with_manual_rate(self) -> float:
        """System plus manual. Excludes asserted on purpose: an assertion with
        no transaction behind it is an open item, not a match."""
        return (self.system + self.manual) / self.lines if self.lines else 0.0


def match_split(session, run_id, date_from=None, date_to=None,
                store=None) -> MatchSplit:
    """The three-way split, read from the database rather than a frame."""
    row = _apply_bill_filters(
        session.query(
            func.count(R1BillLine.id),
            func.sum(R1BillLine.amount),
            func.sum(case((R1BillLine.matched.is_(True), 1), else_=0)),
            func.sum(case((R1BillLine.matched.is_(True), R1BillLine.amount), else_=0)),
        ),
        run_id, date_from, date_to, store,
    ).one()

    split = MatchSplit(
        lines=int(row[0] or 0), value=float(row[1] or 0),
        system=int(row[2] or 0), system_value=float(row[3] or 0),
    )

    overlay = session.query(ManualMatch.status, func.count(ManualMatch.id),
                            func.sum(ManualMatch.amount)) \
        .filter(ManualMatch.lifecycle == "active")
    if date_from:
        overlay = overlay.filter(ManualMatch.business_date >= str(date_from))
    if date_to:
        overlay = overlay.filter(ManualMatch.business_date <= str(date_to))
    if store:
        overlay = overlay.filter(ManualMatch.store == store)

    for status, count, value in overlay.group_by(ManualMatch.status).all():
        if status == "matched_manual":
            split.manual = int(count or 0)
            split.manual_value = float(value or 0)
        elif status == "asserted_unverified":
            split.asserted = int(count or 0)
            split.asserted_value = float(value or 0)
    return split


MANUAL_LIFECYCLE_LABEL = {
    "active": "In force",
    "reversed": "Reversed by a person",
    "superseded": "Redundant — the engine later matched it the same way",
    "conflicted": "CONFLICT — the engine matched it to a different transaction",
    "orphaned": "The bill no longer exists in the dump",
}


def manual_matches(session, date_from=None, date_to=None, store=None,
                   operator=None, reason_code=None, lifecycle=None,
                   status=None) -> pd.DataFrame:
    """Every manual match, filterable. This is the first thing an auditor asks
    to see, so nothing is hidden by default — reversed, superseded, conflicted
    and orphaned rows are all included unless a lifecycle filter is given."""
    query = session.query(ManualMatch)
    if date_from:
        query = query.filter(ManualMatch.business_date >= str(date_from))
    if date_to:
        query = query.filter(ManualMatch.business_date <= str(date_to))
    if store:
        query = query.filter(ManualMatch.store == store)
    if operator:
        query = query.filter(ManualMatch.operator == operator)
    if reason_code:
        query = query.filter(ManualMatch.reason_code == reason_code)
    if lifecycle:
        query = query.filter(ManualMatch.lifecycle == lifecycle)
    if status:
        query = query.filter(ManualMatch.status == status)

    rows = query.order_by(ManualMatch.created_at.desc()).all()
    return pd.DataFrame([{
        "id": r.id,
        "business_date": r.business_date,
        "store": r.store,
        "bill_no": r.bill_no,
        "mop": r.mop,
        "amount": float(r.amount or 0),
        "gateway": r.gateway,
        "transaction_id": r.transaction_ref,
        "status": r.status,
        "case": r.validation_case,
        "override_used": bool(r.override_used),
        "override_reason": r.override_reason,
        "reason_code": r.reason_code,
        "note": r.note,
        "operator": r.operator,
        "created_at": r.created_at,
        "batch_id": r.batch_id,
        "lifecycle": r.lifecycle,
        "lifecycle_note": r.lifecycle_note,
        "reversed_by": r.reversed_by,
    } for r in rows])


def manual_match_options(session, column: str) -> list:
    field = {"operator": ManualMatch.operator,
             "reason_code": ManualMatch.reason_code,
             "lifecycle": ManualMatch.lifecycle,
             "status": ManualMatch.status}[column]
    return sorted(v[0] for v in session.query(field).distinct().all() if v[0])


def manual_match_events(session, manual_match_id: int) -> pd.DataFrame:
    rows = (session.query(ManualMatchEvent)
            .filter(ManualMatchEvent.manual_match_id == manual_match_id)
            .order_by(ManualMatchEvent.at.asc()).all())
    return pd.DataFrame([{"at": r.at, "event": r.event, "operator": r.operator,
                          "detail": r.detail} for r in rows])


def exceptions_by_type(session, date_from=None, date_to=None, store=None) -> pd.DataFrame:
    """Count and value per type, biggest value first."""
    rows = _apply_exception_filters(
        session.query(Exception_.type, func.count(Exception_.id), func.sum(Exception_.amount)),
        date_from, date_to, store,
    ).group_by(Exception_.type).all()

    df = pd.DataFrame(
        [{"type": t, "count": int(n or 0), "value": float(v or 0)} for t, n, v in rows]
    )
    if df.empty:
        return df
    return df.sort_values("value", ascending=False).reset_index(drop=True)


def top_stores_by_exception_value(session, date_from=None, date_to=None,
                                  limit: int = 10, types=None) -> pd.DataFrame:
    """Where the money is. The answer the summary exists to give.

    `types` defaults to the not-received group. Ranking stores on ALL exception
    types would put money-not-received and money-received-but-unbilled in the
    same bar — the same misstatement risk_split() exists to prevent, since a
    store could then top the chart purely on payments it collected and failed
    to bill. Pass an explicit tuple to rank on something else.
    """
    if types is None:
        types = NOT_RECEIVED_TYPES
    rows = _apply_exception_filters(
        session.query(Exception_.store, func.count(Exception_.id), func.sum(Exception_.amount)),
        date_from, date_to, store=None,
    ).filter(Exception_.store.isnot(None), Exception_.store != "None",
             Exception_.type.in_(tuple(types)))      .group_by(Exception_.store).all()

    df = pd.DataFrame(
        [{"store": s, "count": int(n or 0), "value": float(v or 0)} for s, n, v in rows]
    )
    if df.empty:
        return df
    return df.sort_values("value", ascending=False).head(limit).reset_index(drop=True)


def daily_trend(session, run_id, date_from=None, date_to=None, store=None) -> pd.DataFrame:
    """Match rate and unmatched value per business date, so one bad day in a
    month is visible at a glance."""
    rows = _apply_bill_filters(
        session.query(
            R1BillLine.business_date,
            func.count(R1BillLine.id),
            func.sum(case((R1BillLine.matched.is_(True), 1), else_=0)),
            func.sum(case((R1BillLine.matched.is_(False), R1BillLine.amount), else_=0)),
            func.sum(R1BillLine.amount),
        ),
        run_id, date_from, date_to, store,
    ).group_by(R1BillLine.business_date).all()

    records = []
    for business_date, lines, matched, unmatched_value, value in rows:
        lines = int(lines or 0)
        records.append({
            "business_date": business_date,
            "bill_lines": lines,
            "matched": int(matched or 0),
            "match_rate": (float(matched or 0) / lines * 100) if lines else 0.0,
            "unmatched_value": float(unmatched_value or 0),
            "digital_value": float(value or 0),
        })
    df = pd.DataFrame(records)
    return df.sort_values("business_date").reset_index(drop=True) if not df.empty else df


def control_totals(run) -> pd.DataFrame:
    """Rows read and control total per source — a data-integrity check, not a
    headline."""
    notes = json.loads(run.notes) if run and run.notes else {}
    load = notes.get("load", {})
    if not load:
        return pd.DataFrame()
    return pd.DataFrame([
        {"source": source, "status": info.get("status"),
         "rows_read": info.get("rows_read"), "control_total": info.get("control_total")}
        for source, info in load.items()
    ])


def run_timings(run) -> pd.DataFrame:
    notes = json.loads(run.notes) if run and run.notes else {}
    timings = notes.get("timings", {})
    if not timings:
        return pd.DataFrame()
    total = sum(timings.values()) or 1
    return pd.DataFrame([
        {"stage": stage, "seconds": secs, "share": secs / total * 100}
        for stage, secs in sorted(timings.items(), key=lambda kv: -kv[1])
    ])


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

def _run_dates(session) -> dict:
    """run_id -> the date that run happened, so first/last seen can show a date
    instead of a run number."""
    return {r.id: r.started_at for r in session.query(Run).all()}


def unmatched_bill_facts(session, run_id: int, bill_nos=None) -> dict:
    """{(business_date, store, bill_no, mop): (bill_time, created_by)} for
    every UNMATCHED bill line of this run — the bounded, indexed population
    (ix_r1_matched_date) an exception-bills view actually needs, never the
    whole month. bill_time/created_by are real R1BillLine columns
    (2026-09-29); Exception_ itself does not carry them — a bill exception's
    business_date/store come from the row itself and bill_no/mop from its
    own `detail`, so this is the join that puts the three back together for
    display, without duplicating them into a JSON blob."""
    out = {}
    query = (session.query(R1BillLine.business_date, R1BillLine.store,
                           R1BillLine.bill_no, R1BillLine.mop,
                           R1BillLine.bill_time, R1BillLine.created_by)
             .filter(R1BillLine.run_id == run_id, R1BillLine.matched.is_(False)))
    if bill_nos is not None:
        # Only the bills a page actually shows (bill_no is globally unique, CLAUDE.md 5).
        wanted = sorted({str(b) for b in bill_nos if b})
        if not wanted:
            return {}
        if len(wanted) <= 900:          # SQLite's bound-variable limit; a bigger set (an export chunk)
            query = query.filter(R1BillLine.bill_no.in_(wanted))   # reads the whole unmatched set instead
    rows = query.all()
    for business_date, store, bill_no, mop, bill_time, created_by in rows:
        out[(str(business_date), str(store), str(bill_no), str(mop))] = (bill_time, created_by)
    return out


def _exception_records(session, rows) -> pd.DataFrame:
    """Readable exception columns for the given Exception_ ORM rows (and only those):
    severity, dates not run numbers, the JSON `detail` parsed. Shared by the whole-list
    view and the server-side page, so the two can never disagree about a row."""
    if not rows:
        return pd.DataFrame()

    run_dates = _run_dates(session)
    now = pd.Timestamp.now()

    records = []
    for e in rows:
        detail = json.loads(e.detail) if e.detail else {}
        reference = (detail.get("bill_no") or detail.get("transaction_id") or "—")

        first_seen = run_dates.get(e.first_seen_run_id)
        last_seen = run_dates.get(e.last_seen_run_id)
        age_days = (now - pd.Timestamp(first_seen)).days if first_seen else None

        amount = float(e.amount) if e.amount is not None else 0.0
        records.append({
            "severity": severity_for(e.type, amount),
            "date": e.business_date,
            "store": e.store if e.store not in (None, "None") else "—",
            "type": e.type,
            "reference": reference,
            "amount": amount,
            "age_days": age_days,
            "status": e.status,
            "first_seen": first_seen.date().isoformat() if first_seen else None,
            "last_seen": last_seen.date().isoformat() if last_seen else None,
            "exception_key": e.exception_key,      # identity, not for display
            "detail": detail,
            # A real column (2026-09-29), only ever set for
            # unmapped_gateway_transaction rows; None for a bill exception
            # and None for Axis (no real time exists in that gateway's own
            # file — see r1_bill_txn.build_gateway_pool's own note).
            "transaction_time": e.transaction_time,
        })
    return pd.DataFrame(records)


def exception_rows(session, date_from=None, date_to=None, store=None,
                   open_only=True, types=None, search: str = None) -> pd.DataFrame:
    """The working list: readable columns, severity, dates not run numbers.

    Sorted by value descending — the biggest number is the first thing seen.
    The composite key stays as the stable identity but is not a display column.
    Loads EVERY matching row - use `exception_page` for anything a person scrolls.
    """
    query = _apply_exception_filters(session.query(Exception_), date_from, date_to,
                                     store, open_only)
    if types:
        query = query.filter(Exception_.type.in_(types))

    df = _exception_records(session, query.all())
    if df.empty:
        return df

    if search:
        needle = search.strip().lower()
        mask = (df["reference"].astype(str).str.lower().str.contains(needle, na=False)
                | df["store"].astype(str).str.lower().str.contains(needle, na=False)
                | df["exception_key"].astype(str).str.lower().str.contains(needle, na=False))
        df = df[mask]

    return df.sort_values("amount", ascending=False).reset_index(drop=True)


# Server-side sort choices for an exception page: label -> ordered SQL columns. The id is
# always the last key, so a page boundary can never repeat or skip a row.
EXCEPTION_SORTS = {
    "Amount (high to low)": lambda: (Exception_.amount.desc(), Exception_.id.asc()),
    "Amount (low to high)": lambda: (Exception_.amount.asc(), Exception_.id.asc()),
    "Date (oldest first)": lambda: (Exception_.business_date.asc(), Exception_.store.asc(),
                                    Exception_.id.asc()),
    "Date (newest first)": lambda: (Exception_.business_date.desc(), Exception_.store.asc(),
                                    Exception_.id.asc()),
    "Store": lambda: (Exception_.store.asc(), Exception_.business_date.asc(), Exception_.id.asc()),
}
DEFAULT_EXCEPTION_SORT = "Amount (high to low)"


def _exception_page_query(session, date_from, date_to, store, types, statuses):
    query = _apply_exception_filters(session.query(Exception_), date_from, date_to, store,
                                     open_only=False)
    query = query.filter(Exception_.type.in_(types))
    if statuses:
        query = query.filter(Exception_.status.in_(statuses))
    return query


def exception_page(session, date_from, date_to, store, types, statuses=("open",),
                   limit: int = 50, offset: int = 0, sort: str = DEFAULT_EXCEPTION_SORT):
    """ONE PAGE of exceptions, filtered and sorted in the database. Returns
    (frame_for_this_page_only, total_rows, total_value) - the count and value are SQL
    aggregates over the whole filtered population, never a length of the page. Only the
    page's rows have their JSON parsed; nothing else is read."""
    base = _exception_page_query(session, date_from, date_to, store, types, statuses)
    n, v = (_exception_page_query(session, date_from, date_to, store, types, statuses)
            .with_entities(func.count(Exception_.id), func.sum(Exception_.amount)).one())
    order = EXCEPTION_SORTS.get(sort, EXCEPTION_SORTS[DEFAULT_EXCEPTION_SORT])()
    rows = base.order_by(*order).limit(limit).offset(offset).all()
    return _exception_records(session, rows), int(n or 0), float(v or 0.0)


def exception_export(session, date_from, date_to, store, types, statuses=("open",),
                     sort: str = DEFAULT_EXCEPTION_SORT, progress=None, transform=None) -> pd.DataFrame:
    """EVERY exception in the filtered population - never the page on screen - read in
    chunks of EXPORT_CHUNK with progress, so a large export never holds one giant query
    result. `transform(page_frame) -> frame` (optional) shapes each chunk exactly as the
    screen shapes its page, so the file and the screen cannot disagree."""
    base = _exception_page_query(session, date_from, date_to, store, types, statuses)
    total = base.with_entities(func.count(Exception_.id)).scalar() or 0
    order = EXCEPTION_SORTS.get(sort, EXCEPTION_SORTS[DEFAULT_EXCEPTION_SORT])()
    frames, done = [], 0
    while done < total:
        rows = base.order_by(*order).limit(EXPORT_CHUNK).offset(done).all()
        if not rows:
            break
        frame = _exception_records(session, rows)
        frames.append(transform(frame) if transform else frame)
        done += len(rows)
        if progress:
            progress(done, total)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def exception_counts(session, date_from, date_to, store, bill_types, txn_types,
                     statuses=("open",)) -> tuple:
    """Count + value for the bill-type and transaction-type exception
    buckets, one aggregate SQL query per bucket — never the full per-row
    frame `exception_rows()` builds (JSON-parsed detail, severity, age for
    every row). For Summary's Exceptions section header, which must show a
    row count even while the section is collapsed and its table is not
    being rendered (§6.14: a header count is cheap; a full detail-plus-
    remarks build is not, and only happens when the section is actually
    open).

    Returns (bill_count, bill_value, txn_count, txn_value).
    """
    def _agg(types):
        query = _apply_exception_filters(
            session.query(func.count(Exception_.id), func.sum(Exception_.amount)),
            date_from, date_to, store, open_only=False)
        query = query.filter(Exception_.type.in_(types),
                             Exception_.status.in_(statuses))
        n, v = query.one()
        return int(n or 0), float(v or 0.0)

    bill_n, bill_v = _agg(bill_types)
    txn_n, txn_v = _agg(txn_types)
    return bill_n, bill_v, txn_n, txn_v


def remark_history(session, exception_key: str) -> pd.DataFrame:
    row = session.query(Exception_).filter_by(exception_key=exception_key).one_or_none()
    if row is None:
        return pd.DataFrame()
    remarks = (session.query(Remark).filter_by(exception_id=row.id)
               .order_by(Remark.created_at.asc()).all())
    return pd.DataFrame([
        {"when": r.created_at, "who": r.created_by, "remark": r.remark_text}
        for r in remarks
    ])


def latest_remarks(session, exception_keys) -> dict:
    """{exception_key: (remark_text, created_by)} for the LATEST remark on
    each of the given exception keys — one bulk query, never one per row
    (§6.14: never pull a page's remarks with N+1 queries).

    Keys with no remark at all are simply absent from the returned dict.
    """
    keys = [k for k in set(exception_keys) if k]
    if not keys:
        return {}
    rows = (session.query(Exception_.exception_key, Remark.created_by,
                          Remark.remark_text, Remark.created_at)
           .join(Remark, Remark.exception_id == Exception_.id)
           .filter(Exception_.exception_key.in_(keys))
           .order_by(Remark.created_at.asc()).all())
    latest = {}
    for exception_key, created_by, remark_text, _created_at in rows:
        latest[exception_key] = (remark_text, created_by)  # last write wins — ascending order
    return latest


def latest_remarks_full(session, exception_keys) -> dict:
    """Like `latest_remarks()`, but also carries the timestamp —
    {exception_key: (remark_text, created_by, created_at)}. A separate
    function rather than changing `latest_remarks()`'s own return shape,
    since Manual matches already depends on that 2-tuple (2026-09-30, added
    for Summary's own editable remark columns, which need remark_at shown
    alongside remark_by)."""
    keys = [k for k in set(exception_keys) if k]
    if not keys:
        return {}
    rows = (session.query(Exception_.exception_key, Remark.created_by,
                          Remark.remark_text, Remark.created_at)
           .join(Remark, Remark.exception_id == Exception_.id)
           .filter(Exception_.exception_key.in_(keys))
           .order_by(Remark.created_at.asc()).all())
    latest = {}
    for exception_key, created_by, remark_text, created_at in rows:
        latest[exception_key] = (remark_text, created_by, created_at)
    return latest


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def _transaction_query(session, run_id, date_from=None, date_to=None, store=None,
                       status=None, mop=None, matched=None, search: str = None):
    """The filtered bill-line query. Shared by the paged view and the export so
    the export can never see a different population from the screen."""
    base = _apply_bill_filters(session.query(R1BillLine), run_id, date_from, date_to, store)

    if status:
        base = base.filter(R1BillLine.status == status)
    if mop:
        base = base.filter(R1BillLine.mop == mop)
    if matched is not None:
        base = base.filter(R1BillLine.matched.is_(matched))
    if search:
        needle = f"%{search.strip()}%"
        base = base.filter(
            R1BillLine.bill_no.like(needle)
            | R1BillLine.matched_txn_ref.like(needle)
            | R1BillLine.paytm_ref.like(needle)
            | R1BillLine.axis_ref.like(needle)
            | R1BillLine.bajaj_ref.like(needle)
        )
    return base.order_by(R1BillLine.business_date, R1BillLine.store, R1BillLine.bill_no)


def _bill_line_frame(rows) -> pd.DataFrame:
    return pd.DataFrame([{
        "date": r.business_date,
        "store": r.store,
        "bill_no": r.bill_no,
        "mop": r.mop,
        "amount": float(r.amount) if r.amount is not None else 0.0,
        "status": r.status,
        "tier": r.match_tier,
        "gateway": r.matched_gateway,
        "matched_txn": r.matched_txn_ref,
        "paytm_ref": r.paytm_ref,
        "axis_ref": r.axis_ref,
        "bajaj_ref": r.bajaj_ref,
        "line": r.line_seq,
    } for r in rows])


def transaction_page(session, run_id, date_from=None, date_to=None, store=None,
                     status=None, mop=None, matched=None, search: str = None,
                     limit: int = 50, offset: int = 0):
    """One page of bill lines, plus the total count for 'showing X-Y of N'.

    Search finds a bill number, transaction id or RRN directly, so locating one
    bill never means paging.
    """
    query = _transaction_query(session, run_id, date_from, date_to, store,
                               status, mop, matched, search)
    total = query.count()
    rows = query.limit(limit).offset(offset).all()
    return _bill_line_frame(rows), total


# Chunk size for the export. Large enough that a month is a handful of round
# trips, small enough that progress moves visibly and memory stays bounded.
EXPORT_CHUNK = 25_000


def transaction_export(session, run_id, date_from=None, date_to=None, store=None,
                       status=None, mop=None, matched=None, search: str = None,
                       progress=None) -> pd.DataFrame:
    """EVERY row matching the filters, not a page.

    Read in chunks so `progress(rows_done, total)` can move while a large
    export is assembled — the page must never look frozen.
    """
    query = _transaction_query(session, run_id, date_from, date_to, store,
                               status, mop, matched, search)
    total = query.count()
    frames, done = [], 0
    while done < total:
        rows = query.limit(EXPORT_CHUNK).offset(done).all()
        if not rows:
            break
        frames.append(_bill_line_frame(rows))
        done += len(rows)
        if progress:
            progress(done, total)
    if not frames:
        return _bill_line_frame([])
    return pd.concat(frames, ignore_index=True)


def status_options(session, run_id) -> list:
    return [s[0] for s in session.query(R1BillLine.status)
            .filter(R1BillLine.run_id == run_id).distinct().all() if s[0]]


def mop_options(session, run_id) -> list:
    return [m[0] for m in session.query(R1BillLine.mop)
            .filter(R1BillLine.run_id == run_id).distinct().all() if m[0]]


# ---------------------------------------------------------------------------
# Date-lag matches — a Tier 1 reference match whose transaction was dated a
# different day than the bill (2026-09-13). Information, never an exception:
# the money is received. See r1_bill_txn.match_tier1 / CLAUDE.md §6.3.
# ---------------------------------------------------------------------------

def date_lag_matches(session, run_id, date_from=None, date_to=None,
                     store=None) -> pd.DataFrame:
    """Every matched bill whose transaction was dated a different day than
    the bill — bill_no, bill date, the transaction's own date (derived from
    date_diff_days, never re-guessed), the lag in days, amount and gateway.
    Sorted by the size of the lag, biggest first.
    """
    rows = _apply_bill_filters(
        session.query(R1BillLine), run_id, date_from, date_to, store,
    ).filter(R1BillLine.matched.is_(True),
             R1BillLine.date_diff_days.isnot(None),
             R1BillLine.date_diff_days != 0).all()

    if not rows:
        return pd.DataFrame(columns=["bill_no", "business_date", "txn_date",
                                     "lag_days", "amount", "gateway", "match_note"])

    records = []
    for r in rows:
        bill_date = date.fromisoformat(str(r.business_date))
        lag = int(r.date_diff_days)
        records.append({
            "bill_no": r.bill_no,
            "business_date": r.business_date,
            "txn_date": (bill_date + timedelta(days=lag)).isoformat(),
            "lag_days": lag,
            "amount": float(r.amount or 0),
            "gateway": r.matched_gateway,
            "match_note": r.match_note,
        })
    df = pd.DataFrame(records)
    return df.sort_values("lag_days", key=abs, ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Bill match — the bill side, overlay-aware (2026-09-12)
#
# R1BillLine.matched is the ENGINE's own verdict only — the manual-match
# overlay never writes to it (§6.9b: "the engine runs untouched"). So a bill a
# person matched manually still reads matched=False here. On the Bill match
# page, "Closed" means "the engine matched it OR an active manual match closes
# it" — checked by BILL NUMBER, which CLAUDE.md §5 confirms is globally
# unique, against the small set of currently-active manual matches. This never
# pulls a whole date range of bill lines into Python to compute it — only the
# (typically hundreds, not hundreds of thousands of) active manual matches.
# ---------------------------------------------------------------------------

def _active_manual_bill_nos(session, date_from=None, date_to=None, store=None) -> set:
    query = session.query(ManualMatch.bill_no).filter(ManualMatch.lifecycle == "active")
    if date_from is not None:
        query = query.filter(ManualMatch.business_date >= str(date_from))
    if date_to is not None:
        query = query.filter(ManualMatch.business_date <= str(date_to))
    if store:
        query = query.filter(ManualMatch.store == store)
    return {b for (b,) in query.all() if b}


def _apply_bill_match_scope(query, manual_bill_nos: set, show: str):
    """`show` is 'open' | 'closed' | 'all'."""
    if show == "open":
        query = query.filter(R1BillLine.matched.is_(False))
        if manual_bill_nos:
            query = query.filter(~R1BillLine.bill_no.in_(manual_bill_nos))
    elif show == "closed":
        if manual_bill_nos:
            query = query.filter(or_(R1BillLine.matched.is_(True),
                                     R1BillLine.bill_no.in_(manual_bill_nos)))
        else:
            query = query.filter(R1BillLine.matched.is_(True))
    return query


def _bill_match_summary_from_results(session, run_id, date_from, date_to, store, manual_bill_nos):
    """`bill_match_summary` from the run's persisted per-(date, store) R1 results - a few
    thousand small rows summed in SQL - instead of counting and summing 3.7M bill lines
    (12.7 s for five months). The engine's matched/total are in each result's payload; a bill
    a person matched but the engine did not is added from the (few) manual matches, exactly as
    the line-level query's `matched OR bill_no IN manual` does. Returns None when the run has
    no persisted results (an older run, a test) so the caller falls back to the line query."""
    base = session.query(ReconciliationResult).filter(
        ReconciliationResult.run_id == run_id, ReconciliationResult.reconciliation == "R1")
    if date_from is not None:
        base = base.filter(ReconciliationResult.business_date >= str(date_from))
    if date_to is not None:
        base = base.filter(ReconciliationResult.business_date <= str(date_to))
    if store:
        base = base.filter(ReconciliationResult.store == store)
    if session.query(ReconciliationResult.id).filter(
            ReconciliationResult.run_id == run_id,
            ReconciliationResult.reconciliation == "R1").first() is None:
        return None

    def jsum(field):
        return func.sum(func.json_extract(ReconciliationResult.payload, f"$.{field}"))

    n, v, mn, mv = base.with_entities(
        jsum("digital_bill_lines"), jsum("digital_value"), jsum("matched"), jsum("matched_value")).one()
    total_n, total_v = int(n or 0), float(v or 0)
    closed_n, closed_v = int(mn or 0), float(mv or 0)

    if manual_bill_nos:
        extra_n, extra_v = _apply_bill_filters(
            session.query(func.count(R1BillLine.id), func.sum(R1BillLine.amount)),
            run_id, date_from, date_to, store,
        ).filter(R1BillLine.matched.is_(False), R1BillLine.bill_no.in_(manual_bill_nos)).one()
        closed_n += int(extra_n or 0)
        closed_v += float(extra_v or 0)

    return {
        "open": {"count": total_n - closed_n, "value": total_v - closed_v},
        "closed": {"count": closed_n, "value": closed_v},
        "total": {"count": total_n, "value": total_v},
    }


def bill_match_summary(session, run_id, date_from=None, date_to=None,
                       store=None) -> dict:
    """Open/Closed counts and value, bucketed exactly as the row list is —
    a manually-matched bill counts as closed even though the engine's own
    `matched` column still reads False for it."""
    manual_bill_nos = _active_manual_bill_nos(session, date_from, date_to, store)

    fast = _bill_match_summary_from_results(session, run_id, date_from, date_to, store,
                                            manual_bill_nos)
    if fast is not None:
        return fast

    total_n, total_v = _apply_bill_filters(
        session.query(func.count(R1BillLine.id), func.sum(R1BillLine.amount)),
        run_id, date_from, date_to, store,
    ).one()
    total_n, total_v = int(total_n or 0), float(total_v or 0)

    closed_n, closed_v = _apply_bill_match_scope(
        _apply_bill_filters(
            session.query(func.count(R1BillLine.id), func.sum(R1BillLine.amount)),
            run_id, date_from, date_to, store),
        manual_bill_nos, "closed",
    ).one()
    closed_n, closed_v = int(closed_n or 0), float(closed_v or 0)

    return {
        "open": {"count": total_n - closed_n, "value": total_v - closed_v},
        "closed": {"count": closed_n, "value": closed_v},
        "total": {"count": total_n, "value": total_v},
    }


def _bill_match_query(session, run_id, date_from=None, date_to=None, store=None,
                      show: str = "open", status=None, mop=None, search: str = None):
    manual_bill_nos = _active_manual_bill_nos(session, date_from, date_to, store)
    query = _apply_bill_filters(session.query(R1BillLine), run_id, date_from, date_to, store)
    query = _apply_bill_match_scope(query, manual_bill_nos, show)
    if status:
        query = query.filter(R1BillLine.status == status)
    if mop:
        query = query.filter(R1BillLine.mop == mop)
    if search:
        needle = f"%{search.strip()}%"
        query = query.filter(
            R1BillLine.bill_no.like(needle)
            | R1BillLine.matched_txn_ref.like(needle)
            | R1BillLine.paytm_ref.like(needle)
            | R1BillLine.axis_ref.like(needle)
            | R1BillLine.bajaj_ref.like(needle)
        )
    if show == "open":
        # The open bills are a few thousand of millions of lines. With a plain ORDER BY the
        # planner walks the page index (ix_r1_page) in order, filtering matched=0 as it goes - the
        # whole month, 40 s for five months - when (run, matched, date) is a direct seek.
        # SQLAlchemy cannot emit SQLite's INDEXED BY, so the ordering is wrapped in a no-op
        # function: the planner can no longer be tempted by an index that merely provides the
        # order, picks the selective one, and sorts the few thousand rows it returns. Same order.
        return query.order_by(func.coalesce(R1BillLine.business_date, ""), func.coalesce(R1BillLine.store, ""),
                              func.coalesce(R1BillLine.bill_no, ""))
    return query.order_by(R1BillLine.business_date, R1BillLine.store, R1BillLine.bill_no)


def _bill_match_frame(session, rows) -> pd.DataFrame:
    """Same shape as _bill_line_frame, plus which side closed the bill — the
    system, a person, or neither (still open)."""
    bill_nos = [r.bill_no for r in rows if r.bill_no]
    manual_by_bill_no = {}
    if bill_nos:
        for m in (session.query(ManualMatch)
                  .filter(ManualMatch.lifecycle == "active",
                          ManualMatch.bill_no.in_(bill_nos)).all()):
            manual_by_bill_no[m.bill_no] = m

    records = []
    for r in rows:
        manual = manual_by_bill_no.get(r.bill_no)
        records.append({
            "date": r.business_date,
            "store": r.store,
            "bill_no": r.bill_no,
            "mop": r.mop,
            "amount": float(r.amount) if r.amount is not None else 0.0,
            "status": r.status,
            "closed": bool(r.matched) or manual is not None,
            "closed_by": ("system" if r.matched else ("manual" if manual else None)),
            "gateway": r.matched_gateway or (manual.gateway if manual else None),
            "matched_txn": r.matched_txn_ref or (manual.transaction_ref if manual else None),
            "paytm_ref": r.paytm_ref,
            "axis_ref": r.axis_ref,
            "bajaj_ref": r.bajaj_ref,
            "line": r.line_seq,
        })
    return pd.DataFrame(records)


def _fast_count(query) -> int:
    """COUNT over the same filtered query - without the wrapping subquery `Query.count()` builds,
    which drops the index hint and made the open-bills count walk the whole month (18 s)."""
    from sqlalchemy import func
    return int(query.order_by(None).with_entities(func.count(R1BillLine.id)).scalar() or 0)


def bill_match_page(session, run_id, date_from=None, date_to=None, store=None,
                    show: str = "open", status=None, mop=None, search: str = None,
                    limit: int = 50, offset: int = 0):
    """One page of bills, Bill-match style — Open/Closed/All, overlay-aware."""
    query = _bill_match_query(session, run_id, date_from, date_to, store, show,
                              status, mop, search)
    total = _fast_count(query)
    rows = query.limit(limit).offset(offset).all()
    return _bill_match_frame(session, rows), total


def bill_match_export(session, run_id, date_from=None, date_to=None, store=None,
                      show: str = "open", status=None, mop=None, search: str = None,
                      progress=None) -> pd.DataFrame:
    """EVERY bill matching the filters and the Open/Closed/All scope."""
    query = _bill_match_query(session, run_id, date_from, date_to, store, show,
                              status, mop, search)
    total = _fast_count(query)
    frames, done = [], 0
    while done < total:
        rows = query.limit(EXPORT_CHUNK).offset(done).all()
        if not rows:
            break
        frames.append(_bill_match_frame(session, rows))
        done += len(rows)
        if progress:
            progress(done, total)
    if not frames:
        return _bill_match_frame(session, [])
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Matched bills — Summary's own "Matched" section (2026-09-19)
# ---------------------------------------------------------------------------

def _matched_bill_records(session, rows) -> pd.DataFrame:
    """Matched-bill rows in the exact shape Summary's Matched section shows:
    date, store, bill_no, mop, amount, gateway, transaction_id, txn_date,
    lag, match_source.

    `lag` is `txn_date - bill_date` in days, 0 when they agree. Tier 1 reads
    it straight from `date_diff_days` (§6.3, populated since the global
    reference search went date-agnostic); Tier 2 is same-date by
    construction (its own candidate pool is filtered on `txn date ==
    business_date`); a manual match's own validation recorded its date lag
    at decision time in `validation_detail` (JSON) — read back here rather
    than assumed to be 0, which rule 5 forbids guessing.
    """
    bill_nos = [r.bill_no for r in rows if r.bill_no]
    manual_by_bill_no = {}
    if bill_nos:
        for m in (session.query(ManualMatch)
                  .filter(ManualMatch.lifecycle == "active",
                          ManualMatch.bill_no.in_(bill_nos)).all()):
            manual_by_bill_no[m.bill_no] = m

    records = []
    for r in rows:
        manual = manual_by_bill_no.get(r.bill_no)
        bill_date = date.fromisoformat(r.business_date)
        if manual is not None:
            match_source = "manual"
            gateway = manual.gateway
            transaction_id = manual.transaction_ref
            lag = 0
            if manual.validation_detail:
                try:
                    lag = int(json.loads(manual.validation_detail).get("date_lag_days") or 0)
                except (ValueError, TypeError):
                    lag = 0
        elif r.matched:
            gateway = r.matched_gateway
            transaction_id = r.matched_txn_ref
            lag = int(r.date_diff_days or 0)
            match_source = "tier2" if r.match_tier == "Matched (EDC OTHER)" else "tier1"
        else:
            continue  # scope is always "closed" here — should never happen
        records.append({
            "date": r.business_date, "store": r.store, "bill_no": r.bill_no,
            "mop": r.mop, "amount": float(r.amount) if r.amount is not None else 0.0,
            "gateway": (gateway or "—"), "transaction_id": (transaction_id or "—"),
            "txn_date": (bill_date + timedelta(days=lag)).isoformat(),
            "lag": lag, "match_source": match_source,
        })
    columns = ["date", "store", "bill_no", "mop", "amount", "gateway",
              "transaction_id", "txn_date", "lag", "match_source"]
    return pd.DataFrame(records, columns=columns)


def matched_bills_page(session, run_id, date_from=None, date_to=None, store=None,
                       limit: int = 50, offset: int = 0):
    """One page of CLOSED bills, in Summary's Matched-section shape. Sorted
    date then store by default — `st.dataframe`'s own click-to-sort handles
    anything else the operator wants."""
    manual_bill_nos = _active_manual_bill_nos(session, date_from, date_to, store)
    query = _apply_bill_filters(session.query(R1BillLine), run_id, date_from, date_to, store)
    query = _apply_bill_match_scope(query, manual_bill_nos, "closed")
    total = query.count()
    rows = (query.order_by(R1BillLine.business_date, R1BillLine.store, R1BillLine.bill_no)
           .limit(limit).offset(offset).all())
    return _matched_bill_records(session, rows), total


def matched_bills_export(session, run_id, date_from=None, date_to=None, store=None,
                         progress=None) -> pd.DataFrame:
    """EVERY matched bill in the filtered range, not a page."""
    manual_bill_nos = _active_manual_bill_nos(session, date_from, date_to, store)
    query = _apply_bill_filters(session.query(R1BillLine), run_id, date_from, date_to, store)
    query = _apply_bill_match_scope(query, manual_bill_nos, "closed")
    query = query.order_by(R1BillLine.business_date, R1BillLine.store, R1BillLine.bill_no)
    total = query.count()
    frames, done = [], 0
    while done < total:
        rows = query.limit(EXPORT_CHUNK).offset(done).all()
        if not rows:
            break
        frames.append(_matched_bill_records(session, rows))
        done += len(rows)
        if progress:
            progress(done, total)
    if not frames:
        return _matched_bill_records(session, [])
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

#  A `running` row whose heartbeat is older than this is shown as STALLED
#  rather than trusted at face value (2026-09-24 — a 5-month run sat silent
#  for 14+ minutes, indistinguishable from hung, with nothing on screen
#  saying so). Must match `runs.STALE_RUN_SECONDS` exactly (2026-09-24,
#  unified to 5 minutes) — the two constants live in separate modules only
#  because `queries.py` must not import `runs.py`, never because they are
#  allowed to disagree about what "stalled" means.
STALLED_AFTER_SECONDS = 300


def run_history(session, limit: int = 20) -> pd.DataFrame:
    runs = session.query(Run).order_by(Run.started_at.desc()).limit(limit).all()
    records = []
    now = datetime.now()
    for r in runs:
        # `notes` is JSON on every successful-path write, but a run that
        # failed via a raw exception before this was fixed could still carry
        # a plain string (`"{ExceptionType}: msg"`) — parsed defensively so
        # ONE malformed row can never break this whole page, the same
        # try/except shape dashboard_common.run_notes() already uses.
        try:
            notes = json.loads(r.notes) if r.notes else {}
            if not isinstance(notes, dict):
                notes = {}
        except (ValueError, TypeError):
            notes = {"failure_reason": r.notes}
        r1 = notes.get("r1", {})
        dates = json.loads(r.business_dates) if r.business_dates else [None, None]

        display_status = r.status
        stalled = False
        if r.status == "running":
            age = (now - r.heartbeat_at).total_seconds() if r.heartbeat_at else None
            if age is None or age > STALLED_AFTER_SECONDS:
                display_status = "stalled"
                stalled = True

        records.append({
            "run": r.id,
            "started": r.started_at,
            "range": f"{dates[0] or '—'} .. {dates[1] or '—'}",
            "status": display_status,
            "stalled": stalled,
            "current_stage": r.current_stage,
            "progress_detail": r.progress_detail,
            "heartbeat_at": r.heartbeat_at,
            "heartbeat_age_s": ((now - r.heartbeat_at).total_seconds()
                                 if r.heartbeat_at else None),
            "failure_reason": notes.get("failure_reason"),
            "duration_s": ((r.finished_at - r.started_at).total_seconds()
                           if r.finished_at else
                           (now - r.started_at).total_seconds() if r.status == "running" else None),
            "bill_lines": r1.get("digital_bill_lines"),
            "matched": r1.get("matched_total"),
            "by": r.triggered_by,
        })
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# EDC OTHER support
#
# An EDC OTHER bill carries no gateway reference, so Tier 2 matches it on
# store + business date + amount against what Tier 1 did not consume. When more
# than one candidate survives that, the bill is Ambiguous and a person decides
# — time never auto-matches (CLAUDE.md 6.3).
#
# These are read-side helpers only: they explain an existing outcome, they do
# not match anything.
# ---------------------------------------------------------------------------

def edc_other_collision(session, run_id, business_date, store, amount) -> pd.DataFrame:
    """The other bill lines sharing this store + date + amount.

    This is *why* a case is ambiguous: the key Tier 2 has to work with is not
    unique here. Showing the colliding bills tells the operator what they are
    actually choosing between.
    """
    rows = (session.query(R1BillLine)
            .filter(R1BillLine.run_id == run_id,
                    R1BillLine.business_date == str(business_date),
                    R1BillLine.store == store,
                    R1BillLine.amount == amount)
            .order_by(R1BillLine.bill_no)
            .all())
    return pd.DataFrame([{
        "bill_no": r.bill_no,
        "line": r.line_seq,
        "mop": r.mop,
        "amount": float(r.amount) if r.amount is not None else 0.0,
        "status": r.status,
        "tier": r.match_tier or "—",
        "matched_txn": r.matched_txn_ref or "—",
    } for r in rows])


def edc_other_summary(session, run_id, date_from=None, date_to=None, store=None) -> dict:
    """How the EDC OTHER population came out — the numbers behind the group."""
    # ONE aggregate, not four counts. Four separate count() calls each scanned
    # the month: measured 18.2s on 660k rows, which is not a page load. Rolled
    # into a single pass with conditional sums instead.
    total, matched, ambiguous, unmatched, value = _apply_bill_filters(
        session.query(
            func.count(R1BillLine.id),
            func.sum(case((R1BillLine.match_tier == "Matched (EDC OTHER)", 1), else_=0)),
            func.sum(case((R1BillLine.match_tier == "Ambiguous", 1), else_=0)),
            func.sum(case(((R1BillLine.matched.is_(False))
                           & (R1BillLine.match_tier.is_(None)), 1), else_=0)),
            func.sum(R1BillLine.amount),
        ),
        run_id, date_from, date_to, store,
    ).filter(R1BillLine.mop.in_(("EDC OTHER", "EDC OTHER AXIS"))).one()

    total = total or 0
    matched = matched or 0
    ambiguous = ambiguous or 0
    unmatched = unmatched or 0
    value = value or 0.0

    return {
        "bill_lines": int(total),
        "matched_on_residual": int(matched),
        "ambiguous": int(ambiguous),
        "no_candidate": int(unmatched),
        "value": float(value),
    }


# ---------------------------------------------------------------------------
# Manual matches page rebuild (2026-09-20) — By transaction, the 3-line
# Mapped/Refunded/Unmapped transaction summary, and the Manual match bill
# report. "By bill" mode reuses bill_match_page/_export (show="open") above
# unchanged — the columns it already returns (date, store, bill_no, mop,
# amount, status, matched_txn) are exactly what that mode needs.
# ---------------------------------------------------------------------------

_UNMAPPED_TXN_COLUMNS = ["gateway", "transaction_id", "date", "store", "amount",
                        "payment_mode", "matched_bill_no", "resolution"]


def unmapped_transactions_frame(session, date_from=None, date_to=None,
                                store=None, warehouse=None) -> pd.DataFrame:
    """Every UNMAPPED gateway transaction in range — "By transaction" mode's
    table. Bounded to the filtered date range/store, same as the old
    Transactions page (CLAUDE.md 6.14) — reads per-date warehouse Parquet
    only, never a raw file and never the whole warehouse."""
    from . import gateway_txns as gt

    if date_from is None or date_to is None:
        return pd.DataFrame(columns=_UNMAPPED_TXN_COLUMNS)

    frame = gt.gateway_transactions_with_resolution(session, date_from, date_to,
                                                     warehouse=warehouse)
    if frame.empty:
        return pd.DataFrame(columns=_UNMAPPED_TXN_COLUMNS)
    if store:
        frame = frame[frame["store"] == store]
    frame = frame[frame["resolution"] == "unmapped"]
    out = frame.rename(columns={"txn_ref": "transaction_id", "business_date": "date"})
    out["matched_bill_no"] = out["matched_bill_no"]
    return out[_UNMAPPED_TXN_COLUMNS].sort_values(["date", "store"]).reset_index(drop=True)


def unmapped_transactions_page(session, date_from=None, date_to=None, store=None,
                               limit: int = 50, offset: int = 0, warehouse=None):
    frame = unmapped_transactions_frame(session, date_from, date_to, store, warehouse)
    total = len(frame)
    return frame.iloc[offset:offset + limit].reset_index(drop=True), total


def unmapped_transactions_export(session, date_from=None, date_to=None, store=None,
                                 warehouse=None, progress=None) -> pd.DataFrame:
    """EVERY unmapped transaction matching the filter — not a page."""
    return unmapped_transactions_frame(session, date_from, date_to, store, warehouse)


@dataclass
class TransactionResolution:
    """The 3-line Mapped/Refunded/Unmapped transaction summary. `refunded_*`
    is the WHOLE refunded population (pending + released) — never split
    across this line and "unmapped"; `refund_pending_*` /
    `refund_released_*` are the sub-counts within it, always shown together,
    never merged into one figure (the operator's own instruction)."""
    mapped_n: int = 0
    mapped_v: float = 0.0
    refunded_n: int = 0
    refunded_v: float = 0.0
    refund_pending_n: int = 0
    refund_pending_v: float = 0.0
    refund_released_n: int = 0
    refund_released_v: float = 0.0
    unmapped_n: int = 0
    unmapped_v: float = 0.0
    total_n: int = 0
    total_v: float = 0.0

    @property
    def ok(self) -> bool:
        return self.mapped_n + self.refunded_n + self.unmapped_n == self.total_n

    @property
    def difference(self) -> int:
        return self.total_n - (self.mapped_n + self.refunded_n + self.unmapped_n)


def _manual_txn_facts(session) -> list:
    """[(gateway, business_date, store, amount)] of the TRANSACTION each ACTIVE, accepted manual
    match closed - read from the validation snapshot taken when it was recorded. An asserted-but-
    unverified match names no real transaction and is not here."""
    out = []
    for m in (session.query(ManualMatch)
              .filter(ManualMatch.lifecycle == "active", ManualMatch.status == "matched_manual").all()):
        try:
            txn = (json.loads(m.validation_detail) if m.validation_detail else {}).get("txn") or {}
        except ValueError:
            continue
        if txn.get("gateway") and txn.get("business_date"):
            out.append((str(txn["gateway"]).lower(), str(txn["business_date"]), txn.get("store"),
                        float(txn.get("amount") or 0.0)))
    return out


def _transaction_resolution_from_agg(session, run_id, date_from, date_to, store):
    """The Mapped / Refunded / Unmapped strip from the run's persisted `txn_day_agg`, with the
    live things applied on top - never the 3.7M-row pool. A manual match moves one transaction
    from unmapped to mapped; a customer refund moves one from unmapped to refunded. Returns None
    when the run has no aggregate (an older run) so the caller falls back to the pool read."""
    from .db import TransactionRefund, TxnDayAgg

    if session.query(TxnDayAgg.id).filter(TxnDayAgg.run_id == run_id).first() is None:
        return None
    query = session.query(func.sum(TxnDayAgg.total_n), func.sum(TxnDayAgg.total_v),
                          func.sum(TxnDayAgg.mapped_n), func.sum(TxnDayAgg.mapped_v)).filter(
        TxnDayAgg.run_id == run_id,
        TxnDayAgg.business_date >= str(date_from), TxnDayAgg.business_date <= str(date_to))
    if store:
        query = query.filter(TxnDayAgg.store == store)
    tn, tv, mn, mv = query.one()
    out = TransactionResolution()
    out.total_n, out.total_v = int(tn or 0), float(tv or 0.0)
    out.mapped_n, out.mapped_v = int(mn or 0), float(mv or 0.0)

    for _gateway, day, txn_store, amount in _manual_txn_facts(session):
        if str(date_from) <= day <= str(date_to) and (not store or txn_store == store):
            out.mapped_n += 1
            out.mapped_v += amount

    refunds = session.query(TransactionRefund).filter(
        TransactionRefund.business_date >= str(date_from), TransactionRefund.business_date <= str(date_to))
    if store:
        refunds = refunds.filter(TransactionRefund.store == store)
    for r in refunds.all():
        amount = float(r.amount or 0.0)
        out.refunded_n += 1
        out.refunded_v += amount
        if r.refund_status == "pending":
            out.refund_pending_n += 1
            out.refund_pending_v += amount
        else:
            out.refund_released_n += 1
            out.refund_released_v += amount

    out.unmapped_n = out.total_n - out.mapped_n - out.refunded_n
    out.unmapped_v = out.total_v - out.mapped_v - out.refunded_v
    return out


def transaction_resolution_summary(session, date_from=None, date_to=None,
                                   store=None, warehouse=None, run_id=None) -> TransactionResolution:
    """Mapped to bill / Refunded to customer / Unmapped (open) / Total — the
    three lines must sum to the full transaction population; asserted here,
    shown as MISMATCH by the caller if `.ok` is False."""
    from . import gateway_txns as gt

    out = TransactionResolution()
    if date_from is None or date_to is None:
        return out

    if run_id is not None and warehouse is None:
        fast = _transaction_resolution_from_agg(session, run_id, date_from, date_to, store)
        if fast is not None:
            return fast

    frame = gt.gateway_transactions_with_resolution(session, date_from, date_to,
                                                     warehouse=warehouse)
    if store:
        frame = frame[frame["store"] == store]
    if frame.empty:
        return out

    amounts = pd.to_numeric(frame["amount"], errors="coerce")
    out.total_n = len(frame)
    out.total_v = float(amounts.sum())

    mapped = frame["resolution"] == "mapped"
    out.mapped_n = int(mapped.sum())
    out.mapped_v = float(amounts[mapped].sum())

    refunded = frame["resolution"] == "refunded"
    out.refunded_n = int(refunded.sum())
    out.refunded_v = float(amounts[refunded].sum())

    pending = refunded & (frame["refund_status"] == "pending")
    out.refund_pending_n = int(pending.sum())
    out.refund_pending_v = float(amounts[pending].sum())
    released = refunded & (frame["refund_status"] != "pending")
    out.refund_released_n = int(released.sum())
    out.refund_released_v = float(amounts[released].sum())

    unmapped = frame["resolution"] == "unmapped"
    out.unmapped_n = int(unmapped.sum())
    out.unmapped_v = float(amounts[unmapped].sum())
    return out


_MANUAL_BILL_REPORT_COLUMNS = ["date", "store", "bill_no", "mop", "amount",
                               "gateway", "transaction_id", "txn_date", "lag",
                               "matched_by", "matched_at", "reason", "status"]


def manual_match_bill_report(session, date_from=None, date_to=None,
                             store=None) -> pd.DataFrame:
    """The "Manual match bill report" — one row per manual match ever
    recorded (every lifecycle state, since this is the audit trail, not a
    work queue), newest first. `lag`/`txn_date` are read back from the
    validation snapshot taken at decision time (§6.9c's `_matched_bill_records`
    does the identical thing for the engine's own matched-bills table) —
    never assumed to be 0, which rule 5 forbids guessing."""
    query = session.query(ManualMatch)
    if date_from is not None:
        query = query.filter(ManualMatch.business_date >= str(date_from))
    if date_to is not None:
        query = query.filter(ManualMatch.business_date <= str(date_to))
    if store:
        query = query.filter(ManualMatch.store == store)
    rows = query.order_by(ManualMatch.created_at.desc()).all()

    records = []
    for r in rows:
        lag = 0
        if r.validation_detail:
            try:
                lag = int(json.loads(r.validation_detail).get("date_lag_days") or 0)
            except (ValueError, TypeError):
                lag = 0
        try:
            bill_date = date.fromisoformat(str(r.business_date))
            txn_date = (bill_date + timedelta(days=lag)).isoformat()
        except (ValueError, TypeError):
            txn_date = None
        records.append({
            "date": r.business_date, "store": r.store, "bill_no": r.bill_no,
            "mop": r.mop, "amount": float(r.amount or 0), "gateway": r.gateway,
            "transaction_id": r.transaction_ref, "txn_date": txn_date, "lag": lag,
            "matched_by": r.operator, "matched_at": r.created_at,
            "reason": r.reason_code, "status": r.lifecycle,
        })
    return pd.DataFrame(records, columns=_MANUAL_BILL_REPORT_COLUMNS)
