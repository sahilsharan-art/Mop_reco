"""Masters live in the DATABASE (2026-11-21).

Until now every master was an Excel workbook that each run re-read and re-snapshotted. The operating model is now:

  * the masters are imported ONCE from Excel into PostgreSQL (`build_snapshot` -> `import_snapshot`);
  * from then on they are edited on the Masters screen only (`save_version`) - nothing reads a workbook any more;
  * every change is a NEW version with who / why / when and a full copy of the rows (CLAUDE.md 6.12: nothing is
    overwritten; a past business date still resolves against the version that was current then);
  * the setting `masters_source` = 'database' is the one switch. Once it is set, `masters.load_master` never opens a
    file, and `masters.preflight_masters` checks the stored versions instead of a workbook.

Six masters, each a list of rows with fixed logical fields (MASTER_FIELDS): the five lookup masters that were
workbooks, plus the opening cash balance (a Tally export, now a master like the others, with its as-at date and the
file's own grand total kept in the version's meta).

No Streamlit here (CLAUDE.md 8). Functions take a Session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from .db import AppSetting, MasterRow, MasterVersion

logger = logging.getLogger("mop_reco.masters_db")

SETTING_MASTERS_SOURCE = "masters_source"
SOURCE_DATABASE = "database"
SOURCE_EXCEL = "excel"

# master name -> its fields, in column order. The first field(s) identify a row.
MASTER_FIELDS = {
    "store": ("store_name", "store"),
    "mid_axis": ("mid", "store"),
    "mid_paytm": ("mid", "store"),
    "pickup_code": ("pickup_code", "store"),
    "pickup_location": ("pickup_code", "location", "store"),
    "opening_balance": ("cost_centre", "opening"),
}
KEY_FIELDS = {
    "store": ("store_name",), "mid_axis": ("mid",), "mid_paytm": ("mid",),
    "pickup_code": ("pickup_code",), "pickup_location": ("pickup_code", "location"),
    "opening_balance": ("cost_centre",),
}
TITLES = {
    "store": "Store master (site name to store code)",
    "mid_axis": "Axis MID to store",
    "mid_paytm": "Paytm MID to store",
    "pickup_code": "CMS pickup code to store",
    "pickup_location": "Pickup location (a pickup code shared by more than one store)",
    "opening_balance": "Opening cash balance by cost centre",
}
SCREEN_SOURCE = "(edited on screen)"
STORE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,12}$")


class MasterEditError(ValueError):
    """A master edit that breaks a rule. `errors` lists every problem, naming the row and the value."""

    def __init__(self, errors: list):
        self.errors = errors
        super().__init__("; ".join(errors[:6]) + (f" ... (+{len(errors) - 6} more)" if len(errors) > 6 else ""))


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------

def get_setting(session: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    row = session.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(session: Session, key: str, value: str, who: Optional[str] = None) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value, updated_at=datetime.now(), updated_by=who))
    else:
        row.value, row.updated_at, row.updated_by = value, datetime.now(), who
    session.commit()


def in_database(session) -> bool:
    """True once the masters have been moved into the database. From then on no master workbook is ever read."""
    if session is None:
        return False
    try:
        return get_setting(session, SETTING_MASTERS_SOURCE) == SOURCE_DATABASE
    except Exception as exc:  # noqa: BLE001 - a database without the table yet is, by definition, not switched over
        logger.warning("masters_db.in_database: could not read the masters_source setting (%s: %s); treating the masters as not in the database",
                       type(exc).__name__, exc)
        session.rollback()
        return False


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def current_version(session: Session, name: str) -> Optional[MasterVersion]:
    return (session.query(MasterVersion).filter_by(master_name=name, is_current=True)
            .order_by(MasterVersion.loaded_at.desc()).first())


def version_rows(session: Session, version_id: int) -> list:
    return [json.loads(r.row_json) for r in
            session.query(MasterRow).filter_by(master_version_id=version_id).order_by(MasterRow.id).all()]


def current_rows(session: Session, name: str) -> list:
    v = current_version(session, name)
    return version_rows(session, v.id) if v is not None else []


def history(session: Session, name: str) -> list:
    """Every version, newest first: id, when, who, why, where from, rows, current?"""
    out = []
    for v in (session.query(MasterVersion).filter_by(master_name=name)
              .order_by(MasterVersion.loaded_at.desc(), MasterVersion.id.desc()).all()):
        out.append({"id": v.id, "loaded_at": v.loaded_at, "created_by": v.created_by or "",
                    "change_note": v.change_note or "", "source_kind": v.source_kind or "excel_load",
                    "rows": v.row_count, "current": bool(v.is_current),
                    "source": v.source_file_path})
    return out


def meta_of(session: Session, name: str) -> dict:
    v = current_version(session, name)
    return json.loads(v.meta_json) if v is not None and v.meta_json else {}


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

def _clean_row(name: str, row: dict) -> dict:
    out = {}
    for f in MASTER_FIELDS[name]:
        v = row.get(f)
        if isinstance(v, str):
            v = v.strip()
        out[f] = v
    return out


def _canon(store: str) -> str:
    """Store codes replaced by the company (NLD->NEG, KGP->MKG) count as the same store (CLAUDE.md 6.30)."""
    try:
        from .masters import canonical_store
        return canonical_store(store) or store
    except Exception as exc:  # noqa: BLE001
        logger.warning("store alias lookup failed for %r (%s: %s); the code is used as given", store, type(exc).__name__, exc)
        return store


def _norm_name(v) -> str:
    return " ".join(str(v or "").upper().split())


@dataclass
class Validation:
    errors: list = field(default_factory=list)       # break a rule that an EDIT introduced - the save is refused
    warnings: list = field(default_factory=list)     # a problem the data already had (or a soft issue) - shown, not blocking

    @property
    def ok(self) -> bool:
        return not self.errors


def validate(name: str, rows: list, previous_rows: Optional[list] = None, store_codes: Optional[set] = None) -> Validation:
    """Check `rows` of master `name`. A problem that is ALSO in `previous_rows` (it was already there) is a warning;
    one that the edit introduces is an error. `store_codes` (the store master's codes) turns an unknown store into a
    warning - never an error, because a store the master lacks may still be trading (CHB, CTA)."""
    v = Validation()
    fields = MASTER_FIELDS[name]
    cleaned = [_clean_row(name, r) for r in rows]
    prev_clean = [_clean_row(name, r) for r in (previous_rows or [])]

    def issue(msg: str, preexisting: bool):
        (v.warnings if preexisting else v.errors).append(msg)

    prev_rows_set = {tuple(str(r.get(f)) for f in fields) for r in prev_clean}
    for i, r in enumerate(cleaned, start=1):
        was_there = tuple(str(r.get(f)) for f in fields) in prev_rows_set      # this very row was already stored
        for f in fields:
            if r[f] is None or (isinstance(r[f], str) and r[f] == ""):
                issue(f"row {i}: '{f}' is blank", was_there)
        if name == "opening_balance":
            try:
                float(r["opening"])
            except (TypeError, ValueError):
                issue(f"row {i}: opening '{r['opening']}' is not a number", was_there)
        elif "store" in r and r["store"] and not STORE_CODE_PATTERN.match(str(r["store"])):
            issue(f"row {i}: store code '{r['store']}' must be letters/digits/underscore, up to 12 "
                  "(it is reported as unresolved, never guessed)", was_there)
    if v.errors:
        return v

    key = KEY_FIELDS[name]

    def keyof(r):
        return tuple(_norm_name(r[k]) if (name, k) in {("store", "store_name"), ("pickup_location", "location")}
                     else str(r[k]) for k in key)

    def conflicts(rs):
        seen, bad = {}, {}
        for r in rs:
            k = keyof(r)
            target = r.get("store")
            target = _canon(target) if target else None
            if k in seen and seen[k] != target:
                bad[k] = (seen[k], target)
            seen.setdefault(k, target)
        return bad

    now_bad, was_bad = conflicts(cleaned), conflicts(prev_clean)
    for k, (a, b) in now_bad.items():
        label = " / ".join(k)
        issue(f"'{label}' is listed against two different stores ({a} and {b}) - it would be reported as ambiguous, "
              "never guessed", k in was_bad)

    if name in ("store", "mid_axis", "mid_paytm", "pickup_code", "pickup_location"):
        seen_exact = {}
        for r in cleaned:
            k = (keyof(r), r.get("store"))
            seen_exact[k] = seen_exact.get(k, 0) + 1
        dups = [k for k, n in seen_exact.items() if n > 1]
        prev_seen = {}
        for r in prev_clean:
            k = (keyof(r), r.get("store"))
            prev_seen[k] = prev_seen.get(k, 0) + 1
        for k in dups:
            issue(f"'{' / '.join(k[0])}' appears {seen_exact[k]} times against the same store", prev_seen.get(k, 0) > 1)

    if store_codes is not None and name != "store" and "store" in fields:
        known = {_canon(c) for c in store_codes} | set(store_codes)
        prev_unknown = {r["store"] for r in prev_clean if r.get("store") and r["store"] not in known}
        for st in sorted({r["store"] for r in cleaned if r.get("store") and r["store"] not in known}):
            (v.warnings).append(f"store '{st}' is not in the store master" + (" (already so before this edit)" if st in prev_unknown else ""))
    return v


def diff(previous_rows: list, new_rows: list, name: str) -> dict:
    """What an edit changes, by row key: added, removed, changed (with old/new)."""
    key = KEY_FIELDS[name]

    def index(rows):
        out = {}
        for r in rows:
            c = _clean_row(name, r)
            out.setdefault(tuple(str(c[k]) for k in key), c)
        return out

    a, b = index(previous_rows), index(new_rows)
    added = [b[k] for k in b if k not in a]
    removed = [a[k] for k in a if k not in b]
    changed = [{"key": " / ".join(k), "old": a[k], "new": b[k]} for k in a if k in b and a[k] != b[k]]
    return {"added": added, "removed": removed, "changed": changed,
            "summary": f"{len(added)} added, {len(removed)} removed, {len(changed)} changed"}


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def save_version(session: Session, name: str, rows: list, operator: str, note: str, *,
                 source_kind: str = "screen_edit", meta: Optional[dict] = None, enforce: bool = True):
    """Store `rows` as the new current version of master `name`. Returns (version, created, diff).

    A person must be named and a reason given. Every rule in `validate` that the edit BREAKS refuses the save and
    names the row; a problem that was already there is carried as a warning. Unchanged rows create no new version."""
    if name not in MASTER_FIELDS:
        raise MasterEditError([f"unknown master '{name}'"])
    if enforce and not (operator or "").strip():
        raise MasterEditError(["a name is required: who is making this change?"])
    if enforce and not (note or "").strip():
        raise MasterEditError(["a reason is required: why is this being changed?"])
    previous = current_rows(session, name)
    cleaned = [_clean_row(name, r) for r in rows]
    stores = {r["store"] for r in current_rows(session, "store")} if name != "store" else None
    result = validate(name, cleaned, previous, stores)
    if enforce and not result.ok:
        raise MasterEditError(result.errors)
    from .masters import _upsert_master_version
    if name == "opening_balance" and source_kind in ("screen_edit", "restore"):
        # the file's own Grand Total no longer describes the edited rows: it is replaced by their sum (and said so)
        old_meta = dict(meta if meta is not None else meta_of(session, name))
        old_meta.update({"grand_total": round(sum(float(r["opening"]) for r in cleaned), 2), "edited_on_screen": True,
                         "source_file": SCREEN_SOURCE})
        meta = old_meta
    version, created = _upsert_master_version(
        session, name, cleaned, SCREEN_SOURCE if source_kind == "screen_edit" else f"({source_kind})", None,
        created_by=operator, change_note=note, source_kind=source_kind, meta=meta)
    return version, created, diff(previous, cleaned, name)


def restore_version(session: Session, name: str, version_id: int, operator: str, note: str):
    """Make an older version current again - as a NEW version holding the old rows (history is never rewritten)."""
    old = session.get(MasterVersion, version_id)
    if old is None or old.master_name != name:
        raise MasterEditError([f"version #{version_id} is not a version of '{name}'"])
    return save_version(session, name, version_rows(session, version_id), operator,
                        f"restored version #{version_id}: {note}", source_kind="restore",
                        meta=json.loads(old.meta_json) if old.meta_json else None)


# the upload sources the database now replaces: nothing is uploaded for them any more, they are edited on screen
UPLOAD_KEYS_REPLACED = ("pickup_code_master", "pickup_location_master", "mid_master", "store_master", "opening_cash")


# ---------------------------------------------------------------------------
# the one-time move: Excel -> snapshot file -> database
# ---------------------------------------------------------------------------

def _fingerprint(rows: list) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def read_opening_balance_workbook(config, session) -> tuple:
    """(rows, meta) from the Tally opening-cash workbook, read ONCE for the import. Rows are
    {cost_centre, opening}; meta carries the as-at date (the file's own Period line) and the file's Grand Total."""
    from . import ingest as ing
    from . import io_
    file_path, mtime = io_.latest_file(config, "opening_cash")
    result = io_.read_any(file_path)
    header_idx = io_.find_header_row(result.grid, ing.OPENING_CASH_REQUIRED_COLUMNS)
    header = [str(c).strip() if c is not None else "" for c in result.grid[header_idx]]
    import pandas as pd
    df = pd.DataFrame(result.grid[header_idx + 1:], columns=header)
    grand_total = None
    if "Sub-Ledger" in df.columns:
        totals = df[df["Sub-Ledger"].astype(str).str.strip().eq("Grand Total :")]
        if not totals.empty:
            grand_total = ing._to_float(totals.iloc[0]["Opening"])
    df = df[df["Cost Centre"].notna()].copy()
    df["cost_centre"] = df["Cost Centre"].astype(str).str.strip()
    df = df[df["cost_centre"].ne("") & df["cost_centre"].ne("nan")]
    df["opening_f"] = df["Opening"].map(ing._to_float)
    as_of = ing._opening_period_start(result.grid)
    rows = [{"cost_centre": c, "opening": float(o)} for c, o in zip(df["cost_centre"], df["opening_f"])]
    meta = {"as_at": str(as_of) if as_of else None, "grand_total": grand_total,
            "source_file": str(file_path), "source_modified": mtime.isoformat(sep=" ", timespec="seconds")}
    return rows, meta


def _stored_versions(session, name: str) -> list:
    """Every stored version of a master, oldest first, as plain dicts (rows included)."""
    out = []
    for v in (session.query(MasterVersion).filter_by(master_name=name)
              .order_by(MasterVersion.loaded_at, MasterVersion.id).all()):
        rows = version_rows(session, v.id)
        out.append({"loaded_at": v.loaded_at.isoformat(sep=" "), "created_by": v.created_by,
                    "change_note": v.change_note, "source_kind": v.source_kind or "excel_load",
                    "source": v.source_file_path,
                    "source_mtime": v.source_file_mtime.isoformat(sep=" ") if v.source_file_mtime else None,
                    "meta": json.loads(v.meta_json) if v.meta_json else None,
                    "rows": rows, "fingerprint": _fingerprint([_clean_row(name, r) for r in rows])})
    return out


def build_snapshot(config, session, prefer: str = "excel") -> dict:
    """Everything the database needs to take over from Excel, as one JSON-able dict, with a report of what was
    decided for each master. Each master carries ALL its stored versions (a correction made on 19 Nov must not
    rewrite how October resolves - CLAUDE.md 6.12); then, if `prefer='excel'` and the workbook passes the checks and
    differs from the newest stored version, the workbook is added as one more version dated now. A workbook that is
    unusable (the pickup-location file has lost its LOCATION column) is not used; the stored versions stand and the
    report says so. Nothing is written anywhere."""
    from . import masters as m
    snap = {"created_at": datetime.now().isoformat(sep=" ", timespec="seconds"), "masters": {}, "report": []}
    for name in m.MASTER_SPECS:
        versions = _stored_versions(session, name)
        stored = versions[-1]["rows"] if versions else []
        note = f"{name}: {len(versions)} stored version(s), newest {len(stored)} rows"
        try:
            rows, path, mtime = m._read_master_rows(config, name)
            check = m.preflight_master(config, name)
            if check.problems:
                raise m.MasterLoadError("; ".join(check.problems))
            same = bool(stored) and _fingerprint([_clean_row(name, r) for r in rows]) == _fingerprint(
                [_clean_row(name, r) for r in stored])
            if prefer == "excel":
                if same:
                    note += f" - workbook {path.name} identical to the newest"
                else:
                    summary = f" ({diff(stored, rows, name)['summary']})" if stored else ""
                    versions.append({"loaded_at": datetime.now().isoformat(sep=" "), "created_by": "import",
                                     "change_note": f"workbook {path.name} as at {mtime:%Y-%m-%d %H:%M} differed from the stored version" + summary,
                                     "source_kind": "excel_import", "source": str(path),
                                     "source_mtime": mtime.isoformat(sep=" "), "meta": None, "rows": rows,
                                     "fingerprint": _fingerprint([_clean_row(name, r) for r in rows])})
                    note += f" - workbook {path.name} DIFFERS ({len(rows)} rows), added as the newest version"
        except Exception as exc:  # noqa: BLE001 - say why the workbook was not used
            note += f" - workbook not usable ({str(exc)[:150]}): the stored versions stand"
        snap["masters"][name] = {"versions": versions, "row_count": len(versions[-1]["rows"]) if versions else 0,
                                 "fingerprint": versions[-1]["fingerprint"] if versions else _fingerprint([])}
        snap["report"].append(note)
    rows, meta = read_opening_balance_workbook(config, session)
    fp = _fingerprint([_clean_row("opening_balance", r) for r in rows])
    snap["masters"]["opening_balance"] = {
        "versions": [{"loaded_at": datetime.now().isoformat(sep=" "), "created_by": "import",
                      "change_note": "opening balance workbook", "source_kind": "excel_import",
                      "source": meta["source_file"], "source_mtime": meta["source_modified"],
                      "meta": meta, "rows": rows, "fingerprint": fp}],
        "row_count": len(rows), "fingerprint": fp}
    snap["report"].append(f"opening_balance: {len(rows)} cost centres as at {meta['as_at']}, file grand total {meta['grand_total']}")
    snap["snapshot_fingerprint"] = _fingerprint({k: v["fingerprint"] for k, v in snap["masters"].items()})
    return snap


def import_snapshot(session: Session, snap: dict, operator: str, note: str = "") -> list:
    """Load a snapshot into this database: every version of every master, in order and with its ORIGINAL date (so a
    past business date still resolves against the version current then), then VERIFY by reading each master back,
    and only then switch `masters_source` to 'database'. Idempotent: a master whose newest version already holds
    exactly the snapshot's newest rows is left alone. Returns report lines."""
    from .masters import _upsert_master_version
    if not (operator or "").strip():
        raise MasterEditError(["a name is required: who is running the import?"])
    lines = []
    for name, entry in snap["masters"].items():
        versions = entry["versions"]
        for ver in versions:
            if _fingerprint([_clean_row(name, r) for r in ver["rows"]]) != ver["fingerprint"]:
                raise MasterEditError([f"{name}: the snapshot file is damaged (a version's fingerprint does not match its rows)"])
        have = current_version(session, name)
        if have is not None and _fingerprint([_clean_row(name, r) for r in version_rows(session, have.id)]) == entry["fingerprint"]:
            lines.append(f"{name}: already present ({have.row_count} rows, version #{have.id}) - unchanged")
            continue
        created = 0
        for ver in versions:
            when = datetime.fromisoformat(ver["loaded_at"])
            mtime = datetime.fromisoformat(ver["source_mtime"]) if ver.get("source_mtime") else None
            _v, was_created = _upsert_master_version(
                session, name, [_clean_row(name, r) for r in ver["rows"]], ver.get("source") or "(snapshot)", mtime,
                loaded_at=when, created_by=ver.get("created_by") or operator,
                change_note=(ver.get("change_note") or "") + (f" | {note}" if note else "") + f" | imported by {operator}",
                source_kind=ver.get("source_kind") or "snapshot_import", meta=ver.get("meta"))
            created += int(was_created)
        back = current_rows(session, name)
        if _fingerprint([_clean_row(name, r) for r in back]) != entry["fingerprint"]:
            raise MasterEditError([f"{name}: the rows read back from the database differ from the snapshot"])
        lines.append(f"{name}: {created} version(s) imported with their original dates; newest {len(back)} rows verified by reading back")
    set_setting(session, SETTING_MASTERS_SOURCE, SOURCE_DATABASE, operator)
    lines.append("masters_source = database: from now on no master workbook is read")
    return lines


# ---------------------------------------------------------------------------
# the run's pre-flight when the masters are in the database
# ---------------------------------------------------------------------------

def preflight_checks(session: Session) -> list:
    """One `masters.MasterCheck` per lookup master, from the STORED versions: it must exist, have rows, and carry
    no rule violation (existing ambiguities are warnings - they are reported as ambiguous at resolution, never
    guessed). Replaces the workbook check once the masters are in the database."""
    from .masters import MASTER_SPECS, MasterCheck
    store_codes = {r["store"] for r in current_rows(session, "store")}
    checks = []
    for name in MASTER_SPECS:
        c = MasterCheck(master=name, columns_required={f: [f] for f in MASTER_FIELDS[name]})
        v = current_version(session, name)
        if v is None:
            c.problems.append(f"{name}: no version stored in the database - import the masters first")
            checks.append(c)
            continue
        rows = version_rows(session, v.id)
        c.file = f"database version #{v.id} ({v.source_kind or 'excel_load'}, {v.created_by or 'system'})"
        c.modified = v.loaded_at.isoformat(sep=" ", timespec="seconds")
        c.sheet = None
        c.rows = len(rows)
        c.stored_rows = len(rows)
        c.columns_found = list(MASTER_FIELDS[name])
        if not rows:
            c.problems.append(f"{name}: the stored version has no rows")
        res = validate(name, rows, rows, store_codes if name != "store" else None)
        c.warnings.extend(res.warnings[:20])
        c.problems.extend(f"{name}: {e}" for e in res.errors[:20])
        checks.append(c)
    return checks
