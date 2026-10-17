"""Master data loaders — Stage 1.

Masters that actually exist today (confirmed 2026-08-24, see
docs/DATA_INVENTORY.md and CLAUDE.md section 5):

  - pickup_code     `5.CMS DEPOSIT MASTER\\CASH DEPOSIT MASTER.xlsx`  (PICKCODE -> CODE)
  - pickup_location `5a. Pickup location master\\PICKUP LOCATION MASTER.xlsx`
                    (PICKCODE + LOCATION -> CODE) — added 2026-09-12, hand-maintained,
                    only for a pickup code the agency shares between more than one
                    store (defect A). Every other pickup code is untouched.
  - mid_axis      `6.MID WISE STORE CODE MASTER\\...xlsx` sheet "Axis mid wise store"
  - mid_paytm     `6.MID WISE STORE CODE MASTER\\...xlsx` sheet "Paytm Mid wise store code"

No store master exists (store code, store name, cluster, active flag, CMS
cycle) — confirmed absent, not just unsupplied. Where a store master would
normally answer something, this module derives a provisional, non-authoritative
fallback from the POS dump instead — see `derive_provisional_store_master`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import openpyxl

from .config import Config
from .db import MasterRow, MasterVersion


logger = logging.getLogger("mop_reco")


class MasterLoadError(ValueError):
    """A master file is missing a required field under every alias, or can't be read."""


# ---------------------------------------------------------------------------
# Alias maps — CLAUDE.md section 5. Every logical field has a list of
# acceptable column names. Never guess a column by position; if none of a
# field's aliases are present, raise, naming the file and the missing field.
# ---------------------------------------------------------------------------

PICKUP_CODE_ALIASES = {
    "pickup_code": ["PICKCODE"],
    "store": ["CODE"],
}

# Pickup codes the agency shares between more than one store — see
# resolve_pickup_store() below. Deliberately separate from PICKUP_CODE_ALIASES:
# a pickup code NOT in this master's rows resolves on the code alone exactly
# as it always has, so adding an entry here can only ever affect the specific
# code it names.
PICKUP_LOCATION_ALIASES = {
    "pickup_code": ["PICKCODE"],
    "location": ["LOCATION"],
    "store": ["CODE"],
}

AXIS_MID_ALIASES = {
    "mid": ["MID"],
    "store": ["Store Code"],
}

PAYTM_MID_ALIASES = {
    "mid": ["MID"],
    "store": ["Store Code"],
}

# The store master, supplied 2026-09-10 — full site name -> short store code.
# 'SITES NAMES' is the name the Tally cost centre uses; 'Store Code' is what
# POS, PTC and every gateway report carry. 'CURRENTLY ACTICVE' (the file's own
# spelling) duplicates Store Code on all 214 rows and is not read.
STORE_MASTER_ALIASES = {
    "store_name": ["SITES NAMES", "SITE NAMES", "Site Name"],
    "store": ["Store Code", "STORE CODE"],
}

# master_name -> where to read it from and how to interpret its columns.
# `sheet: None` means "the workbook's first sheet".
MASTER_SPECS = {
    "pickup_code": {
        "source": "pickup_code_master",
        "sheet": None,
        "aliases": PICKUP_CODE_ALIASES,
    },
    "pickup_location": {
        "source": "pickup_location_master",
        "sheet": None,
        "aliases": PICKUP_LOCATION_ALIASES,
    },
    "mid_axis": {
        "source": "mid_master",
        "sheet": "Axis mid wise store",
        "aliases": AXIS_MID_ALIASES,
    },
    "mid_paytm": {
        "source": "mid_master",
        "sheet": "Paytm Mid wise store code",
        "aliases": PAYTM_MID_ALIASES,
    },
    "store": {
        "source": "store_master",
        "sheet": None,
        "aliases": STORE_MASTER_ALIASES,
    },
}

# Which resolve_store() source name maps to which master, and which of its
# logical fields carries the identifier being resolved.
SOURCE_TO_MASTER = {
    "paytm_mid": ("mid_paytm", "mid"),
    "axis_mid": ("mid_axis", "mid"),
    "pickup_code": ("pickup_code", "pickup_code"),
    "store_name": ("store", "store_name"),
}

# (master_name, key_field) pairs whose identifier is matched after normalising.
# Only the store name needs it: the same site is written
# 'CITYKART STORES PVT. LTD. (Anantnag-ANG)' in the store master and
# 'CITYKART STORES PVT. LTD. (ANANTNAG-ANG)' in the Tally cost centre, and one
# name carries a stray inner space — '( ETAWAH-ETW)'. Matching raw strings
# resolves 154 of 214 cost centres; normalising resolves 193, and every one of
# the 39 gained differs by case alone.
#
# Normalising is NOT fuzzy matching. Case, surrounding whitespace and repeated
# spaces are collapsed and then the comparison is EXACT. Nothing is scored,
# ranked or guessed at, and a name that does not match is reported by name.
NORMALISED_KEYS = {("store", "store_name"), ("pickup_location", "location")}


