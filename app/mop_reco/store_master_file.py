"""The store master workbook with Cluster and Region (2026-11-27). Pure: no Streamlit, no database writes.

`parse(path)` reads an .xlsx / .csv by column NAME (case, spacing, underscores ignored; the header row is found in the first rows) and
returns the rows in the store master's own shape {store_name, store, cluster, region}. `compare(parsed, current_rows)` says what saving it
would change. Nothing is guessed: a blank cell stays blank (shown as Unassigned by `store_scope`), a code listed twice is reported, and a
store of the current master that the file leaves out is named - it would be removed, so the person saving must confirm that.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import masters_db, store_scope

# logical field -> header names accepted for it (after _norm)
ALIASES = {
    "store_name": ("sites names", "site names", "sites name", "site name", "sitename", "sitenames", "store name", "storename", "name"),
    "store": ("store code", "storecode", "store", "code"),
    "cluster": ("cluster", "cluster name", "clustername", "cluster names"),
    "region": ("region", "region type", "regiontype", "region name", "regionname"),
}
REQUIRED = ("store", "cluster", "region")           # the site name is optional: a file without it is applied onto the stored store master by code
HEADER_SEARCH_ROWS = 15


def _norm(h) -> str:
    return " ".join(str(h if h is not None else "").replace("_", " ").split()).lower()


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


@dataclass
class ParsedStoreMaster:
    file_name: str = ""
    sha256: str = ""
    rows: list = field(default_factory=list)
    columns_found: dict = field(default_factory=dict)     # logical field -> header as written
    errors: list = field(default_factory=list)            # the file cannot be saved
    warnings: list = field(default_factory=list)
    header_row: int = 0
    mode: str = "full"                                    # "full": rows carry the site name; "merge": Store Code + Cluster + Region only

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.rows)


def _grid(path: Path) -> list:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        for enc in ("utf-8-sig", "cp1252"):
            try:
                with open(path, newline="", encoding=enc) as fh:
                    return [list(r) for r in csv.reader(fh)]
            except UnicodeDecodeError:
                continue
        raise ValueError("the CSV could not be decoded (tried UTF-8 and Windows-1252)")
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        # the first sheet that has a store-code column; otherwise the first sheet (reported as missing columns)
        first = None
        for ws in wb.worksheets:
            grid = [list(r) for r in ws.iter_rows(values_only=True)]
            first = first if first is not None else grid
            if any(_header_map(row).get("store") is not None for row in grid[:HEADER_SEARCH_ROWS]):
                return grid
        return first or []
    finally:
        wb.close()


def _header_map(row) -> dict:
    """logical field -> column index, for a candidate header row."""
    found = {}
    for i, h in enumerate(row):
        n = _norm(h)
        for logical, names in ALIASES.items():
            if logical not in found and n in names:
                found[logical] = i
    return found


def parse(path, file_name: Optional[str] = None) -> ParsedStoreMaster:
    path = Path(path)
    out = ParsedStoreMaster(file_name=file_name or path.name)
    try:
        out.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        grid = _grid(path)
    except Exception as exc:  # noqa: BLE001 - said, never raised into the page
        out.errors.append(f"The file could not be read: {type(exc).__name__}: {exc}")
        return out
    header_at, cols = None, {}
    for i, row in enumerate(grid[:HEADER_SEARCH_ROWS]):
        m = _header_map(row)
        if "store" in m and len(m) > len(cols):
            header_at, cols = i, m
    if header_at is None:
        out.errors.append("No header row with a 'Store Code' column was found in the first rows of the file.")
        return out
    out.header_row = header_at + 1
    out.columns_found = {k: str(grid[header_at][v]).strip() for k, v in cols.items()}
    missing = [f for f in REQUIRED if f not in cols]
    if missing:
        names = {"store": "Store Code", "cluster": "Cluster", "region": "Region"}
        out.errors.append("The file has no column for: " + ", ".join(names[m] for m in missing)
                          + f". Columns found: {', '.join(str(c) for c in grid[header_at] if c not in (None, ''))}.")
        return out
    out.mode = "full" if "store_name" in cols else "merge"
    seen_code: dict = {}
    for n, raw in enumerate(grid[header_at + 1:], start=header_at + 2):
        rec = {f: _text(raw[i]) if i < len(raw) else "" for f, i in cols.items()}
        if not any(rec.values()):
            continue
        if not rec["store"] and not rec["store_name"]:
            continue                                   # a stray cell, not a store
        row = {"store_name": rec.get("store_name", ""), "store": rec["store"], "cluster": rec["cluster"], "region": rec["region"]}
        if out.mode == "merge":
            row.pop("store_name")
        if not row["store"]:
            out.errors.append(f"row {n}: no Store Code" + (f" for '{row['store_name']}'" if out.mode == "full" else ""))
            continue
        if out.mode == "full" and not row["store_name"]:
            out.errors.append(f"row {n}: no site name for store {row['store']}")
            continue
        for f in row:
            if row[f].startswith("#") and row[f].endswith(("!", "?", "A")):       # an Excel error (#REF!, #N/A) that was pasted in
                out.errors.append(f"row {n}: '{f}' holds an Excel error value ({row[f]})")
        seen_code.setdefault(row["store"], []).append((n, row))
        out.rows.append(row)
    for code, hits in seen_code.items():
        if len(hits) > 1:
            attrs = {(r["cluster"], r["region"]) for _, r in hits}
            if len(attrs) > 1:
                out.errors.append(f"store {code} is listed on rows {', '.join(str(n) for n, _ in hits)} with different cluster / region - "
                                  "one store sits in one cluster and one region")
            else:
                out.warnings.append(f"store {code} is listed more than once (rows {', '.join(str(n) for n, _ in hits)}) - "
                                    "it is kept once per site name")
    if not out.rows and not out.errors:
        out.errors.append("The file has a header but no store rows.")
    return out


def summary(rows: list) -> dict:
    """Counts the person checks before saving: stores, regions, clusters, how many have none, and the shape of the hierarchy."""
    h = store_scope.Hierarchy.from_rows(rows)
    regions = h.regions()
    per_region = []
    for rg in regions:
        cl = h.clusters(rg)
        per_region.append({"region": rg, "clusters": len(cl), "stores": len(h.stores(rg))})
    per_cluster = []
    for rg in regions:
        for cl in h.clusters(rg):
            per_cluster.append({"region": rg, "cluster": cl, "stores": len(h.stores(rg, cl))})
    return {
        "rows": len(rows), "stores": len(h.region_of),
        "regions": [r for r in regions if r != store_scope.UNASSIGNED],
        "clusters": len({c for c in h.cluster_of.values() if c != store_scope.UNASSIGNED}),
        "no_region": sorted(s for s, r in h.region_of.items() if r == store_scope.UNASSIGNED),
        "no_cluster": sorted(s for s, c in h.cluster_of.items() if c == store_scope.UNASSIGNED),
        "per_region": per_region, "per_cluster": per_cluster,
        "clusters_in_several_regions": h.clusters_in_several_regions(),
    }


def compare(rows: list, current_rows: list) -> dict:
    """What saving `rows` would do to the stored store master, by store code. `removed` are codes the file leaves out."""
    cur = {}
    for r in current_rows:
        code = str(r.get("store") or "").strip()
        if code:
            cur.setdefault(code, r)
    new = {}
    for r in rows:
        new.setdefault(r["store"], r)
    added = sorted(set(new) - set(cur))
    removed = sorted(set(cur) - set(new))
    moved, renamed = [], []
    for code in sorted(set(new) & set(cur)):
        a, b = cur[code], new[code]
        old_attr = (str(a.get("cluster") or "").strip(), str(a.get("region") or "").strip())
        new_attr = (str(b.get("cluster") or "").strip(), str(b.get("region") or "").strip())
        if old_attr != new_attr:
            moved.append({"store": code, "from": " / ".join(x or "-" for x in old_attr),
                          "to": " / ".join(x or "-" for x in new_attr)})
        if masters_db._norm_name(a.get("store_name")) != masters_db._norm_name(b.get("store_name")):
            renamed.append({"store": code, "from": a.get("store_name"), "to": b.get("store_name")})
    return {"added": added, "removed": removed, "cluster_region_changed": moved, "renamed": renamed,
            "unchanged": len(set(new) & set(cur)) - len(moved) - len(renamed)}


def merge_into_current(rows: list, current_rows: list) -> dict:
    """A file with Store Code, Cluster and Region only (no site names) is applied onto the STORED store master by store code: every
    stored row keeps its site name and takes the file's cluster / region for its code. Store codes replaced by the company (NLD -> NEG,
    KGP -> MKG) are matched through the alias. Nothing is invented and nothing removed:
      merged         the new full list of rows (every stored row, with its cluster / region)
      not_in_master  codes in the file with no stored row - they cannot be added (no site name is known); reported by code
      not_in_file    stored codes the file does not mention - they keep whatever cluster / region they had (none yet = Unassigned)
    """
    def canon(code: str) -> str:
        return masters_db._canon(code) or code

    file_attr = {}
    for r in rows:
        file_attr.setdefault(canon(r["store"]), (r["cluster"], r["region"]))
    stored_codes = {canon(str(r.get("store") or "").strip()) for r in current_rows if str(r.get("store") or "").strip()}
    merged, not_in_file = [], set()
    for r in current_rows:
        code = str(r.get("store") or "").strip()
        new = dict(r)
        if code and canon(code) in file_attr:
            new["cluster"], new["region"] = file_attr[canon(code)]
        elif code:
            not_in_file.add(code)
        merged.append(new)
    not_in_master = sorted(c for c in file_attr if c not in stored_codes)
    return {"merged": merged, "not_in_master": not_in_master, "not_in_file": sorted(not_in_file),
            "applied": len(file_attr) - len(not_in_master)}
