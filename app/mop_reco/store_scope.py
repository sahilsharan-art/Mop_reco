"""Region -> Cluster -> Store (2026-11-27). Pure: no Streamlit, no database.

The dashboard's global filter used to be ONE store. A region or a cluster is a set of stores, so the filter value `store` that every page
passes to the engines can now be:

    None            every store
    "ABD"           one store                  (exactly as before)
    ("ABD", "AGT")  a tuple of stores          (a region or a cluster, resolved from the store master)

`clause` / `mask` / `contains` apply it to a SQL column, a pandas column and a plain value, so each call site changes from `== store` to one
of these and a single store behaves identically. An EMPTY selection is never produced (it would read as "no filter" = every store): the sidebar
stops the page instead.

The hierarchy comes from the store master's `cluster` and `region` fields. A store with a blank region or cluster is kept, under
`UNASSIGNED` - never dropped and never guessed into a region (CLAUDE.md rules 4 and 5). A store the active-store file no longer lists is
stored with region and cluster `CLOSED` (set at upload time, never inferred here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Union

UNASSIGNED = "Unassigned"
CLOSED = "Closed"          # a store of the master that the active-store file no longer lists (store_master_file.merge_into_current)
ALL_REGIONS = "All regions"
ALL_CLUSTERS = "All clusters"
ALL_STORES = "All stores"

StoreScope = Union[None, str, tuple]


def normalize(store) -> StoreScope:
    """None / "" / "All stores" -> None; one code -> that code; several -> a sorted tuple of distinct codes (one code stays a string).
    An empty collection is an error, never "all stores"."""
    if store is None or store == "" or store == ALL_STORES:
        return None
    if isinstance(store, str):
        return store
    codes = sorted({str(s) for s in store if s not in (None, "")})
    if not codes:
        raise ValueError("an empty store selection would be read as every store; refusing")
    return codes[0] if len(codes) == 1 else tuple(codes)


def as_list(store) -> Optional[list]:
    s = normalize(store)
    if s is None:
        return None
    return [s] if isinstance(s, str) else list(s)


def clause(column, store):
    """SQLAlchemy condition for `column` against the scope (the caller has already checked `if store:`)."""
    codes = as_list(store)
    if codes is None:
        raise ValueError("clause() needs a store scope; check `if store:` first")
    return column == codes[0] if len(codes) == 1 else column.in_(codes)


def mask(series, store):
    """pandas boolean mask for a store column against the scope. No scope -> all True."""
    codes = as_list(store)
    if codes is None:
        import pandas as pd
        return pd.Series(True, index=series.index)
    return series == codes[0] if len(codes) == 1 else series.isin(codes)


def contains(store, value) -> bool:
    """Is this one store inside the scope? No scope -> True."""
    codes = as_list(store)
    return True if codes is None else value in codes


def label(store, region: Optional[str] = None, cluster: Optional[str] = None) -> str:
    """Words for the page header: 'all stores', 'ABD only', 'cluster Patna (North), 12 stores', 'region North, 48 stores'."""
    codes = as_list(store)
    if codes is None:
        return "all stores"
    if len(codes) == 1 and not (region or cluster):
        return f"{codes[0]} only"
    if cluster:
        return f"cluster {cluster}, {len(codes)} stores"
    if region:
        return f"region {region}, {len(codes)} stores"
    return f"{len(codes)} stores"


@dataclass
class Hierarchy:
    """store -> (region, cluster), built from the store master rows. Blank region / cluster become UNASSIGNED."""
    region_of: dict = field(default_factory=dict)
    cluster_of: dict = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Iterable[dict]) -> "Hierarchy":
        h = cls()
        for r in rows:
            code = str(r.get("store") or "").strip()
            if not code or code == "None":
                continue
            region = str(r.get("region") or "").strip() or UNASSIGNED
            cluster = str(r.get("cluster") or "").strip() or UNASSIGNED
            h.region_of.setdefault(code, region)         # the first row for a code wins (a code listed twice is flagged by validation)
            h.cluster_of.setdefault(code, cluster)
        return h

    @property
    def has_any(self) -> bool:
        return any(v != UNASSIGNED for v in self.region_of.values()) or any(v != UNASSIGNED for v in self.cluster_of.values())

    def regions(self, stores: Optional[Iterable[str]] = None) -> list:
        pool = self._pool(stores)
        return sorted({self.region_of.get(s, UNASSIGNED) for s in pool}, key=_unassigned_last)

    def clusters(self, region: Optional[str] = None, stores: Optional[Iterable[str]] = None) -> list:
        pool = self._pool(stores)
        return sorted({self.cluster_of.get(s, UNASSIGNED) for s in pool
                       if region in (None, ALL_REGIONS) or self.region_of.get(s, UNASSIGNED) == region}, key=_unassigned_last)

    def stores(self, region: Optional[str] = None, cluster: Optional[str] = None,
               stores: Optional[Iterable[str]] = None) -> list:
        pool = self._pool(stores)
        return sorted(s for s in pool
                      if region in (None, ALL_REGIONS) or self.region_of.get(s, UNASSIGNED) == region
                      if cluster in (None, ALL_CLUSTERS) or self.cluster_of.get(s, UNASSIGNED) == cluster)

    def _pool(self, stores):
        return sorted(set(stores)) if stores is not None else sorted(self.region_of)

    def clusters_in_several_regions(self) -> dict:
        """cluster name -> regions, for a name that appears under more than one region (shown as a warning when the master is loaded)."""
        seen: dict = {}
        for s, c in self.cluster_of.items():
            if c != UNASSIGNED:
                seen.setdefault(c, set()).add(self.region_of.get(s, UNASSIGNED))
        return {c: sorted(r) for c, r in seen.items() if len(r) > 1}


def _unassigned_last(name: str):
    """Real regions / clusters first, then Closed, then Unassigned."""
    return (name == UNASSIGNED, name == CLOSED, name.lower())


def resolve(h: Hierarchy, loaded_stores: Iterable[str], region: Optional[str] = None, cluster: Optional[str] = None,
            store: Optional[str] = None) -> StoreScope:
    """The scope the filters describe, limited to the stores the run actually loaded. A named store wins; otherwise a cluster (inside the
    region, if one is chosen); otherwise a region; otherwise every store (None). A region/cluster with no loaded store is an error the
    caller turns into a message - never "all stores"."""
    if store not in (None, "", ALL_STORES):
        return str(store)
    if region in (None, "", ALL_REGIONS) and cluster in (None, "", ALL_CLUSTERS):
        return None
    codes = h.stores(region, cluster, loaded_stores)
    return normalize(codes)