def normalise_name(value) -> str:
    """Upper-case, trimmed, repeated whitespace collapsed to one space."""
    return re.sub(r"\s+", " ", str(value)).strip().upper()


# ---------------------------------------------------------------------------
# Reading a master file into rows, honouring the alias map
# ---------------------------------------------------------------------------

def _resolve_alias(header: list, logical_field: str, aliases: list, file_label: str) -> str:
    for candidate in aliases:
        if candidate in header:
            return candidate
    raise MasterLoadError(
        f"{file_label}: none of the acceptable column names {aliases} were found "
        f"for field '{logical_field}' (header seen: {header})"
    )


def _most_recent_file(config: Config, source_name: str) -> Path:
    status = config.source_status(source_name)
    if not status.files:
        raise MasterLoadError(f"No usable files found for source '{source_name}'")
    # File selection rule, CLAUDE.md section 4: most recently modified wins.
    return max(status.files, key=lambda p: p.stat().st_mtime)


def _read_master_rows(config: Config, master_name: str):
    """Returns (rows, file_path, file_mtime). Read-only — never modifies the source."""
    spec = MASTER_SPECS[master_name]
    file_path = _most_recent_file(config, spec["source"])

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sheet_name = spec["sheet"] or wb.sheetnames[0]
    if sheet_name not in wb.sheetnames:
        raise MasterLoadError(
            f"{file_path.name}: expected sheet '{sheet_name}' not found "
            f"(sheets present: {wb.sheetnames})"
        )
    ws = wb[sheet_name]
    it = ws.iter_rows(values_only=True)
    header = list(next(it))

    file_label = f"{file_path.name} [{sheet_name}]"
    col_index = {}
    for logical_field, aliases in spec["aliases"].items():
        actual_col = _resolve_alias(header, logical_field, aliases, file_label)
        col_index[logical_field] = header.index(actual_col)

    rows = []
    for raw in it:
        if raw is None or all(v is None for v in raw):
            continue
        row = {field: raw[idx] for field, idx in col_index.items()}
        if any(v is None for v in row.values()):
            continue
        rows.append(row)

    mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
    return rows, file_path, mtime


# ---------------------------------------------------------------------------
# Pre-flight — the schema guard for the MASTERS (2026-10-13)
#
# A monthly source already fails loudly on a missing column, because its loader
# raises while the run reads it. A master did not get that treatment in time: a
# master is read only when something first needs it, and the pickup-location
# master is first needed by the CMS stage - which runs AFTER R1 has matched and
# persisted. On 2026-10-08 that file lost its LOCATION column (it was overwritten
# with a copy of the pickup-code list); nothing noticed until the test suite
# failed days later. LOCATION is what splits pickup code AX31918 between APM and
# MMR, so the failure was one step from silently putting APM's credits on MMR.
#
# `preflight_masters` therefore runs FIRST in every run, before any source is
# read, and is read-only (it never snapshots a version). It checks every master
# in MASTER_SPECS - file found, sheet found, every required column present under
# some alias, at least one data row - and reports ALL problems at once, not just
# the first. A problem of that kind fails the run. Softer observations
# (rows added or removed since the stored version, Excel error values such as
# #REF! in a required field, an identifier mapped to two stores) are returned as
# warnings and recorded on the run, never silently absorbed.
# ---------------------------------------------------------------------------

class MasterPreflightError(MasterLoadError):
    """One or more masters cannot be used. The message names every master, file
    and missing field - raised before the run reads anything else."""

    def __init__(self, problems: list, checks: list):
        self.problems = problems
        self.checks = checks
        super().__init__(
            "Master data pre-flight FAILED - nothing was run. "
            + " | ".join(problems)
        )


@dataclass
class MasterCheck:
    master: str
    file: str = None
    modified: str = None
    sheet: str = None
    columns_found: list = field(default_factory=list)
    columns_required: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    rows: int = 0
    stored_rows: int = None
    added: int = None
    removed: int = None
    remapped: int = None
    warnings: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    # Set when the operator has authorised THIS run to use the stored version of a master whose file on disk
    # is unusable (2026-10-17): {"reason", "file_modified", "header_seen", "stored_version", "stored_loaded_at"}.
    stored_in_force: dict = None

    @property
    def ok(self) -> bool:
        return not self.problems


_EXCEL_ERRORS = ("#REF!", "#N/A", "#VALUE!", "#NAME?", "#DIV/0!", "#NULL!", "#NUM!")


