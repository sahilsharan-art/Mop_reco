"""Manual matching — a human overlay on R1, never an edit to source data.

The engine (`r1_bill_txn`) runs untouched and writes its own rows. This module
stores assertions in `manual_matches`, keyed by the bill's deterministic key,
and applies them afterwards. A re-run rebuilds the engine's rows from scratch;
the overlay survives because nothing in the engine ever writes to it.

The rule that matters most: **one transaction can back at most one bill.**
Assigning the same transaction to two bills double-counts revenue and hides a
genuine shortfall. It is refused by the validator, refused again by a partial
unique index in the database, and can only ever be recorded as a deliberate,
reasoned override — never on a single click.

No Streamlit imports here (§8). The page supplies the operator and the input.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from . import io_
from .db import ManualMatch, ManualMatchBatch, ManualMatchEvent, R1BillLine
from .exceptions import bill_exception_key, set_status

logger = logging.getLogger("mop_reco")

# A gateway reference is globally unique (2026-09-13, supersedes the ±1-day
# WINDOW fix from the day before). A late-evening sale is often posted by the
# gateway the following day — first found on bill SA62/024932/0726 (9 July),
# whose transaction sat in the 10 July file — but a window still missed
# genuine matches further out: SA12/045803/0726's transaction was dated a
# full 7 days after the bill. Every lookup below now searches the ENTIRE
# loaded transaction table for a gateway, no date restriction of any kind.
# The match is still recorded with how many days apart the two dates were
# (see r1_bill_txn.match_tier1's date_diff_days / match_note, and
# Validation.date_lag_days / date_lag_note below) — never silently accepted
# as same-day when it was not; only the SEARCH is date-agnostic. Revenue
# recognition stays on the bill's own business date regardless.


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))

GATEWAYS = ("paytm", "axis", "bajaj")

# The two statuses a manual match can carry. They are NEVER folded into the
# engine's "Matched" — see match_rate_split().
STATUS_MATCHED_MANUAL = "matched_manual"
STATUS_ASSERTED = "asserted_unverified"

ACTIVE = "active"
REVERSED = "reversed"
SUPERSEDED = "superseded"
CONFLICTED = "conflicted"
ORPHANED = "orphaned"

# Which validation case a submission fell into.
CASE_CLEAN = "a"        # found, amount equal, same store, unused
CASE_DISCREPANT = "b"   # found but wrong somehow, or already used
CASE_NOT_FOUND = "c"    # no such transaction in that date's gateway data

CASE_LABEL = {
    CASE_CLEAN: "Found and clean",
    CASE_DISCREPANT: "Found with a discrepancy",
    CASE_NOT_FOUND: "Not found in any gateway file, on any date",
}

# The gateway is NOT a column and is never asked for. It is derived from the
# transaction id by searching all three gateways — the operator already
# knows the id; making them also name the gateway is asking for something
# the data can answer. See identify_gateway().
#
# MOP and reason_code are ALSO not columns (revised 2026-09-13, on
# instruction — the template used to carry both). bill_no is globally unique
# (§5), so the bill's own mop is looked up from it rather than asked for
# again; every bulk-uploaded row records the same fixed reason code,
# BULK_UPLOAD_REASON_CODE, and the operator's own free-text `remark` goes in
# `note` instead of a picked-from-a-list reason.
UPLOAD_COLUMNS = ["bill_no", "business_date", "store", "transaction_id", "remark"]

BULK_UPLOAD_REASON_CODE = "bulk_upload"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class Validation:
    case: str
    ok: bool                      # may be accepted without an override
    status: str                   # the status that WOULD be written
    problems: list = field(default_factory=list)   # human-readable, specific
    txn: dict = None              # the gateway row, when found
    used_by_bill: str = None      # the other bill, when already used
    used_by_manual_id: int = None
    # Which gateway the id was found in. Derived, never supplied by the
    # operator — see identify_gateway().
    gateway: str = None
    # The transaction was found on a DIFFERENT date than the bill, within the
    # search window — still a valid match (2026-09-12), recorded here so it
    # is visible rather than silently accepted with no trace. 0 when the
    # dates agree; date_lag_note is None in that case too.
    date_lag_days: int = 0
    date_lag_note: str = None

    @property
    def needs_override(self) -> bool:
        return self.case == CASE_DISCREPANT

    def as_json(self) -> str:
        return json.dumps({
            "case": self.case, "problems": self.problems, "txn": self.txn,
            "gateway": self.gateway,
            "used_by_bill": self.used_by_bill,
            "used_by_manual_id": self.used_by_manual_id,
            "date_lag_days": self.date_lag_days,
            "date_lag_note": self.date_lag_note,
        }, default=str)


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _clean_ref(value) -> str:
    """Transaction ids/RRNs specifically: strips a leading apostrophe and a
    trailing '.0' on top of a plain strip (io_.clean_ref, already used
    elsewhere in the app for the same reason) — confirmed 2026-09-12 that a
    ref pasted out of Excel arrives as `'2026071001102...`, and the quote is
    Excel's own text-forcing marker, not part of the id. `_clean()` above
    stays a plain strip for every OTHER field (store, mop, reason_code,
    note, gateway) — only reference ids get this."""
    return io_.clean_ref(value) or ""


def find_transaction(gateway_pool: pd.DataFrame, gateway: str,
                     transaction_ref: str, business_date: str = None):
    """The gateway row for this reference, anywhere in `gateway_pool`.

    `gateway_pool` is the engine's own pool, with ITS column names —
    gateway / txn_ref / business_date / store / amount / status / txn_type
    (see r1_bill_txn.build_gateway_pool). Using the engine's shape rather than
    a renamed copy is deliberate: the validator must search exactly the data
    the engine searched, or it can disagree with it.

    No date filter here (2026-09-12, and no date restriction anywhere in the
    caller either as of 2026-09-13 — a gateway reference is globally unique,
    so the pool passed in is the whole loaded table, not a window). If the
    same reference somehow appears more than once in the pool (never
    expected — see r1_bill_txn.match_tier1), the row closest to
    `business_date` wins, purely to avoid picking an arbitrary one of two
    coincidental duplicates — this is a tiebreak, not a match rule.
    """
    if gateway_pool is None or gateway_pool.empty:
        return None
    ref = _clean_ref(transaction_ref)
    if not ref:
        return None
    hit = gateway_pool[
        gateway_pool["gateway"].astype(str).str.lower().eq(gateway.lower())
        & gateway_pool["txn_ref"].map(lambda v: _clean_ref(v)).eq(ref)
    ]
    if hit.empty:
        return None
    if len(hit) > 1 and business_date is not None:
        center = _as_date(business_date)
        hit = hit.copy()
        hit["_lag"] = hit["business_date"].map(
            lambda d: abs((_as_date(d) - center).days))
        hit = hit.sort_values("_lag")
    return hit.iloc[0].to_dict()


# warehouse source folder per gateway, for the bounded lookup below.
POOL_SOURCE = {"paytm": "paytm_settlement", "axis": "axis_edc", "bajaj": "bajaj"}

POOL_COLUMNS = {
    "paytm": {"transaction_id": "txn_ref", "amount": "amount", "status": "status",
              "transaction_type": "txn_type", "store": "store",
              "payment_mode": "payment_mode"},
    "axis": {"RRN": "txn_ref", "GROSS_AMT": "amount", "store": "store"},
    "bajaj": {"Transaction Id": "txn_ref", "Transaction Amount": "amount",
              "Transaction Status": "status"},
}


def targeted_pool(transaction_ref: str, warehouse=None) -> pd.DataFrame:
    """The rows of the gateway pool whose reference is exactly `transaction_ref` - same shape as
    `gateway_pool_for_date`, but read with a Parquet predicate on the reference column so a
    single pasted id costs a few column reads instead of loading millions of rows. The search is
    still the WHOLE warehouse (a gateway reference is globally unique), never a date window."""
    from pathlib import Path as _Path

    ref = _clean_ref(transaction_ref)
    root = _Path(warehouse) if warehouse else _Path(__file__).resolve().parents[2] / "warehouse"
    columns = ["gateway", "txn_ref", "business_date", "store", "amount", "status", "txn_type", "payment_mode"]
    frames = []
    for name, source in POOL_SOURCE.items():
        folder = root / source
        if not folder.exists() or not ref:
            continue
        ref_col = next(src for src, dst in POOL_COLUMNS[name].items() if dst == "txn_ref")
        for path in sorted(folder.glob("*.parquet")):
            if path.stem.startswith("_"):
                continue
            try:
                raw = pd.read_parquet(path, filters=[(ref_col, "==", ref)])
            except Exception:  # noqa: BLE001 - a file without the column cannot hold the id
                continue
            if raw.empty:
                continue
            mapping = {src: dst for src, dst in POOL_COLUMNS[name].items() if src in raw.columns}
            frame = raw[list(mapping)].rename(columns=mapping)
            frame["gateway"] = name
            if "business_date" not in frame.columns:
                frame["business_date"] = path.stem
            for column in ("status", "txn_type", "store", "payment_mode"):
                if column not in frame.columns:
                    frame[column] = None
            frames.append(frame[columns])
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


def gateway_pool_for_date(business_date=None, gateway: str = None,
                          warehouse=None) -> pd.DataFrame:
    """Every loaded row for a gateway (or all three), in the engine's pool
    shape. No date restriction of any kind (2026-09-13) — a gateway
    reference is globally unique, so the search is the ENTIRE table, never a
    window around `business_date`. `business_date` is accepted but not used
    for filtering; kept as a parameter only because callers pass the bill's
    own date for logging/lag-computation purposes elsewhere.

    §6.14 still applies on the read side — this reads already-normalised
    per-date Parquet, never a raw file — but it is no longer bounded to a
    handful of dates: it reads every date-partitioned file the warehouse
    holds for the gateway(s) asked for. Measured on the July data: ~33
    files, well under a second — still far short of the 1.49M-row POS month
    this rule exists to keep off a page, and this is a single operator
    action, not a page load shared by everyone.
    """
    from pathlib import Path as _Path

    root = _Path(warehouse) if warehouse else _Path(__file__).resolve().parents[2] / "warehouse"
    wanted = [gateway] if gateway else list(POOL_SOURCE)
    columns = ["gateway", "txn_ref", "business_date", "store",
              "amount", "status", "txn_type", "payment_mode"]
    frames = []
    for name in wanted:
        folder = root / POOL_SOURCE[name]
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.parquet")):
            if path.stem.startswith("_"):
                continue
            raw = pd.read_parquet(path)
            mapping = {src: dst for src, dst in POOL_COLUMNS[name].items()
                       if src in raw.columns}
            frame = raw[list(mapping)].rename(columns=mapping)
            frame["gateway"] = name
            if "business_date" not in frame.columns:
                frame["business_date"] = path.stem
            for column in ("status", "txn_type", "store", "payment_mode"):
                if column not in frame.columns:
                    frame[column] = None
            frames.append(frame[columns])
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


@dataclass
class GatewayHit:
    gateway: str = None
    txn: dict = None
    candidates: list = field(default_factory=list)   # every gateway it was found in
    problem: str = None

    @property
    def found(self) -> bool:
        return self.gateway is not None


def identify_gateway(session, business_date, transaction_ref: str,
                     gateway_pool: pd.DataFrame = None) -> GatewayHit:
    """Which gateway does this id belong to? Search all three; never ask.

    An id found in exactly one gateway identifies itself. Found in none is
    case (c). Found in MORE than one is reported as a problem rather than
    resolved — a Paytm transaction_id and an Axis RRN colliding would mean
    picking one silently, and picking silently is what rule 5 forbids.

    The search covers the ENTIRE loaded transaction table, no date
    restriction of any kind (2026-09-13 — a gateway reference is globally
    unique) — see gateway_pool_for_date.
    """
    ref = _clean_ref(transaction_ref)
    hit = GatewayHit()
    if not ref:
        hit.problem = "No transaction id or RRN was given"
        return hit

    pool = (gateway_pool if gateway_pool is not None
            else gateway_pool_for_date(business_date))
    for name in GATEWAYS:
        row = find_transaction(pool, name, ref, business_date)
        if row is not None:
            hit.candidates.append((name, row))

    if not hit.candidates:
        return hit
    if len(hit.candidates) > 1:
        names = ", ".join(n for n, _ in hit.candidates)
        hit.problem = (
            f"Transaction {ref} appears in more than one gateway ({names}) near "
            f"{business_date}. Which one it belongs to cannot be decided from "
            "the id alone, so it is reported rather than guessed."
        )
        return hit

    hit.gateway, hit.txn = hit.candidates[0]
    return hit


def validate_by_id(session, bill: dict, transaction_ref: str,
                   gateway_pool: pd.DataFrame = None) -> Validation:
    """Validate from the transaction id alone, working out the gateway.

    This is what the inline paste and the bulk upload both use. The returned
    Validation carries `gateway` so the caller can show which gateway the id
    was found in — the operator learns it rather than supplies it.
    """
    pool = (gateway_pool if gateway_pool is not None
            else gateway_pool_for_date())
    hit = identify_gateway(session, bill["business_date"], transaction_ref, pool)

    if hit.problem and not hit.found:
        # Ambiguous across gateways, or blank. Neither is a match.
        result = Validation(CASE_DISCREPANT, False, STATUS_ASSERTED, [hit.problem])
        result.gateway = None
        return result

    if not hit.found:
        # Case (c) — absent from the ENTIRE loaded table, not just a window
        # around the bill's date. Only now is "not found" the right word.
        result = Validation(
            CASE_NOT_FOUND, False, STATUS_ASSERTED,
            [f"Transaction {_clean_ref(transaction_ref)} is not in the Paytm, "
             f"Axis or Bajaj data loaded, on any date. This cannot be "
             "called matched."])
        result.gateway = None
        return result

    result = validate(session, bill, hit.gateway, transaction_ref, pool)
    result.gateway = hit.gateway
    return result


def validate(session, bill: dict, gateway: str, transaction_ref: str,
             gateway_pool: pd.DataFrame) -> Validation:
    """Which of cases (a), (b), (c) applies — before anything is written.

    `bill` needs business_date, store, bill_no, mop, amount.
    """
    gateway = _clean(gateway).lower()
    ref = _clean_ref(transaction_ref)
    problems = []

    if gateway not in GATEWAYS:
        return Validation(CASE_DISCREPANT, False, STATUS_ASSERTED,
                          [f"'{gateway}' is not one of {', '.join(GATEWAYS)}"])
    if not ref:
        return Validation(CASE_DISCREPANT, False, STATUS_ASSERTED,
                          ["No transaction id or RRN was given"])

    # Already used by ANOTHER active manual match? This is checked first
    # because it is the answer that matters most, and it is true regardless of
    # whether the transaction is otherwise clean.
    existing = (session.query(ManualMatch)
                .filter(ManualMatch.gateway == gateway,
                        ManualMatch.transaction_ref == ref,
                        ManualMatch.lifecycle == ACTIVE)
                .first())
    if existing and existing.bill_key != bill_key_of(bill):
        problems.append(
            f"Transaction {ref} is already matched manually to bill "
            f"{existing.bill_no} ({existing.store}, {existing.business_date}, "
            f"Rs {float(existing.amount or 0):,.2f}) by {existing.operator} on "
            f"{existing.created_at:%d %b %Y %H:%M}. One transaction cannot pay "
            "two bills — accepting this would double-count revenue and hide a "
            "genuine shortfall."
        )

    txn = find_transaction(gateway_pool, gateway, ref, bill["business_date"])

    if txn is None:
        # Case (c). Not "matched" — asserted, and it stays visible as an open
        # item. Note this is reported even if it is also already used; the
        # not-found problem is the governing one. `gateway_pool` here is the
        # ENTIRE loaded table for the gateway (2026-09-13, no date
        # restriction) — absent from it means absent everywhere loaded, not
        # just near this one date.
        problems.append(
            f"No {gateway} transaction {ref} exists anywhere in the data "
            "loaded. This cannot be called matched."
        )
        return Validation(CASE_NOT_FOUND, False, STATUS_ASSERTED, problems)

    # Found on a DIFFERENT date than the bill — could be any number of days
    # away now that the search is date-agnostic (2026-09-13). Still a valid
    # match — a gateway reference is globally unique — but never silently
    # accepted as same-day when it was not.
    date_lag_days = 0
    date_lag_note = None
    found_date_str = _clean(txn.get("business_date"))
    bill_date_str = _clean(bill.get("business_date"))
    if found_date_str and found_date_str != bill_date_str:
        found_d = _as_date(found_date_str)
        bill_d = _as_date(bill_date_str)
        date_lag_days = (found_d - bill_d).days
        direction = "after" if date_lag_days > 0 else "before"
        date_lag_note = (
            f"Found in the {found_d:%d %B} file; bill dated {bill_d:%d %B} "
            f"({abs(date_lag_days)} day(s) {direction}). Still accepted as a "
            "match — a gateway reference is globally unique regardless of "
            "which day the gateway posted it."
        )

    # Already consumed by the ENGINE on this run?
    engine_owner = (session.query(R1BillLine)
                    .filter(R1BillLine.matched_txn_ref == ref,
                            R1BillLine.matched.is_(True))
                    .first())
    if engine_owner:
        problems.append(
            f"Transaction {ref} is already matched by the engine to bill "
            f"{engine_owner.bill_no} ({engine_owner.store}, "
            f"{engine_owner.business_date}). One transaction cannot pay two bills."
        )

    txn_amount = float(txn.get("amount") or 0)
    bill_amount = float(bill.get("amount") or 0)
    if abs(txn_amount - bill_amount) > 0.005:
        problems.append(
            f"Amount differs: the bill is Rs {bill_amount:,.2f} and the "
            f"transaction is Rs {txn_amount:,.2f} "
            f"(a difference of Rs {txn_amount - bill_amount:+,.2f})."
        )

    txn_store = _clean(txn.get("store"))
    bill_store = _clean(bill.get("store"))
    if txn_store and bill_store and txn_store != bill_store:
        problems.append(
            f"Store differs: the bill is {bill_store} and the transaction "
            f"belongs to {txn_store}."
        )

    txn_status = _clean(txn.get("status")).upper()
    if txn_status and txn_status != "SUCCESS":
        problems.append(f"The transaction status is {txn_status}, not SUCCESS.")

    txn_type = _clean(txn.get("txn_type")).upper()
    if txn_type and txn_type != "ACQUIRING":
        problems.append(f"The transaction type is {txn_type}, not ACQUIRING.")

    if problems:
        return Validation(CASE_DISCREPANT, False, STATUS_MATCHED_MANUAL,
                          problems, txn=_txn_summary(txn),
                          used_by_bill=(engine_owner.bill_no if engine_owner
                                        else (existing.bill_no if existing else None)),
                          used_by_manual_id=(existing.id if existing else None),
                          date_lag_days=date_lag_days, date_lag_note=date_lag_note)

    return Validation(CASE_CLEAN, True, STATUS_MATCHED_MANUAL, [],
                      txn=_txn_summary(txn),
                      date_lag_days=date_lag_days, date_lag_note=date_lag_note)


def _txn_summary(txn: dict) -> dict:
    keep = ("gateway", "txn_ref", "business_date", "store", "amount",
            "status", "txn_type", "payment_mode")
    return {k: (float(txn[k]) if k == "amount" and txn.get(k) is not None
                else txn.get(k)) for k in keep if k in txn}


def find_bill_for_match(session, run_id: int, bill_no: str):
    """The reverse direction of validate_by_id(): given a BILL NUMBER (typed
    into "By transaction" mode's Matched bill no column), find the bill and
    say whether it is currently open. `bill_no` is globally unique (§5), so
    no date/store scope is needed to search for it — the whole run is
    searched, never a filtered slice, so a bill outside the sidebar's current
    date range still resolves rather than reporting a false "not found".

    Returns (bill_dict_or_None, is_open, reason_if_not_open).
    """
    bill_no = _clean(bill_no)
    if not bill_no:
        return None, False, "no bill number was given"

    row = (session.query(R1BillLine)
          .filter(R1BillLine.run_id == run_id, R1BillLine.bill_no == bill_no)
          .first())
    if row is None:
        return None, False, f"bill {bill_no!r} was not found in the loaded data"

    bill = {"business_date": row.business_date, "store": row.store,
           "bill_no": row.bill_no, "mop": row.mop,
           "amount": float(row.amount or 0)}

    if row.matched:
        return bill, False, (
            f"bill {bill_no!r} is already matched by the engine to transaction "
            f"{row.matched_txn_ref} ({row.matched_gateway})"
        )

    existing = (session.query(ManualMatch)
               .filter(ManualMatch.bill_key == bill_key_of(bill),
                       ManualMatch.lifecycle == ACTIVE).first())
    if existing is not None:
        return bill, False, (
            f"bill {bill_no!r} is already matched manually to transaction "
            f"{existing.transaction_ref} ({existing.gateway}) by {existing.operator}"
        )

    return bill, True, None


def bill_key_of(bill: dict) -> str:
    """The deterministic bill key (§6.9), so remarks and status set before a
    manual match still point at the same thing afterwards."""
    return bill_exception_key(bill["business_date"], bill.get("store"),
                              bill.get("bill_no"), bill.get("mop"))


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record(session, bill: dict, gateway, transaction_ref: str,
           reason_code: str, operator: str, validation: Validation,
           note: str = None, override_reason: str = None,
           batch: ManualMatchBatch = None) -> ManualMatch:
    """Write the overlay row, close the bill's exception, and log the event.

    Refuses a case (b) submission with no written override reason: the whole
    point of case (b) is that it cannot be accepted on a single click.
    """
    if not operator:
        raise ValueError("A manual match must record who made it")
    if not _clean(reason_code):
        raise ValueError("A manual match must carry a reason code")

    # This is checked BEFORE the override rule: "the bill already has a match"
    # is the more fundamental refusal, and reporting the override requirement
    # first would send the operator off to write a justification for something
    # that cannot be recorded at all.
    # The gateway comes from the validation when the caller worked it out from
    # the id (the inline paste and the upload both do). Case (c) has no
    # gateway at all — the id was found nowhere — and that is recorded as
    # "unknown" rather than left null, so the register never implies a gateway
    # confirmed something it does not have.
    gateway = _clean(gateway) or _clean(validation.gateway) or "unknown"
    gateway = gateway.lower()

    key = bill_key_of(bill)
    if (session.query(ManualMatch)
            .filter(ManualMatch.bill_key == key,
                    ManualMatch.lifecycle == ACTIVE).first()):
        raise ValueError(f"Bill {bill.get('bill_no')} already has an active "
                         "manual match. Reverse it before matching again.")

    if validation.needs_override and not _clean(override_reason):
        raise ValueError(
            "This submission has a discrepancy (case b) and needs an explicit "
            "written override reason. Refused."
        )

    now = datetime.now()
    entry = ManualMatch(
        bill_key=key,
        business_date=str(bill["business_date"]),
        store=bill.get("store"),
        bill_no=bill.get("bill_no"),
        mop=bill.get("mop"),
        amount=bill.get("amount"),
        gateway=gateway,
        transaction_ref=_clean_ref(transaction_ref),
        status=validation.status,
        validation_case=validation.case,
        override_used=bool(validation.needs_override),
        override_reason=_clean(override_reason) or None,
        reason_code=_clean(reason_code),
        note=_clean(note) or None,
        validation_detail=validation.as_json(),
        operator=operator,
        created_at=now,
        batch_id=batch.id if batch else None,
        lifecycle=ACTIVE,
    )
    session.add(entry)
    session.flush()

    session.add(ManualMatchEvent(
        manual_match_id=entry.id, event="created", at=now, operator=operator,
        detail=json.dumps({
            "case": validation.case, "status": validation.status,
            "override": bool(validation.needs_override),
            "override_reason": _clean(override_reason) or None,
            "problems": validation.problems,
        }),
    ))

    # The bill's exception is closed with a reason, and the remark history that
    # was already there is untouched (rule 4).
    if validation.status == STATUS_MATCHED_MANUAL:
        try:
            set_status(session, key, "closed", operator,
                       reason_code="matched manually")
        except (ValueError, LookupError) as exc:
            # No exception row for this bill (it may never have been
            # registered). Not fatal, but not silent either.
            logger.info(f"manual_match: no exception to close for {key} ({exc})")

    session.commit()
    logger.info(f"manual_match: {entry.status} case={entry.validation_case} "
                f"bill={entry.bill_no} txn={entry.transaction_ref} by {operator}")
    return entry


def reverse(session, manual_match_id: int, operator: str,
            reason: str) -> ManualMatch:
    """Undo a manual match: the bill returns to unmatched and the transaction
    is released. The record itself is kept (rule 4)."""
    entry = session.get(ManualMatch, manual_match_id)
    if entry is None:
        raise LookupError(f"No manual match #{manual_match_id}")
    if entry.lifecycle != ACTIVE:
        raise ValueError(f"Manual match #{manual_match_id} is already "
                         f"{entry.lifecycle}, not active")
    if not _clean(reason):
        raise ValueError("A reversal must carry a reason")

    now = datetime.now()
    entry.lifecycle = REVERSED
    entry.lifecycle_note = _clean(reason)
    entry.lifecycle_at = now
    entry.reversed_by = operator
    session.add(ManualMatchEvent(manual_match_id=entry.id, event="reversed",
                                 at=now, operator=operator,
                                 detail=json.dumps({"reason": _clean(reason)})))
    try:
        set_status(session, entry.bill_key, "open", operator,
                   reason_code="manual match reversed")
    except (ValueError, LookupError) as exc:
        logger.info(f"manual_match: no exception to reopen for {entry.bill_key} ({exc})")
    session.commit()
    logger.info(f"manual_match: reversed #{entry.id} by {operator}")
    return entry


def reverse_batch(session, batch_id: int, operator: str, reason: str) -> int:
    """A whole upload undone as a unit — that is what the batch id is for."""
    entries = (session.query(ManualMatch)
               .filter(ManualMatch.batch_id == batch_id,
                       ManualMatch.lifecycle == ACTIVE).all())
    for entry in entries:
        reverse(session, entry.id, operator, reason)
    return len(entries)


# ---------------------------------------------------------------------------
# Applying the overlay to the engine's rows
# ---------------------------------------------------------------------------

def active_matches(session, run_id: int = None) -> pd.DataFrame:
    rows = (session.query(ManualMatch)
            .filter(ManualMatch.lifecycle == ACTIVE).all())
    return pd.DataFrame([{
        "id": r.id, "bill_key": r.bill_key, "business_date": r.business_date,
        "store": r.store, "bill_no": r.bill_no, "mop": r.mop,
        "amount": float(r.amount or 0), "gateway": r.gateway,
        "transaction_ref": r.transaction_ref, "status": r.status,
        "validation_case": r.validation_case, "override_used": r.override_used,
        "reason_code": r.reason_code, "operator": r.operator,
        "created_at": r.created_at, "batch_id": r.batch_id,
    } for r in rows])


def consumed_transactions(session) -> set:
    """(gateway, transaction_ref) pairs held by an active manual match.

    The engine must treat these as already consumed on a re-run — a
    transaction consumed manually can never also be consumed automatically.
    """
    return {(r.gateway, r.transaction_ref) for r in
            session.query(ManualMatch)
            .filter(ManualMatch.lifecycle == ACTIVE).all()}


def apply_overlay(session, bill_lines: pd.DataFrame) -> pd.DataFrame:
    """Returns `bill_lines` with the overlay applied — a NEW frame; the input
    is not mutated, and the engine's own columns are preserved alongside.

    Adds:
      manual_status      the overlay status, or None
      match_source       "system" | "manual" | "asserted" | None
      effective_matched  system-matched OR manual-matched (NOT asserted)
    """
    out = bill_lines.copy()
    if out.empty:
        for column in ("manual_status", "match_source", "effective_matched"):
            out[column] = None
        return out

    overlay = active_matches(session)
    out["bill_key"] = [
        bill_key_of({"business_date": r.business_date, "store": r.store,
                     "bill_no": r.bill_no, "mop": r.mop})
        for r in out.itertuples()
    ]
    if overlay.empty:
        out["manual_status"] = None
        out["manual_txn"] = None
    else:
        by_key = overlay.set_index("bill_key")
        out["manual_status"] = out["bill_key"].map(by_key["status"])
        out["manual_txn"] = out["bill_key"].map(by_key["transaction_ref"])

    def source(row):
        if row.matched:
            return "system"
        if row.manual_status == STATUS_MATCHED_MANUAL:
            return "manual"
        if row.manual_status == STATUS_ASSERTED:
            return "asserted"
        return None

    out["match_source"] = [source(r) for r in out.itertuples()]
    # An assertion with no gateway row behind it is NOT a match. It stays an
    # open item, which is the whole reason it has its own status.
    out["effective_matched"] = out["match_source"].isin(("system", "manual"))
    return out


# ---------------------------------------------------------------------------
# The match rate, never blended
# ---------------------------------------------------------------------------

@dataclass
class MatchRateSplit:
    lines: int = 0
    system: int = 0
    manual: int = 0
    asserted: int = 0
    unmatched: int = 0
    value: float = 0.0
    system_value: float = 0.0
    manual_value: float = 0.0
    asserted_value: float = 0.0
    unmatched_value: float = 0.0

    @property
    def system_rate(self) -> float:
        return self.system / self.lines if self.lines else 0.0

    @property
    def effective_rate(self) -> float:
        """System + manual. Deliberately NOT including asserted."""
        return (self.system + self.manual) / self.lines if self.lines else 0.0


def match_rate_split(overlaid: pd.DataFrame) -> MatchRateSplit:
    """Matched by system / matched manually / asserted unverified, separately.

    A single blended "99.99% matched" that includes human assertions is a
    number that flatters itself, so this never produces one.
    """
    split = MatchRateSplit()
    if overlaid.empty:
        return split
    split.lines = len(overlaid)
    split.value = float(pd.to_numeric(overlaid["amount"], errors="coerce").sum())

    for name, attr in (("system", "system"), ("manual", "manual"),
                       ("asserted", "asserted")):
        subset = overlaid[overlaid["match_source"] == name]
        setattr(split, attr, len(subset))
        setattr(split, f"{attr}_value",
                float(pd.to_numeric(subset["amount"], errors="coerce").sum()))

    rest = overlaid[overlaid["match_source"].isna()]
    split.unmatched = len(rest)
    split.unmatched_value = float(pd.to_numeric(rest["amount"], errors="coerce").sum())
    return split


# ---------------------------------------------------------------------------
# Conflict handling on re-run
# ---------------------------------------------------------------------------

@dataclass
class ReconcileOutcome:
    superseded: list = field(default_factory=list)
    conflicted: list = field(default_factory=list)
    orphaned: list = field(default_factory=list)
    unchanged: int = 0

    @property
    def needs_attention(self) -> list:
        return self.conflicted


def reconcile_after_run(session, run_id: int, operator: str = "system",
                        apply_changes: bool = True) -> ReconcileOutcome:
    """Re-check every active manual match against a fresh run.

    Three cases, all reported, none silent:
      - the engine now matches that bill to the SAME transaction -> superseded
      - the engine matches it to a DIFFERENT transaction -> conflicted, both
        kept visible, a person resolves it. Nothing is overwritten.
      - the bill no longer exists -> orphaned, the transaction is released,
        the record is kept.
    """
    outcome = ReconcileOutcome()
    entries = (session.query(ManualMatch)
               .filter(ManualMatch.lifecycle == ACTIVE).all())
    if not entries:
        return outcome

    lines = {}
    for line in (session.query(R1BillLine)
                 .filter(R1BillLine.run_id == run_id).all()):
        lines[bill_key_of({"business_date": line.business_date,
                           "store": line.store, "bill_no": line.bill_no,
                           "mop": line.mop})] = line

    now = datetime.now()
    for entry in entries:
        line = lines.get(entry.bill_key)

        if line is None:
            _transition(session, entry, ORPHANED, now, operator,
                        "the bill is no longer in the dump — most likely an IT "
                        "duplicate deletion. The transaction is released and "
                        "this record is kept.")
            outcome.orphaned.append(entry)
            continue

        if not line.matched:
            outcome.unchanged += 1
            continue

        engine_ref = _clean(line.matched_txn_ref)
        if engine_ref == _clean(entry.transaction_ref):
            _transition(session, entry, SUPERSEDED, now, operator,
                        f"the engine now matches this bill to the same "
                        f"transaction ({engine_ref}) on its own, so the manual "
                        "match is redundant.")
            outcome.superseded.append(entry)
        else:
            _transition(session, entry, CONFLICTED, now, operator,
                        f"CONFLICT: the engine matched this bill to "
                        f"{engine_ref}, the manual match says "
                        f"{entry.transaction_ref}. Both are kept — a person "
                        "must resolve which is right. Neither was overwritten.")
            outcome.conflicted.append(entry)

    if apply_changes:
        session.commit()
    else:
        session.rollback()
    logger.info(f"manual_match: after run #{run_id} — "
                f"{len(outcome.superseded)} superseded, "
                f"{len(outcome.conflicted)} CONFLICTED, "
                f"{len(outcome.orphaned)} orphaned, "
                f"{outcome.unchanged} unchanged")
    return outcome


def _transition(session, entry: ManualMatch, lifecycle: str, at: datetime,
                operator: str, note: str):
    entry.lifecycle = lifecycle
    entry.lifecycle_note = note
    entry.lifecycle_at = at
    session.add(ManualMatchEvent(manual_match_id=entry.id, event=lifecycle,
                                 at=at, operator=operator,
                                 detail=json.dumps({"note": note})))


# ---------------------------------------------------------------------------
# Bulk upload
# ---------------------------------------------------------------------------

@dataclass
class RowOutcome:
    row_number: int
    bill: dict
    gateway: str
    transaction_ref: str
    reason_code: str
    note: str
    verdict: str            # "accept" | "warn" | "reject"
    why: list = field(default_factory=list)
    validation: Validation = None


@dataclass
class UploadPreview:
    rows: list = field(default_factory=list)

    @property
    def accepted(self) -> list:
        return [r for r in self.rows if r.verdict == "accept"]

    @property
    def warned(self) -> list:
        return [r for r in self.rows if r.verdict == "warn"]

    @property
    def rejected(self) -> list:
        return [r for r in self.rows if r.verdict == "reject"]

    def as_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "row": r.row_number,
            "business_date": r.bill.get("business_date"),
            "store": r.bill.get("store"),
            "bill_no": r.bill.get("bill_no"),
            "mop": r.bill.get("mop"),
            "gateway": r.gateway,
            "transaction_id": r.transaction_ref,
            "remark": r.note,
            "verdict": r.verdict,
            "case": r.validation.case if r.validation else "",
            "date_lag_days": r.validation.date_lag_days if r.validation else 0,
            "why": " | ".join(r.why),
        } for r in self.rows])


def upload_template() -> pd.DataFrame:
    """The downloadable template — exactly the columns the loader reads."""
    return pd.DataFrame([{
        "bill_no": "SB58/024800/0726",
        "business_date": "2026-07-20",
        "store": "ABD",
        "transaction_id": "20260720111212800110168...",
        "remark": "confirmed with Paytm support, ticket 12345",
    }], columns=UPLOAD_COLUMNS)


def preview_upload(session, frame: pd.DataFrame, gateway_pool: pd.DataFrame,
                   run_id: int, reason_codes: list = None) -> UploadPreview:
    """Validate EVERY row and apply nothing.

    A row is rejected — not warned — when the bill is not an unmatched bill in
    this run, or when the transaction is already used. Those are the two cases
    where applying it would corrupt a figure rather than merely be doubtful.

    `bill_no` alone identifies the bill (globally unique, §5) — mop is looked
    up from the system's own record, never re-typed by the operator. The
    uploaded `business_date`/`store` are cross-checked against that same
    record and a mismatch is rejected (rule 5: nothing is guessed, and a
    disagreement between what the operator typed and what the bill actually
    is must be surfaced, never silently resolved in either direction).
    `reason_codes` is accepted for signature compatibility but no longer
    used — every bulk-uploaded row records `BULK_UPLOAD_REASON_CODE`.
    """
    preview = UploadPreview()
    missing = [c for c in UPLOAD_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"The upload is missing column(s): {', '.join(missing)}. "
                         f"Expected exactly: {', '.join(UPLOAD_COLUMNS)}")

    unmatched_by_bill_no = {}
    for line in (session.query(R1BillLine)
                 .filter(R1BillLine.run_id == run_id,
                         R1BillLine.matched.is_(False)).all()):
        unmatched_by_bill_no.setdefault(line.bill_no, line)

    seen_refs = {}
    for position, raw in enumerate(frame.to_dict("records"), start=2):
        bill_no = _clean(raw.get("bill_no"))
        typed_date = _clean(raw.get("business_date"))
        typed_store = _clean(raw.get("store"))
        ref = _clean_ref(raw.get("transaction_id"))
        remark = _clean(raw.get("remark"))
        why = []

        line = unmatched_by_bill_no.get(bill_no)
        bill = {"business_date": typed_date, "store": typed_store,
               "bill_no": bill_no, "mop": None}
        if line is None:
            why.append(f"bill {bill_no!r} is not an unmatched bill in this run "
                       "(not found, or already matched)")
            preview.rows.append(RowOutcome(position, bill, None, ref,
                                           BULK_UPLOAD_REASON_CODE, remark,
                                           "reject", why))
            continue

        bill = {"business_date": line.business_date, "store": line.store,
               "bill_no": line.bill_no, "mop": line.mop,
               "amount": float(line.amount or 0)}
        if typed_date and typed_date != _clean(line.business_date):
            why.append(f"typed business_date {typed_date!r} does not match "
                       f"the bill's own date {line.business_date!r}")
        if typed_store and typed_store != _clean(line.store):
            why.append(f"typed store {typed_store!r} does not match the "
                       f"bill's own store {line.store!r}")
        if why:
            preview.rows.append(RowOutcome(position, bill, None, ref,
                                           BULK_UPLOAD_REASON_CODE, remark,
                                           "reject", why))
            continue

        # A file that names the same transaction twice would double-count
        # within the batch itself, which no per-row check would catch.
        if ref in seen_refs:
            why.append(f"transaction {ref} is also used on row "
                       f"{seen_refs[ref]} of this same file")
            preview.rows.append(RowOutcome(position, bill, None, ref,
                                           BULK_UPLOAD_REASON_CODE, remark,
                                           "reject", why))
            continue
        seen_refs[ref] = position

        # The gateway is worked out from the id, not read from the file.
        validation = validate_by_id(session, bill, ref, gateway_pool)
        gateway = validation.gateway
        why.extend(validation.problems)
        if validation.date_lag_note:
            # Visible, never hidden — but NOT a reason to warn or reject; a
            # date lag is still accepted on its own (a gateway reference is
            # globally unique regardless of which day it posted, 2026-09-13).
            why.append(validation.date_lag_note)

        already_used = any("cannot pay two bills" in p for p in validation.problems)
        if already_used:
            verdict = "reject"
        elif validation.case == CASE_CLEAN:
            verdict = "accept"
        else:
            verdict = "warn"

        preview.rows.append(RowOutcome(position, bill, gateway, ref,
                                       BULK_UPLOAD_REASON_CODE, remark,
                                       verdict, why, validation))
    return preview


def apply_upload(session, preview: UploadPreview, operator: str,
                 include_warnings: bool = False, override_reason: str = None,
                 file_name: str = None) -> tuple:
    """Apply a previewed upload as ONE batch, so it can be reversed as a unit.

    `include_warnings` applies the case (b)/(c) rows too, and then
    `override_reason` is mandatory — the same rule as the single-row flow.
    """
    chosen = list(preview.accepted)
    if include_warnings:
        if not _clean(override_reason):
            raise ValueError("Applying rows with warnings needs a written "
                             "override reason. Refused.")
        chosen += preview.warned

    batch = ManualMatchBatch(
        created_at=datetime.now(), operator=operator, source="upload",
        file_name=file_name, rows_submitted=len(preview.rows),
        rows_accepted=0, rows_rejected=len(preview.rejected),
        note=_clean(override_reason) or None,
    )
    session.add(batch)
    session.flush()

    applied, failed = [], []
    for row in chosen:
        try:
            entry = record(session, row.bill, row.gateway, row.transaction_ref,
                           row.reason_code, operator, row.validation,
                           note=row.note, override_reason=override_reason,
                           batch=batch)
            applied.append(entry)
        except (ValueError, LookupError) as exc:
            failed.append((row, str(exc)))

    batch.rows_accepted = len(applied)
    batch.rows_rejected = len(preview.rejected) + len(failed)
    session.commit()
    logger.info(f"manual_match: batch #{batch.id} applied {len(applied)} row(s), "
                f"{len(failed)} failed, {len(preview.rejected)} rejected")
    return batch, applied, failed


# ---------------------------------------------------------------------------
# Suggested matches — no reference at all, same store + amount within a
# window (2026-09-13). Never auto-matched — time never decides a match
# (CLAUDE.md §6.3, the same rule Tier 2 already follows) — this only
# SUGGESTS a candidate (or a whole solvable set of them) for one click.
# ---------------------------------------------------------------------------

SUGGESTION_WINDOW_DAYS = 2
SUGGESTED_REASON_CODE = "gateway_confirmed_by_email"


@dataclass
class Suggestion:
    """One (store, amount) group: some open bills with no reference, some
    unmapped transactions in the window, both sharing that store and amount.

    When the two counts are EQUAL, this is a SOLVABLE SET (2026-09-13, on
    instruction: "2 bills and 2 transactions... treat it as a solvable set,
    not ambiguous") — `paired` holds one (bill, txn) tuple per bill, matched
    by closest date, ready for a single "accept all" action. When the counts
    differ, `paired` is empty and every bill is shown against the full
    candidate list instead — genuinely ambiguous, never resolved by
    guessing, exactly like Tier 2's own rule for one bill with more than one
    candidate.
    """
    store: str
    amount: float
    bills: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    paired: list = field(default_factory=list)

    @property
    def is_solvable_set(self) -> bool:
        return len(self.bills) > 0 and len(self.bills) == len(self.candidates)


def suggested_matches(session, run_id, date_from, date_to, store: str = None,
                      window_days: int = SUGGESTION_WINDOW_DAYS,
                      warehouse=None) -> list:
    """For every OPEN bill with NO gateway reference at all, suggest unmapped
    transactions in the SAME store and SAME amount, dated within
    `window_days` of the bill. Grouped by (store, amount): equal bill/
    candidate counts is a solvable set (see `Suggestion`); otherwise every
    bill is listed with its candidates, unresolved. Sorted solvable sets
    first, then by amount descending.
    """
    from . import gateway_txns as gt

    bills_query = session.query(R1BillLine).filter(
        R1BillLine.run_id == run_id, R1BillLine.matched.is_(False),
        (R1BillLine.paytm_ref.is_(None) | (R1BillLine.paytm_ref == "")),
        (R1BillLine.axis_ref.is_(None) | (R1BillLine.axis_ref == "")),
        (R1BillLine.bajaj_ref.is_(None) | (R1BillLine.bajaj_ref == "")),
    )
    if date_from is not None:
        bills_query = bills_query.filter(R1BillLine.business_date >= str(date_from))
    if date_to is not None:
        bills_query = bills_query.filter(R1BillLine.business_date <= str(date_to))
    if store:
        bills_query = bills_query.filter(R1BillLine.store == store)
    open_bills = bills_query.all()
    if not open_bills:
        return []

    bill_dates_all = [_as_date(b.business_date) for b in open_bills]
    pool_from = min(bill_dates_all) - timedelta(days=window_days)
    pool_to = max(bill_dates_all) + timedelta(days=window_days)
    pool = gt.gateway_pool_for_range(pool_from, pool_to, warehouse=warehouse)
    mapped = gt.with_bill_mapping(session, pool)
    unmapped = mapped.loc[~mapped["mapped"]]
    if store:
        unmapped = unmapped.loc[unmapped["store"] == store]

    groups: dict = {}
    for b in open_bills:
        key = (b.store, round(float(b.amount or 0), 2))
        groups.setdefault(key, {"bills": [], "candidates": []})["bills"].append(b)
    for row in unmapped.itertuples():
        key = (row.store, round(float(row.amount), 2))
        if key in groups:
            groups[key]["candidates"].append(row)

    out = []
    for (store_code, amount), payload in groups.items():
        bills = payload["bills"]
        bill_dates = [_as_date(b.business_date) for b in bills]
        candidates = [
            c for c in payload["candidates"]
            if any(abs((_as_date(c.business_date) - bd).days) <= window_days
                  for bd in bill_dates)
        ]
        if not candidates:
            continue

        suggestion = Suggestion(store=store_code, amount=amount)
        suggestion.bills = [{
            "business_date": b.business_date, "bill_no": b.bill_no,
            "mop": b.mop, "amount": float(b.amount or 0), "store": b.store,
        } for b in bills]
        suggestion.candidates = [{
            "gateway": c.gateway, "txn_ref": c.txn_ref,
            "business_date": c.business_date, "amount": float(c.amount),
        } for c in candidates]

        if len(bills) == len(candidates):
            # A solvable set — pair each bill with its closest-date candidate,
            # greedily, removing each candidate once used.
            remaining = list(suggestion.candidates)
            for bill_dict, bd in zip(suggestion.bills, bill_dates):
                remaining.sort(key=lambda c: abs(
                    (_as_date(c["business_date"]) - bd).days))
                suggestion.paired.append((bill_dict, remaining.pop(0)))
        out.append(suggestion)

    out.sort(key=lambda s: (not s.is_solvable_set, -s.amount))
    return out