def preflight_master(config: Config, master_name: str, stored_rows: list = None) -> MasterCheck:
    """Read-only check of one master file. `stored_rows` (the rows of the version
    currently stored for this master) lets it report what changed."""
    spec = MASTER_SPECS[master_name]
    check = MasterCheck(master=master_name,
                        columns_required={f: a for f, a in spec["aliases"].items()})
    try:
        file_path = _most_recent_file(config, spec["source"])
    except MasterLoadError as exc:
        check.problems.append(f"{master_name}: {exc}")
        return check
    check.file = str(file_path)
    check.modified = datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(sep=" ", timespec="seconds")

    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    except Exception as exc:                                   # unreadable / not a workbook
        check.problems.append(f"{master_name}: {file_path.name} cannot be opened ({type(exc).__name__}: {exc})")
        return check
    try:
        sheet_name = spec["sheet"] or wb.sheetnames[0]
        check.sheet = sheet_name
        if sheet_name not in wb.sheetnames:
            check.problems.append(
                f"{master_name}: {file_path.name} has no sheet '{sheet_name}' (sheets present: {wb.sheetnames})")
            return check
        it = wb[sheet_name].iter_rows(values_only=True)
        header = list(next(it, []) or [])
        check.columns_found = [h for h in header if h is not None]
        index = {}
        for logical, aliases in spec["aliases"].items():
            hit = next((a for a in aliases if a in header), None)
            if hit is None:
                check.missing.append(logical)
                check.problems.append(
                    f"{master_name}: {file_path.name} [{sheet_name}] has lost required column "
                    f"{aliases} (field '{logical}'); header seen: {check.columns_found}")
            else:
                index[logical] = header.index(hit)
        if check.missing:
            return check

        rows = []
        for raw in it:
            if raw is None or all(v is None for v in raw):
                continue
            row = {f: raw[i] for f, i in index.items()}
            if any(v is None for v in row.values()):
                continue
            rows.append(row)
        check.rows = len(rows)
        if not rows:
            check.problems.append(f"{master_name}: {file_path.name} [{sheet_name}] has no data rows")
            return check
    finally:
        wb.close()

    # --- softer observations ------------------------------------------------
    errors = sorted({str(v) for r in rows for v in r.values() if str(v).strip() in _EXCEL_ERRORS})
    if errors:
        n = sum(1 for r in rows if any(str(v).strip() in _EXCEL_ERRORS for v in r.values()))
        check.warnings.append(f"{master_name}: {n} row(s) carry an Excel error value {errors} in a required field")

    key_field = next(iter(spec["aliases"]))
    if master_name == "pickup_location":
        keyf = lambda r: (str(r["pickup_code"]).strip(), normalise_name(r["location"]))
    else:
        norm = (master_name, key_field) in NORMALISED_KEYS
        keyf = lambda r: normalise_name(r[key_field]) if norm else str(r[key_field]).strip()
    mapped: dict = {}
    for r in rows:
        mapped.setdefault(keyf(r), set()).add(str(r["store"]).strip())
    clash = {k: sorted(v) for k, v in mapped.items() if len(v) > 1}
    if clash:
        check.warnings.append(
            f"{master_name}: {len(clash)} identifier(s) map to more than one store and will be "
            f"reported as ambiguous, never guessed: {dict(list(clash.items())[:5])}")

    if stored_rows is not None:
        check.stored_rows = len(stored_rows)
        old: dict = {}
        for r in stored_rows:
            old.setdefault(keyf(r), set()).add(str(r["store"]).strip())
        check.added = len(set(mapped) - set(old))
        check.removed = len(set(old) - set(mapped))
        check.remapped = sum(1 for k in set(mapped) & set(old) if mapped[k] != old[k])
        if check.added or check.removed or check.remapped:
            check.warnings.append(
                f"{master_name}: differs from the stored version - {check.added} added, "
                f"{check.removed} removed, {check.remapped} re-mapped identifier(s)")
    return check


# A master named here is read from its STORED version instead of its file, for the duration of one run - and only
# because an operator said so, with a reason (2026-10-17). Masters are effective-dated by design (6.12), so the stored
# version is a legitimate thing to use; the file on disk is what is broken. The preflight goes back to blocking the
# moment a run is started without this.
_STORED_IN_FORCE: dict = {}


def set_stored_masters_in_force(mapping: dict):
    """{master_name: reason}. Cleared with an empty dict (or `clear_stored_masters_in_force`)."""
    _STORED_IN_FORCE.clear()
    _STORED_IN_FORCE.update(mapping or {})


def clear_stored_masters_in_force():
    _STORED_IN_FORCE.clear()


def preflight_masters(config: Config, session=None, raise_on_failure: bool = True, use_stored: dict = None) -> list:
    """Check every master. Returns the list of MasterCheck; raises
    MasterPreflightError (naming every problem) unless raise_on_failure=False.

    `use_stored` ({master_name: reason}) authorises a master whose FILE fails the check to be replaced, for this run
    only, by the version already stored in the database - recorded on the check (file, modified date, header seen,
    stored version) so the run's notes and the Masters page say so. A master with no stored version still fails."""
    use_stored = use_stored or {}
    checks = []
    for name in MASTER_SPECS:
        stored = None
        version = None
        if session is not None:
            version = (session.query(MasterVersion)
                       .filter_by(master_name=name, is_current=True).first())
            if version is not None:
                stored = [json.loads(r.row_json) for r in
                          session.query(MasterRow).filter_by(master_version_id=version.id).all()]
        check = preflight_master(config, name, stored)
        if name in use_stored and check.problems and version is not None:
            check.stored_in_force = {
                "reason": use_stored[name], "file": check.file, "file_modified": check.modified,
                "header_seen": check.columns_found, "problems": list(check.problems),
                "stored_version": version.id, "stored_loaded_at": str(version.loaded_at),
                "stored_rows": version.row_count}
            check.warnings.append(
                f"{name}: the file on disk is unusable ({'; '.join(check.problems)}) - THIS RUN USES THE STORED "
                f"VERSION #{version.id} (loaded {version.loaded_at:%Y-%m-%d %H:%M}, {version.row_count} rows) by "
                f"instruction: {use_stored[name]}")
            check.problems = []
        checks.append(check)
    problems = [p for c in checks for p in c.problems]
    if problems and raise_on_failure:
        raise MasterPreflightError(problems, checks)
    return checks


def preflight_summary(checks: list) -> dict:
    """A JSON-safe summary for Run.notes."""
    return {c.master: {"file": c.file, "modified": c.modified, "sheet": c.sheet, "rows": c.rows,
                       "stored_rows": c.stored_rows, "added": c.added, "removed": c.removed,
                       "remapped": c.remapped, "warnings": c.warnings, "problems": c.problems,
                       "stored_in_force": c.stored_in_force}
            for c in checks}


# ---------------------------------------------------------------------------
# Versioning — amendment C / CLAUDE.md section 6.12. Masters are flat, undated
# files; effective-dating is this module's job, not the file's. Every load is
# snapshotted; nothing is ever overwritten in place.
# ---------------------------------------------------------------------------

def _content_hash(rows: list) -> str:
    canonical = json.dumps(rows, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _upsert_master_version(
    session,
    master_name: str,
    rows: list,
    source_file_path: str,
    source_file_mtime: Optional[datetime],
    loaded_at: Optional[datetime] = None,
):
    """Snapshot `rows` as a new version unless they're identical to the current
    one. Returns (MasterVersion, created: bool). created=False means the file
    hasn't changed since the last load — no duplicate version is written."""
    loaded_at = loaded_at or datetime.now()
    new_hash = _content_hash(rows)

    current = (
        session.query(MasterVersion)
        .filter_by(master_name=master_name, is_current=True)
        .order_by(MasterVersion.loaded_at.desc())
        .first()
    )
    if current is not None and current.content_hash == new_hash:
        return current, False

    if current is not None:
        # Never overwritten — closed off and left in place for history.
        current.is_current = False
        current.effective_to = loaded_at

    version = MasterVersion(
        master_name=master_name,
        source_file_path=str(source_file_path),
        source_file_mtime=source_file_mtime,
        loaded_at=loaded_at,
        effective_to=None,
        row_count=len(rows),
        is_current=True,
        content_hash=new_hash,
    )
    session.add(version)
    session.flush()  # assign version.id
    for row in rows:
        session.add(MasterRow(master_version_id=version.id, row_json=json.dumps(row, default=str)))
    session.commit()

    # A new version changes what "as of" a future date resolves to (anything
    # from `loaded_at` onward now sees this version instead of falling back
    # further back or hitting the old current one) — drop cached answers for
    # this master so nothing stale survives a reload. The rows/grouped caches
    # don't need clearing: they're keyed by version.id, which never changes
    # for an existing version.
    db_url = str(session.bind.url)
    stale_keys = [k for k in _VERSION_CACHE if k[0] == db_url and k[1] == master_name]
    for k in stale_keys:
        del _VERSION_CACHE[k]

    return version, True


def load_master(config: Config, session, master_name: str):
    """Read the real file for `master_name` and version it. Returns (MasterVersion, created)."""
    if master_name not in MASTER_SPECS:
        raise MasterLoadError(f"Unknown master '{master_name}'")
    try:
        rows, file_path, mtime = _read_master_rows(config, master_name)
    except MasterLoadError:
        if master_name not in _STORED_IN_FORCE:
            raise
        current = (session.query(MasterVersion).filter_by(master_name=master_name, is_current=True)
                   .order_by(MasterVersion.loaded_at.desc()).first())
        if current is None:
            raise
        logger.warning(f"{master_name}: file unusable - using stored version #{current.id} "
                       f"(authorised for this run: {_STORED_IN_FORCE[master_name]})")
        return current, False
    return _upsert_master_version(session, master_name, rows, str(file_path), mtime)


def load_all_masters(config: Config, session):
    """Load every known master. Returns {master_name: (MasterVersion, created)}."""
    return {name: load_master(config, session, name) for name in MASTER_SPECS}


def master_label(master_name: str) -> str:
    """"pickup_location" -> "Pickup location master"."""
    return master_name.replace("_", " ").capitalize() + " master"


def load_all_masters_safe(config: Config, session):
    """Like `load_all_masters`, but one bad master never stops the others (2026-10-14).

    Each master is checked first (`preflight_master`: file, sheet, every required column) and
    only loaded if it passes. Returns ({master_name: (version, created)}, {master_name: message}).
    A master that fails is reported by a plain message - "Pickup location master cannot be
    loaded - LOCATION column missing" - never a traceback, and the stored version already in
    the database stays in force; nothing is guessed or inferred to fill the gap."""
    loaded, failed = {}, {}
    for name in MASTER_SPECS:
        check = preflight_master(config, name)
        if check.missing:
            cols = ", ".join(str(MASTER_SPECS[name]["aliases"][m][0]) for m in check.missing)
            failed[name] = (f"{master_label(name)} cannot be loaded - {cols} column missing "
                            f"(header on disk: {', '.join(map(str, check.columns_found))}; "
                            f"file modified {check.modified})")
            continue
        if check.problems:
            failed[name] = f"{master_label(name)} cannot be loaded - " + "; ".join(check.problems)
            continue
        try:
            loaded[name] = load_master(config, session, name)
        except MasterLoadError as exc:
            failed[name] = f"{master_label(name)} cannot be loaded - {exc}"
    return loaded, failed


# A master version's rows are immutable once written (a change always creates
# a NEW version — see _upsert_master_version). So the parsed rows for a given
# version.id can be cached for the life of the process: without this, calling
# resolve_store() in a loop over a whole settlement file re-fetches and
# re-JSON-parses the entire master from the DB on every single row, which is
# the difference between milliseconds and minutes on ~17,000 rows. Cache keys
# include the DB's own URL so tests using separate throwaway SQLite files
# never collide on a reused autoincrement id.
_ROWS_CACHE: dict = {}
_GROUPED_CACHE: dict = {}
# The version-lookup query itself is cheap in isolation, but resolve_store()
# is called once per row of a settlement file (tens of thousands of times per
# run against real data) with the *same* (master_name, as_of) every time — so
# even a cheap query becomes the dominant cost at that volume. Cached here too.
_VERSION_CACHE: dict = {}


def _find_version_asof(session, master_name: str, as_of):
    if isinstance(as_of, date) and not isinstance(as_of, datetime):
        # End-of-day, not start-of-day: a version loaded at any point during
        # `as_of` is in effect for that business date, not only one loaded
        # before midnight at its start.
        as_of = datetime.combine(as_of, datetime.max.time())

    cache_key = (str(session.bind.url), master_name, as_of)
    if cache_key in _VERSION_CACHE:
        return _VERSION_CACHE[cache_key]

    version = (
        session.query(MasterVersion)
        .filter(MasterVersion.master_name == master_name, MasterVersion.loaded_at <= as_of)
        .order_by(MasterVersion.loaded_at.desc())
        .first()
    )
    if version is None:
        # as_of predates every version we've ever loaded — fall back to the
        # earliest, since that's the only master data that has ever existed
        # in this system (CLAUDE.md section 6.12).
        version = (
            session.query(MasterVersion)
            .filter(MasterVersion.master_name == master_name)
            .order_by(MasterVersion.loaded_at.asc())
            .first()
        )
    if version is None:
        raise MasterLoadError(f"No version of master '{master_name}' has ever been loaded")
    _VERSION_CACHE[cache_key] = version
    return version


def get_rows_asof(session, master_name: str, as_of):
    """The version of `master_name` that applied as of `as_of` (date or datetime)
    — never just the newest — and its rows."""
    version = _find_version_asof(session, master_name, as_of)
    cache_key = (str(session.bind.url), master_name, version.id)
    if cache_key not in _ROWS_CACHE:
        _ROWS_CACHE[cache_key] = [
            json.loads(r.row_json)
            for r in session.query(MasterRow).filter_by(master_version_id=version.id).all()
        ]
    return version, _ROWS_CACHE[cache_key]


def _grouped_by_key(session, master_name: str, as_of, key_field: str):
    """rows for `master_name` as of `as_of`, grouped {identifier: {stores}} on
    `key_field`. Cached per version — this is what makes resolve_store() and
    check_mid_integrity() O(1) per lookup instead of rescanning every row."""
    version = _find_version_asof(session, master_name, as_of)
    cache_key = (str(session.bind.url), master_name, version.id, key_field)
    if cache_key not in _GROUPED_CACHE:
        _, rows = get_rows_asof(session, master_name, as_of)
        normalise = (master_name, key_field) in NORMALISED_KEYS
        grouped: dict = {}
        for row in rows:
            key = (normalise_name(row[key_field]) if normalise
                   else str(row[key_field]).strip())
            grouped.setdefault(key, set()).add(str(row["store"]).strip())
        _GROUPED_CACHE[cache_key] = grouped
    return version, _GROUPED_CACHE[cache_key]


def version_history(session, master_name: str):
    return (
        session.query(MasterVersion)
        .filter_by(master_name=master_name)
        .order_by(MasterVersion.loaded_at.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# Resolution — the single entry point every later stage uses to turn an
# identifier into a store. Never guesses; ambiguity is reported, not resolved.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolveResult:
    source: str
    identifier: str
    store: Optional[str]
    status: str  # "resolved" | "unmapped" | "store_ambiguous" | "location_unresolved"
    candidates: tuple = ()  # populated only when status == "store_ambiguous"
    via_later_version: Optional[str] = None  # set when the identifier was absent as at the date (see below)


def resolve_store(
    session,
    source: str,
    identifier,
    business_date,
    pos_store_codes: Optional[set] = None,
) -> ResolveResult:
    """source in {"pos", "paytm_mid", "axis_mid", "pickup_code"}.

    MID and pickup-code resolution is one-to-one by design. If the master
    version in effect on `business_date` maps the identifier to more than one
    store, that's `store_ambiguous` — never a guess at either candidate
    (CLAUDE.md section 5, Axis EDC resolver).
    """
    identifier_str = str(identifier).strip()

    if source == "pos":
        # No store master exists (confirmed 2026-08-24). The only fallback is
        # the provisional list derived from the POS dump itself — see
        # derive_provisional_store_master. This checks membership in that
        # derived list; it is not authoritative.
        if pos_store_codes is not None and identifier_str in pos_store_codes:
            return ResolveResult(source, identifier_str, identifier_str, "resolved")
        return ResolveResult(source, identifier_str, None, "unmapped")

    if source not in SOURCE_TO_MASTER:
        raise ValueError(f"Unknown resolve_store source '{source}'")

    master_name, key_field = SOURCE_TO_MASTER[source]
    _, grouped = _grouped_by_key(session, master_name, business_date, key_field)
    lookup = (normalise_name(identifier_str)
              if (master_name, key_field) in NORMALISED_KEYS else identifier_str)
    matches = grouped.get(lookup, set())

    if not matches:
        # ABSENCE only (2026-10-17): an identifier the as-at version has never heard of may be one the master
        # learned about later (a store that opened after the last file was loaded, so its September MID is only
        # in an October file). A transaction under that identifier exists, so the store traded; refusing to map it
        # until the next month's master arrives is the bug. A CORRECTION - the identifier present in both
        # versions but mapped to a different store - is never touched by this: the as-at version still wins, so
        # 6.12's rule (a later correction must not rewrite a past resolution) holds.
        later = _first_later_match(session, master_name, key_field, business_date, lookup)
        if later is not None:
            stores, loaded = later
            if len(stores) == 1:
                return ResolveResult(source, identifier_str, next(iter(stores)), "resolved",
                                     via_later_version=loaded)
            return ResolveResult(source, identifier_str, None, "store_ambiguous", tuple(sorted(stores)))
        return ResolveResult(source, identifier_str, None, "unmapped")
    if len(matches) > 1:
        return ResolveResult(
            source, identifier_str, None, "store_ambiguous", tuple(sorted(matches))
        )
    return ResolveResult(source, identifier_str, next(iter(matches)), "resolved")


_LATER_CACHE: dict = {}


def _first_later_match(session, master_name: str, key_field: str, as_of, lookup: str):
    """The earliest version loaded AFTER `as_of` that holds `lookup`, as ({stores}, loaded_at iso), else None."""
    if isinstance(as_of, date) and not isinstance(as_of, datetime):
        as_of = datetime.combine(as_of, datetime.max.time())
    ck = (str(session.bind.url), master_name, key_field, as_of)
    if ck not in _LATER_CACHE:
        normalise = (master_name, key_field) in NORMALISED_KEYS
        merged: dict = {}
        versions = (session.query(MasterVersion)
                    .filter(MasterVersion.master_name == master_name, MasterVersion.loaded_at > as_of)
                    .order_by(MasterVersion.loaded_at.asc()).all())
        for v in versions:
            for r in session.query(MasterRow).filter_by(master_version_id=v.id).all():
                row = json.loads(r.row_json)
                key = (normalise_name(row[key_field]) if normalise else str(row[key_field]).strip())
                merged.setdefault(key, (set(), v.loaded_at.isoformat()))[0].add(str(row["store"]).strip())
        _LATER_CACHE[ck] = merged
    return _LATER_CACHE[ck].get(lookup)


# ---------------------------------------------------------------------------
# Pickup code + location — for the pickup codes the agency shares between
# more than one store (2026-09-12, defect A). The plain pickup_code master
# above is one-to-one and stays that way for every code except the ones
# named here: APM's July credits were being posted entirely under MMR's
# pickup code AX31918, distinguishable only by the CMS file's own LOCAT
# column (ARRAH vs MADHB — confirmed against the store master's own site
# names, "ARRAH-APM" and "Madhubani-2-MMR").
#
# The mechanism is generic on purpose, even though only one code needs it
# today: a code not present in the pickup_location master resolves on the
# code alone, exactly as it always has — nothing changes for the other ~209
# codes, or if the agency starts sharing a different code tomorrow this
# module does not need rebuilding, only a new row in the master.
# ---------------------------------------------------------------------------

def _grouped_pickup_locations(session, as_of):
    """{pickup_code: {location: store}} from the 'pickup_location' master, as
    of `as_of`. Only pickup codes that actually need location to disambiguate
    appear here — every other pickup code is untouched and continues to
    resolve via the plain 'pickup_code' master. Location is matched
    exact-after-normalising, the same way the store name is (NORMALISED_KEYS)
    — not fuzzy matching, just case/whitespace-insensitive."""
    version = _find_version_asof(session, "pickup_location", as_of)
    cache_key = (str(session.bind.url), "pickup_location", version.id, "grouped")
    if cache_key not in _GROUPED_CACHE:
        _, rows = get_rows_asof(session, "pickup_location", as_of)
        grouped: dict = {}
        for row in rows:
            code = str(row["pickup_code"]).strip()
            location = normalise_name(row["location"])
            grouped.setdefault(code, {})[location] = str(row["store"]).strip()
        _GROUPED_CACHE[cache_key] = grouped
    return version, _GROUPED_CACHE[cache_key]


def known_pickup_location_codes(session, as_of) -> set:
    """Which pickup codes the pickup_location master already accounts for —
    used by the CMS pre-flight check to tell a REGISTERED shared code (safe,
    location will decide) from one nobody has told the system about yet
    (must be reported, never silently resolved by picking a store)."""
    _, grouped = _grouped_pickup_locations(session, as_of)
    return set(grouped.keys())


def resolve_pickup_store(session, pickup_code, location, business_date) -> ResolveResult:
    """Pickup-code store resolution, location-aware.

    Where `pickup_code` maps to exactly one store in the plain pickup_code
    master, it resolves on the code alone — exactly as before. Location is
    consulted ONLY for a pickup code registered in the pickup_location master
    (today: AX31918) — where it is, the CMS row's own LOCAT decides between
    the candidate stores. A blank or unrecognised location on a registered
    code is `location_unresolved`, never a guess at either candidate — the
    same "never guess" rule §5 already applies to every other master here.
    """
    identifier_str = str(pickup_code).strip()
    _, locations_by_code = _grouped_pickup_locations(session, business_date)

    if identifier_str not in locations_by_code:
        # Not a code that needs location — resolve exactly as it always has.
        return resolve_store(session, "pickup_code", identifier_str, business_date)

    by_location = locations_by_code[identifier_str]
    location_str = "" if location is None else str(location).strip()
    loc_key = normalise_name(location_str) if location_str else ""
    if not loc_key or loc_key not in by_location:
        return ResolveResult(
            "pickup_code", identifier_str, None, "location_unresolved",
            tuple(sorted(set(by_location.values())))
        )
    return ResolveResult(
        "pickup_code", identifier_str, by_location[loc_key], "resolved")


def pickup_codes_needing_location_registration(cms_df, session, as_of) -> list:
    """Pre-flight check (CLAUDE.md 6.11): a pickup code seen against more than
    one distinct LOCAT value in the data that the pickup_location master does
    not yet know about. This is exactly the gap that let defect A (APM's
    credits posted under MMR's code) go undetected for a month — closing it
    means the next case like it is reported the moment it is ingested, not
    found later by hand because one row's arithmetic looked odd.

    Returns a sorted list of pickup codes needing a location entry added.
    """
    if cms_df is None or cms_df.empty or "LOCAT" not in cms_df.columns:
        return []
    frame = cms_df[cms_df["PICKUPPOINTCODE"].notna() & cms_df["LOCAT"].notna()].copy()
    frame["PICKUPPOINTCODE"] = frame["PICKUPPOINTCODE"].astype(str).str.strip()
    frame["LOCAT"] = frame["LOCAT"].astype(str).str.strip()
    frame = frame[frame["LOCAT"] != ""]
    if frame.empty:
        return []
    variance = frame.groupby("PICKUPPOINTCODE")["LOCAT"].nunique()
    multi_location = set(variance[variance > 1].index)
    known = known_pickup_location_codes(session, as_of)
    return sorted(multi_location - known)


# ---------------------------------------------------------------------------
# Master integrity check — "no MID maps to more than one store as at the
# business date", across both the Paytm and Axis masters (pre-flight check,
# CLAUDE.md section 6.11).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntegrityViolation:
    master_name: str
    mid: str
    stores: tuple


def check_mid_integrity(session, business_date) -> list:
    violations = []
    for master_name in ("mid_paytm", "mid_axis"):
        _, mid_to_stores = _grouped_by_key(session, master_name, business_date, "mid")
        for mid, stores in mid_to_stores.items():
            if len(stores) > 1:
                violations.append(IntegrityViolation(master_name, mid, tuple(sorted(stores))))
    return violations


# ---------------------------------------------------------------------------
# Unmapped report — every identifier that failed to resolve, with how many
# rows and how much value it affects. Never dropped, never guessed.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnmappedEntry:
    source: str
    identifier: str
    row_count: int
    value: float
    status: str  # "unmapped" | "store_ambiguous" | "location_unresolved"


def build_unmapped_report(
    session,
    source: str,
    records: Iterable,
    business_date,
    pos_store_codes: Optional[set] = None,
) -> list:
    """`records` is an iterable of (identifier, amount) pairs — e.g. every row
    of a settlement file keyed by MID. Both unmapped and store_ambiguous count
    here: neither gives a usable store, and both need surfacing, not silence."""
    agg: dict = {}
    for identifier, amount in records:
        result = resolve_store(session, source, identifier, business_date, pos_store_codes=pos_store_codes)
        if result.status == "resolved":
            continue
        key = str(identifier)
        entry = agg.setdefault(key, {"row_count": 0, "value": 0.0, "status": result.status})
        entry["row_count"] += 1
        entry["value"] += float(amount or 0)
        entry["status"] = result.status  # store_ambiguous is the more informative status if seen

    return [
        UnmappedEntry(source=source, identifier=k, row_count=v["row_count"], value=v["value"], status=v["status"])
        for k, v in sorted(agg.items())
    ]


# ---------------------------------------------------------------------------
# Working-day helpers — CLAUDE.md section 6.5. No holiday calendar has been
# supplied (config.yaml: holiday_calendar: null), so every due date computed
# here is weekends-only and must say so on screen, not be assumed correct.
# ---------------------------------------------------------------------------

HOLIDAYS_LOADED = False
_HOLIDAY_SET: frozenset = frozenset()  # populated once a real calendar file exists

DUE_DATE_BASIS_NO_HOLIDAYS = "weekends only, holidays not loaded"
DUE_DATE_BASIS_WITH_HOLIDAYS = "weekends and public holidays"


def is_working_day(d: date) -> bool:
    if d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    if d in _HOLIDAY_SET:
        return False
    return True


@dataclass(frozen=True)
class DueDateResult:
    date: date
    basis: str


def due_date(d: date, cycle_days: int) -> DueDateResult:
    candidate = d + timedelta(days=cycle_days)
    while not is_working_day(candidate):
        candidate += timedelta(days=1)
    basis = DUE_DATE_BASIS_WITH_HOLIDAYS if HOLIDAYS_LOADED else DUE_DATE_BASIS_NO_HOLIDAYS
    return DueDateResult(date=candidate, basis=basis)


# ---------------------------------------------------------------------------
# Provisional store list — used only because no store master exists.
# Derived, not authoritative. See CLAUDE.md section 5, "POS sale — store wise".
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProvisionalStore:
    store: str
    cms_cycle_days: int
    cms_cycle_source: str  # always "cycle_assumed" until a real store master exists


def derive_provisional_store_master(config: Config, file_path=None):
    """Returns (list[ProvisionalStore], note). No store name, cluster or active
    flag is available — only the store code itself, read from the POS bill-wise
    dump's STORE_NAME column. Every CMS cycle defaults to T+1, flagged
    cycle_assumed, per your instruction — never guessed as T+0.

    Goes through io_.read_any, not openpyxl directly: the monthly POS export is
    `.xlsb`, which openpyxl cannot open at all, and it spans several sheets."""
    from . import io_

    if file_path is None:
        file_path = _most_recent_file(config, "pos_bill_wise")
    else:
        file_path = Path(file_path)

    # Prefer the warehouse: this needs one column, and re-opening the 222 MB
    # workbook to read it costs ~340s — which on a page load is a hang.
    from .ingest import read_warehouse, warehouse_manifest

    manifest = warehouse_manifest("pos_bill_wise", file_path)
    if manifest is not None:
        import pandas as pd

        frames = [pd.read_parquet(p, columns=["store"]) for p in manifest["parquet"].values()]
        stores = sorted({str(s).strip() for s in pd.concat(frames)["store"].dropna().unique()})
    else:
        result = io_.read_any(file_path)
        header = [str(c).strip() if c is not None else "" for c in result.grid[0]]
        if "STORE_NAME" not in header:
            raise MasterLoadError(f"{file_path.name}: expected column 'STORE_NAME' not found")
        idx = header.index("STORE_NAME")
        stores = sorted({str(row[idx]).strip() for row in result.grid[1:]
                         if len(row) > idx and row[idx]})

    provisional = [
        ProvisionalStore(store=s, cms_cycle_days=1, cms_cycle_source="cycle_assumed")
        for s in stores
    ]
    note = (
        f"DERIVED from POS bill-wise dump ({file_path.name}) — NOT authoritative. "
        "No store master exists: no store name, cluster or active flag is available, "
        "and every CMS cycle shown here is a T+1 default (cycle_assumed), not a "
        "sourced value."
    )
    return provisional, note
