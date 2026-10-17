"""R1 matching, a few days at a time (one chunk of consecutive dates in memory). CLAUDE.md 6.3 / 6.14.

`r1_bill_txn.run_r1` takes the WHOLE range as in-memory frames: every digital
bill line and the whole gateway pool at once. Its peak memory therefore grows
with the range (5 months of bills is several GB before a single match).
This module produces the SAME result while holding only one CHUNK of
consecutive dates in memory - not the range.

Why it is still exact although a gateway reference is date-agnostic (§6.3
Tier 1 searches the ENTIRE loaded transaction table):

  * The pool is loaded ONCE per run into a disk-resident SQLite index
    (`work/r1.sqlite`), one per-date warehouse file at a time, indexed by
    (gateway, reference). A day's bills then find their transactions across
    every loaded date with an indexed lookup that touches only the rows it
    returns. Memory is one source file during the build and one day's hits
    afterwards - it does not depend on how many months are loaded.
    (Scanning every per-date Parquet file for each day instead was tried:
    correct, but its transient decode buffers, and the allocator's retention
    of them, made peak memory grow with the range.)
  * "A transaction can never close two bills" and "the first bill in frame
    order wins" are carried across chunks by a `used` flag on the pool row in the
    same index: a row Tier 1 consumed is never offered again, and within a chunk
    the k-th bill of a (reference, amount) key takes the k-th still-unused row.
  * Tier 2 (EDC OTHER) must run against what Tier 1 left over across the
    WHOLE range - a bill dated D+3 can consume a transaction dated D - so the
    work is two passes over the dates: pass A runs Tier 1 for every date
    (ascending, so frame order is preserved) and records consumption; pass B
    runs Tier 2 for each date against the pool left after ALL of Tier 1,
    then yields that date's final lines. Transactions on dates that have no
    bills are visited too, because they are unmapped transactions.

Equivalence with `run_r1` is asserted by tests/test_r1_chunked.py on
synthetic data and by scripts/r1_chunked_check.py / r1_e2e_check.py on the
real warehouse against Run #11.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from . import r1_bill_txn as r1

logger = logging.getLogger("mop_reco")

GATEWAYS = ("paytm", "axis", "bajaj")   # the order Tier 1 applies them in

# gateway -> (the pool-builder argument, the raw reference column, the raw
# columns build_gateway_pool reads from that source's per-date Parquet)
_SPEC = {
    "paytm": ("paytm_df", "transaction_id",
              ["transaction_id", "business_date", "amount", "status", "transaction_type",
               "payment_mode", "store", "transaction_time"]),
    "axis": ("axis_df", "RRN",
             ["RRN", "business_date", "GROSS_AMT", "CARD_TYPE", "store", "transaction_time"]),
    "bajaj": ("bajaj_df", "Transaction Id",
              ["Transaction Id", "business_date", "Transaction Amount", "Transaction Status",
               "Card Scheme", "store", "transaction_time"]),
}

BILL_COLUMNS = ["bill_no", "line_seq", "store", "business_date", "mop", "amount", "mop_class",
                "in_sale_universe", "paytm_ref", "axis_ref", "bajaj_ref", "bill_time", "created_by"]

_POOL_COLUMNS = ["gateway", "txn_ref", "business_date", "amount", "status", "txn_type",
                 "payment_mode", "store", "txn_time"]


@dataclass
class ChunkedResult:
    summary: dict
    unmapped_txns: pd.DataFrame
    linkage_exceptions: pd.DataFrame
    dates_with_bills: list = field(default_factory=list)
    # One row per (business_date, store, gateway): every transaction the gateway file holds
    # (total_n/total_v) and how many of those the engine matched to a bill (mapped_n/mapped_v).
    # The Summary's "Mapped / Refunded / Unmapped" strip reads this instead of re-reading the pool.
    txn_agg: pd.DataFrame = None


def _empty_pool() -> pd.DataFrame:
    return pd.DataFrame(columns=_POOL_COLUMNS)


def _none_if_na(v):
    return None if v is None or (not isinstance(v, str) and pd.isna(v)) else v


class _PoolStore:
    """The gateway pool - SUCCESS/ACQUIRING rows only, exactly as
    `r1_bill_txn.successful_acquiring(build_gateway_pool(...))` produces them -
    held in a SQLite file on disk and queried by reference or by date."""

    def __init__(self, files_by_gateway: dict, db_path: Path):
        # files_by_gateway: {gateway: {date_str: path}}
        self.files = {g: [p for _, p in sorted(files_by_gateway.get(g, {}).items())]
                      for g in GATEWAYS}
        self.by_date = {g: dict(files_by_gateway.get(g, {})) for g in GATEWAYS}
        self._datasets: dict = {}
        self.db = sqlite3.connect(str(db_path))
        self.db.execute("pragma journal_mode=off")
        self.db.execute("pragma synchronous=off")
        self.db.execute("pragma cache_size=-65536")      # 64 MB page cache, whatever the range
        self.db.execute("pragma temp_store=file")
        self.db.execute("create table extra (gateway text, ref text, bdate text, store text, amt real)")
        self.db.execute(
            "create table pool (seq integer primary key, gateway text, ref text, bdate text, "
            "amt real, status text, ttype text, mode text, store text, ttime text, "
            "used integer not null default 0)")
        self.all_rows = 0
        self.matching_rows = 0
        self.totals: list = []        # (date, store, gateway, n, value) over every row of every file

    def dates(self) -> set:
        out: set = set()
        for g in GATEWAYS:
            out |= set(self.by_date[g])
        return out

    def row_counts(self) -> dict:
        return {g: sum(pq.ParquetFile(p).metadata.num_rows for p in self.files[g])
                for g in GATEWAYS}

    def txn_aggregate(self) -> pd.DataFrame:
        """Per (business_date, store, gateway): totals over every transaction row, and how many
        the engine matched to a bill (`used`: Tier 1 flagged in the index, Tier 2 marked after)."""
        total = pd.DataFrame(self.totals, columns=["business_date", "store", "gateway", "total_n", "total_v"])
        if total.empty:
            return pd.DataFrame(columns=["business_date", "store", "gateway", "total_n", "total_v",
                                         "mapped_n", "mapped_v"])
        total = (total.groupby(["business_date", "store", "gateway"], dropna=False)
                 .agg(total_n=("total_n", "sum"), total_v=("total_v", "sum")).reset_index())
        # MAPPED, as the Summary's strip has always defined it: a row counts when its
        # (gateway, reference) is one a bill matched - by reference, not by which physical row
        # the engine happened to consume. (A duplicate line sharing a matched reference - 80 of
        # them in the Axis file, Apr-Aug - is therefore "mapped" here although the engine
        # lists it as an unmapped transaction.) Rows outside the matching pool count too.
        self.db.execute("drop table if exists mk")
        self.db.execute("create temp table mk as select distinct gateway, ref from pool "
                        "where used=1 and ref is not null")
        self.db.execute("create index ix_mk on mk (gateway, ref)")
        rows = self.db.execute(
            "select bdate, store, gateway, count(*), coalesce(sum(amt), 0.0) from ("
            " select p.bdate, p.store, p.gateway, p.amt from pool p"
            "  join mk on mk.gateway=p.gateway and mk.ref=p.ref"
            " union all"
            " select e.bdate, e.store, e.gateway, e.amt from extra e"
            "  join mk on mk.gateway=e.gateway and mk.ref=e.ref"
            ") group by bdate, store, gateway").fetchall()
        mapped = pd.DataFrame(rows, columns=["business_date", "store", "gateway", "mapped_n", "mapped_v"])
        out = total.merge(mapped, on=["business_date", "store", "gateway"], how="left")
        out["mapped_n"] = out["mapped_n"].fillna(0).astype("int64")
        out["mapped_v"] = out["mapped_v"].fillna(0.0)
        # The SALE population and what the engine PHYSICALLY consumed of it (2026-10-16): pool rows
        # (SUCCESS / ACQUIRING) and, of those, the rows a bill actually took. A duplicate line that
        # shares a matched reference is a pool row that was NOT consumed, so it is unmapped here - as
        # the exception register lists it. pool_n - consumed_n is exactly the engine's unmapped set.
        prow = self.db.execute(
            "select bdate, store, gateway, count(*), coalesce(sum(amt), 0.0), coalesce(sum(used), 0), "
            "coalesce(sum(case when used=1 then amt else 0.0 end), 0.0) from pool "
            "group by bdate, store, gateway").fetchall()
        pooled = pd.DataFrame(prow, columns=["business_date", "store", "gateway", "pool_n", "pool_v",
                                             "consumed_n", "consumed_v"])
        out = out.merge(pooled, on=["business_date", "store", "gateway"], how="left")
        for c in ("pool_n", "consumed_n"):
            out[c] = out[c].fillna(0).astype("int64")
        for c in ("pool_v", "consumed_v"):
            out[c] = out[c].fillna(0.0)
        return out

    def matching_pool_rows(self) -> int:
        return self.matching_rows

    def _dataset(self, gateway: str):
        """A pyarrow dataset over a gateway's files - used only for the
        one-off non-ACQUIRING linkage scan, never per day."""
        if gateway not in self._datasets:
            files = self.files[gateway]
            if not files:
                self._datasets[gateway] = None
            else:
                # Per-date files can disagree on the type of an all-null column
                # (e.g. Axis transaction_time is `null` on one day, a time on
                # another): unify the schemas rather than trusting the first.
                schema = pa.unify_schemas([pq.read_schema(p) for p in files],
                                          promote_options="permissive")
                self._datasets[gateway] = pads.dataset(files, format="parquet", schema=schema)
        return self._datasets[gateway]

    def _normalise_file(self, gateway: str, path: str):
        """One per-date Parquet file -> (all_rows, the pool rows it contributes, the file's
        totals per store over ALL its rows - refunds, chargebacks and failures included)."""
        _, _, cols = _SPEC[gateway]
        have = set(pq.read_schema(path).names)
        frame = pq.read_table(path, columns=[c for c in cols if c in have]).to_pandas()
        if frame.empty:
            return 0, _empty_pool(), None, None
        arg = _SPEC[gateway][0]
        pool = r1.build_gateway_pool(**{arg: frame})
        everything = pool[["store", "amount"]].copy()
        everything["amount"] = pd.to_numeric(everything["amount"], errors="coerce")
        totals = (everything.groupby("store", dropna=False)["amount"]
                  .agg(total_n="size", total_v="sum").reset_index())
        matching = r1.successful_acquiring(pool)
        extras = pool.loc[~pool.index.isin(matching.index)]
        return len(pool), matching.reset_index(drop=True), totals, extras

    def build(self, on_progress=None):
        """Load every file once, in pool order (gateway, then date, then file
        order), one file in memory at a time."""
        seq = 0
        total_files = sum(len(v) for v in self.files.values())
        done = 0
        for gateway in GATEWAYS:
            for path in self.files[gateway]:
                n_all, pool, totals, extras = self._normalise_file(gateway, path)
                if extras is not None and len(extras):
                    day_text = Path(path).stem
                    self.db.executemany("insert into extra values (?,?,?,?,?)", [
                        (gateway, _none_if_na(r), day_text, _none_if_na(st),
                         None if pd.isna(a) else float(a))
                        for r, st, a in zip(extras["txn_ref"], extras["store"], extras["amount"])])
                self.all_rows += n_all
                if totals is not None:
                    day = Path(path).stem
                    for store, n, v in zip(totals["store"], totals["total_n"], totals["total_v"]):
                        self.totals.append((day, None if pd.isna(store) else str(store),
                                            gateway, int(n), float(v)))
                self.matching_rows += len(pool)
                done += 1
                if not pool.empty:
                    def col(name):
                        c = pool[name].astype(object)
                        return c.where(c.notna(), None).tolist()
                    ttime = [None if t is None else t.isoformat() for t in col("txn_time")]
                    amts = [None if a is None else float(a) for a in col("amount")]
                    rows = list(zip(
                        range(seq, seq + len(pool)), [gateway] * len(pool),
                        col("txn_ref"), [str(b) for b in pool["business_date"]], amts,
                        col("status"), col("txn_type"), col("payment_mode"), col("store"), ttime))
                    seq += len(rows)
                    self.db.executemany(
                        "insert into pool (seq, gateway, ref, bdate, amt, status, ttype, mode, "
                        "store, ttime) values (?,?,?,?,?,?,?,?,?,?)", rows)
                if on_progress and done % 25 == 0:
                    on_progress(f"Indexing the pool: {done:,} of {total_files:,} files")
        self.db.commit()
        self.db.execute("create index ix_pool_ref on pool (gateway, ref)")
        self.db.execute("create index ix_pool_date on pool (gateway, bdate)")
        self.db.commit()
        if on_progress:
            on_progress(f"Pool indexed: {self.matching_rows:,} transactions from {total_files:,} files")

    # -- reads ---------------------------------------------------------------

    @staticmethod
    def _frame(rows) -> pd.DataFrame:
        if not rows:
            return _empty_pool()
        gateway, ref, bdate, amt, status, ttype, mode, store, ttime, seq, used = zip(*rows)
        return pd.DataFrame({
            "seq": pd.Series(seq, dtype="int64"),
            "used": pd.Series(used, dtype="int64"),
            "gateway": list(gateway),
            # A null reference is pd.NA, as in the whole-frame pool: it is
            # printed (str) into a Tier-2 ambiguous-candidate list as "<NA>".
            "txn_ref": pd.Series([pd.NA if r is None else r for r in ref], dtype=object),
            "business_date": pd.Series(
                [_dt.date.fromisoformat(b) for b in bdate], dtype=object),
            "amount": pd.Series([np.nan if a is None else a for a in amt], dtype="float64"),
            "status": pd.Series(status, dtype=object),
            "txn_type": pd.Series(ttype, dtype=object),
            "payment_mode": pd.Series(mode, dtype=object),
            "store": pd.Series(store, dtype=object),
            "txn_time": pd.Series(
                [None if t is None else _dt.time.fromisoformat(t) for t in ttime], dtype=object),
        })

    _SELECT = ("select p.gateway, p.ref, p.bdate, p.amt, p.status, p.ttype, p.mode, p.store, "
               "p.ttime, p.seq, p.used")

    def lookup(self, gateway: str, refs) -> pd.DataFrame:
        """The pool rows of `gateway`, across ALL loaded dates, whose reference is
        in `refs`, in pool order (date, then file order) - only the columns Tier 1
        matches on: txn_ref, business_date (ISO text), amount, payment_mode, seq,
        used. The references go in as ONE JSON array, so SQLite probes its index
        once per reference without a row-by-row insert. (A month of hits is
        ~800k rows; carrying the status/store/time columns nobody reads here was
        most of this module's peak memory.)"""
        refs = [str(r) for r in pd.unique(pd.Series(list(refs), dtype=object).dropna())]
        if not refs:
            return pd.DataFrame(columns=["txn_ref", "business_date", "amount", "payment_mode",
                                         "seq", "used"])
        rows = self.db.execute(
            "select p.ref, p.bdate, p.amt, p.mode, p.seq, p.used "
            "from json_each(?) j join pool p on p.gateway=? and p.ref=j.value order by p.seq",
            (json.dumps(refs), gateway)).fetchall()
        if not rows:
            return pd.DataFrame(columns=["txn_ref", "business_date", "amount", "payment_mode",
                                         "seq", "used"])
        ref, bdate, amt, mode, seq, used = zip(*rows)
        return pd.DataFrame({
            "txn_ref": pd.Series(ref, dtype=object),
            "business_date": pd.Series(bdate, dtype=object),
            "amount": pd.Series([np.nan if a is None else a for a in amt], dtype="float64"),
            "payment_mode": pd.Series(mode, dtype=object),
            "seq": pd.Series(seq, dtype="int64"),
            "used": pd.Series(used, dtype="int64"),
        })

    def mark_used(self, seqs):
        """Tier 1 consumed these pool rows: a transaction can close one bill only."""
        seqs = [int(x) for x in seqs]
        if seqs:
            self.db.execute("update pool set used=1 where seq in (select value from json_each(?))",
                            (json.dumps(seqs),))

    def range_slice(self, gateway: str, lo: str, hi: str, unused_only: bool = False) -> pd.DataFrame:
        """Every pool transaction of `gateway` DATED between `lo` and `hi`
        (ISO dates, inclusive), in pool order, including those with no reference.
        `unused_only` keeps just the rows Tier 1 left unconsumed - after Tier 1
        that is a few thousand rows of millions, so it is filtered in the index
        rather than fetched and thrown away."""
        rows = self.db.execute(
            f"{self._SELECT} from pool p where p.gateway=? and p.bdate>=? and p.bdate<=? "
            + ("and p.used=0 " if unused_only else "") + "order by p.seq",
            (gateway, lo, hi)).fetchall()
        return self._frame(rows)


def _unused_candidates(hits: pd.DataFrame) -> pd.DataFrame:
    """The rows of `hits` a bill can still take: not consumed, with a reference
    and an amount, plus `ref`/`amt` and `rank` - the row's position among the
    still-unused rows of its (reference, amount) key, in pool order.

    This is the loop's "first unconsumed candidate": every earlier bill with the
    key took exactly the first unused one, so the k-th bill takes the k-th."""
    hits = hits.copy()
    hits["ref"] = hits["txn_ref"].astype(object)
    hits["amt"] = r1._round2(hits["amount"].to_numpy(dtype="float64"))
    ok = (hits["used"].to_numpy() == 0) & hits["ref"].notna().to_numpy() \
        & ~np.isnan(hits["amt"].to_numpy())
    hits = hits.loc[ok].copy()
    hits["rank"] = hits.groupby(["ref", "amt"], sort=False).cumcount()
    return hits


def _str_array(series: pd.Series) -> np.ndarray:
    """`series.astype(str)` as an object array, without converting a column that
    is already text (which costs seconds per million Arrow strings). A column of
    any other kind still goes through `astype(str)`, exactly as before."""
    if pd.api.types.is_string_dtype(series.dtype) and not pd.api.types.is_object_dtype(series.dtype):
        return series.to_numpy(dtype=object)
    return series.astype(str).to_numpy(dtype=object)


def _tier1_chunk(bills: pd.DataFrame, pool: _PoolStore):
    """Tier 1 for one chunk of bills (consecutive dates, in frame order), against
    the whole history of the pool. A chunk is exactly the sequence of its dates
    processed one after another: first-bill-wins is a rank within a key in frame
    order, and a consumed row stays consumed in the index for the next chunk.
    `bills` is the caller's own frame and is extended in place (no copy: a month
    of lines is the largest thing this module holds)."""
    bills["matched"] = False
    bills["match_tier"] = pd.NA
    bills["matched_gateway"] = pd.NA
    bills["matched_txn_ref"] = pd.NA
    bills["matched_payment_mode"] = pd.NA
    bills["date_diff_days"] = 0
    bills["match_note"] = pd.NA
    bills["refs_tried"] = sum(
        bills[col].notna().astype(int) for col in r1.REF_COLUMN_BY_GATEWAY.values()
        if col in bills.columns)
    bills["refs_resolved"] = 0

    n = len(bills)
    matched = np.zeros(n, dtype=bool)
    tier = np.full(n, pd.NA, dtype=object)
    gateway_col = np.full(n, pd.NA, dtype=object)
    txn_ref_col = np.full(n, pd.NA, dtype=object)
    mode_col = np.full(n, pd.NA, dtype=object)
    diff_col = np.zeros(n, dtype="int64")
    note_col = np.full(n, pd.NA, dtype=object)
    resolved = np.zeros(n, dtype="int64")
    amounts = bills["amount"].to_numpy(dtype="float64")
    bill_dates = bills["business_date"]

    for gateway, ref_col in r1.REF_COLUMN_BY_GATEWAY.items():
        if ref_col not in bills.columns:
            continue
        cand = bills[ref_col].notna().to_numpy() & ~matched & ~np.isnan(amounts)
        cpos = np.flatnonzero(cand)
        if len(cpos) == 0:
            continue
        bdf = pd.DataFrame({"ref": _str_array(bills[ref_col].iloc[cpos]),
                            "amt": r1._round2(amounts[cpos]), "bpos": cpos})
        hits = pool.lookup(gateway, bdf["ref"].unique())
        if hits.empty:
            continue
        hits = _unused_candidates(hits)
        if hits.empty:
            continue
        keys = ["ref", "amt"]
        bdf["rank"] = bdf.groupby(keys, sort=False).cumcount()
        hit = bdf.merge(hits[keys + ["rank", "payment_mode", "business_date", "seq"]],
                        on=keys + ["rank"], how="inner", suffixes=("", "_t"))
        if hit.empty:
            continue
        pool.mark_used(hit["seq"].to_numpy())
        bp = hit["bpos"].to_numpy()
        matched[bp] = True
        tier[bp] = "Tier 1"
        gateway_col[bp] = gateway
        txn_ref_col[bp] = hit["ref"].to_numpy(dtype=object)
        mode_col[bp] = hit["payment_mode"].to_numpy(dtype=object)
        resolved[bp] += 1
        diff = np.asarray((pd.to_datetime(hit["business_date"].to_numpy())
                           - pd.to_datetime(bill_dates.iloc[bp].to_numpy())).days, dtype="int64")
        diff_col[bp] = diff
        for j in np.flatnonzero(diff != 0):
            d = int(diff[j])
            note_col[bp[j]] = (f"reference matched, txn dated {abs(d)} day(s) "
                               f"{'after' if d > 0 else 'before'} bill")

    bills["matched"] = matched
    bills["match_tier"] = tier
    bills["matched_gateway"] = gateway_col
    bills["matched_txn_ref"] = txn_ref_col
    bills["matched_payment_mode"] = mode_col
    bills["date_diff_days"] = diff_col
    bills["match_note"] = note_col
    bills["refs_resolved"] = resolved
    return bills


def _chunk_residual_pool(pool: _PoolStore, lo: str, hi: str) -> pd.DataFrame:
    """The transactions DATED lo..hi that Tier 1 (over every date) left
    unconsumed, all three gateways, in pool order, as one pool with fresh labels.
    Consumption is a flag on the row, so no ranking is needed to know."""
    parts = []
    for gateway in GATEWAYS:
        slc = pool.range_slice(gateway, lo, hi, unused_only=True)
        if slc.empty:
            continue
        parts.append(slc[_POOL_COLUMNS + ["seq"]])
    if not parts:
        return _empty_pool().assign(seq=pd.Series(dtype="int64"))
    return pd.concat(parts, ignore_index=True)


def _link_non_acquiring(pool: _PoolStore):
    """`r1.link_non_acquiring` over the whole Paytm history, reading only the
    non-ACQUIRING rows and the originals they point at (one scan, once per run)."""
    files = pool.files["paytm"]
    empty = r1.link_non_acquiring(None)
    if not files:
        return empty
    ds = pool._dataset("paytm")
    odd_expr = pc.utf8_upper(pc.field("transaction_type")).isin(list(r1.NON_ACQUIRING_TYPES))
    cols = _SPEC["paytm"][2] + ["reference_transaction_id"]
    have = set(ds.schema.names)
    cols = [c for c in cols if c in have]
    odd = ds.to_table(columns=cols, filter=odd_expr).to_pandas()
    if odd.empty:
        return empty
    ref_ids = pd.unique(odd["reference_transaction_id"].dropna().astype(str).str.strip())
    originals = ds.to_table(columns=cols,
                            filter=pc.field("transaction_id").isin(pa.array(ref_ids))).to_pandas()
    frame = pd.concat([odd, originals], ignore_index=True).drop_duplicates("transaction_id")
    frame["reference_transaction_id"] = frame["reference_transaction_id"].astype("string")
    return r1.link_non_acquiring(frame)


# Working memory per digital bill line while a chunk is in flight (the bills, their
# looked-up transactions and pandas temporaries). Measured on the real Apr-Aug
# warehouse: (1,714 MB peak - 494 MB baseline) / 838,614 lines = ~1.45 KB, rounded
# up. Only used to size a chunk.
BYTES_PER_BILL_ROW = 1_500
# Fraction of the RAM that is available right now that one chunk may use.
CHUNK_MEMORY_SHARE = 0.35


# The default chunk size, from a back-to-back sweep on the real Apr-Aug warehouse
# (3.70M digital lines, warm file cache, every size reproducing Run #11 exactly):
#
#     lines per chunk   chunks   time    peak memory
#     ~47,000 (a date)    155    149 s     666 MB
#     100,000              46     99 s     770 MB      <- default
#     300,000              17    100 s   1,011 MB
#     600,000              12    100 s   1,346 MB
#     838,614 (a month)     7    104 s   1,756 MB
#
# One chunk per date pays a fixed cost 155 times (+50% time). Beyond ~100k lines
# time is flat and only memory grows, so the smallest chunk that is not
# overhead-bound is the right one. (Single runs on a shared machine vary a lot -
# an earlier, cold-cache sweep read 149/213/284 s for 100k/300k/month, and one
# run stalled for 2.6 hours - which is why this one is back to back.)
DEFAULT_CHUNK_ROWS = 100_000


def _adaptive_chunk_rows() -> int:
    """How many digital bill lines one chunk may hold: the measured-fastest size
    (`DEFAULT_CHUNK_ROWS`), lowered if the memory actually available at run time
    could not hold that many (never a constant for a small machine)."""
    try:
        import psutil
        available = psutil.virtual_memory().available
    except Exception:                       # psutil missing: fall back to a modest budget
        available = 2 * 1024 ** 3
    from_memory = max(10_000, int(available * CHUNK_MEMORY_SHARE / BYTES_PER_BILL_ROW))
    return min(DEFAULT_CHUNK_ROWS, from_memory)


def plan_chunks(all_dates: list, weight, max_rows: int = None) -> list:
    """Split the ascending list of ISO dates into chunks of consecutive dates:
    one calendar MONTH each, split further only if a month's `weight` (digital
    bill lines) would exceed `max_rows`. A single date is never split."""
    chunks, cur, w, month = [], [], 0, None
    for d in all_dates:
        wd = weight(d)
        if cur and (d[:7] != month or (max_rows and w + wd > max_rows)):
            chunks.append(cur)
            cur, w = [], 0
        cur.append(d)
        w += wd
        month = d[:7]
    if cur:
        chunks.append(cur)
    return chunks


def run_r1_chunked(bill_files: dict, pool_files: dict, on_day=None, on_progress=None,
                   work_dir=None, chunk_rows: int = None) -> ChunkedResult:
    """R1 over every date in `bill_files` / `pool_files`, one CHUNK in memory.

    bill_files: {date_str: POS per-date Parquet path}
    pool_files: {"paytm"|"axis"|"bajaj": {date_str: Parquet path}}
    on_day(date_str, final_day_frame) is called, oldest date first, ONCE PER DATE
    that has digital bill lines - the persistence hook. Chunk size and commit
    granularity are separate: a chunk is a few days to a month of lines (see
    `chunk_rows`), but the caller still commits every date.

    chunk_rows: None (default) = `_adaptive_chunk_rows()`: the measured-fastest
    size, lowered if the memory available now could not hold it; 0 = always a
    whole calendar month; N = never more than N digital lines in a chunk. A chunk
    never spans a month boundary and a single date is never split.
    """
    own_tmp = work_dir is None
    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="r1chunk_"))
    work.mkdir(parents=True, exist_ok=True)
    pool = None
    try:
        pool = _PoolStore(pool_files, work / "r1.sqlite")
        pool.build(on_progress=on_progress)
        tier1_dir = work / "tier1"
        tier1_dir.mkdir(parents=True, exist_ok=True)

        bill_dates = sorted(bill_files)
        all_dates = sorted(set(bill_dates) | pool.dates())

        def digital_rows(d):
            path = bill_files.get(d)
            if path is None:
                return 0
            t = pq.read_table(path, columns=["mop_class", "in_sale_universe"])
            ok = pc.and_(t["in_sale_universe"], pc.is_in(
                t["mop_class"], value_set=pa.array(list(r1.DIGITAL_CLASSES))))
            return int(pc.sum(ok).as_py() or 0)

        weights = {d: digital_rows(d) for d in all_dates}
        cap = _adaptive_chunk_rows() if chunk_rows is None else (chunk_rows or None)
        chunks = plan_chunks(all_dates, weights.__getitem__, cap)
        if on_progress:
            on_progress(f"Matching in {len(chunks)} chunk(s) of up to "
                        f"{max((sum(weights[d] for d in c) for c in chunks), default=0):,} "
                        f"digital lines (cap {cap if cap else 'one month'})")

        # ---- pass A: Tier 1, chunk by chunk, ascending --------------------
        have_tier1: set = set()
        for ci, chunk in enumerate(chunks):
            frames = []
            for d in chunk:
                if d not in bill_files:
                    continue
                have = set(pq.read_schema(bill_files[d]).names)
                day = pd.read_parquet(bill_files[d], columns=[c for c in BILL_COLUMNS if c in have])
                frames.append(day.loc[day["in_sale_universe"] & day["mop_class"].isin(r1.DIGITAL_CLASSES)])
            frames = [f for f in frames if not f.empty]
            if not frames:
                continue
            bills = pd.concat(frames, ignore_index=True)
            del frames
            out = _tier1_chunk(bills, pool)
            out.to_parquet(tier1_dir / f"{ci}.parquet", index=False)
            have_tier1.add(ci)
            del bills, out
            if on_progress:
                on_progress(f"Tier 1: chunk {ci + 1} of {len(chunks)} ({chunk[0]}..{chunk[-1]})")

        # ---- pass B: Tier 2 + unmapped + yield, chunk by chunk -------------
        totals = dict(lines=0, tier1=0, tier2=0, ambiguous=0, open_exceptions=0, matched=0,
                      matched_value=0.0, unmatched_value=0.0)
        unmapped_parts = []
        dates_with_bills = []
        for ci, chunk in enumerate(chunks):
            lo, hi = chunk[0], chunk[-1]
            residual = _chunk_residual_pool(pool, lo, hi)
            consumed2: set = set()
            if ci in have_tier1:
                bills = pd.read_parquet(tier1_dir / f"{ci}.parquet")
                # Tier 2 only ever touches the unmatched EDC OTHER lines (a couple of
                # thousand a month), so it runs on just those and the answer is
                # written back - never a copy of the whole month's frame.
                eligible = (bills["mop"].isin(r1.EDC_OTHER_MOPS) & ~bills["matched"]).to_numpy()
                bills["ambiguous_candidates"] = pd.Series([pd.NA] * len(bills), dtype=object,
                                                          index=bills.index)
                if eligible.any() and not residual.empty:
                    sub, consumed2 = r1.match_tier2_edc_other(bills.loc[eligible].copy(), residual, set())
                    if consumed2:                # Tier 2 consumed these transactions: flag them mapped
                        pool.mark_used(residual.loc[sorted(consumed2), "seq"].to_numpy())
                    for col in ("matched", "match_tier", "matched_gateway", "matched_txn_ref",
                                "matched_payment_mode", "ambiguous_candidates"):
                        bills.loc[eligible, col] = sub[col].to_numpy()
                bills["status"] = r1._bill_status_vectorized(bills)
                totals["lines"] += len(bills)
                totals["tier1"] += int((bills["match_tier"] == "Tier 1").sum())
                totals["tier2"] += int((bills["match_tier"] == "Matched (EDC OTHER)").sum())
                totals["ambiguous"] += int((bills["match_tier"] == "Ambiguous").sum())
                totals["matched"] += int(bills["matched"].sum())
                totals["open_exceptions"] += int(
                    (~bills["matched"] & (bills["match_tier"] != "Ambiguous")).sum())
                totals["matched_value"] += float(bills.loc[bills["matched"], "amount"].sum())
                totals["unmatched_value"] += float(bills.loc[~bills["matched"], "amount"].sum())
                # One call - and, in the caller, one commit - per DATE, oldest first.
                # (grouped once: a per-date `astype(str) == d` over the whole chunk
                # was 31 string conversions of ~660k rows, 29 s a month.)
                for bdate, day in bills.groupby("business_date", sort=True):
                    d = str(bdate)
                    dates_with_bills.append(d)
                    if on_day:
                        on_day(d, day.reset_index(drop=True))
                del bills
            if not residual.empty:
                left = residual.loc[~residual.index.isin(consumed2)]
                if not left.empty:
                    unmapped_parts.append(left)
            del residual
            if on_progress:
                on_progress(f"Tier 2 / persist: chunk {ci + 1} of {len(chunks)} ({lo}..{hi})")

        unmapped = (pd.concat(unmapped_parts, ignore_index=True)
                    if unmapped_parts else _empty_pool())
        _, linkage_exc = _link_non_acquiring(pool)
        counts = pool.row_counts()
        summary = {
            "digital_bill_lines": totals["lines"],
            "tier1_matched": totals["tier1"],
            "tier2_matched": totals["tier2"],
            "ambiguous": totals["ambiguous"],
            "open_exceptions": totals["open_exceptions"],
            "matched_total": totals["matched"],
            "unmapped_txns": int(len(unmapped)),
            "unmapped_txns_by_gateway": unmapped["gateway"].value_counts().to_dict() if len(unmapped) else {},
            "linkage_exceptions": int(len(linkage_exc)),
            "gateway_pool_rows": int(sum(counts.values())),
            "matching_pool_rows": pool.matching_pool_rows(),
            "matched_value": totals["matched_value"],
            "unmatched_value": totals["unmatched_value"],
        }
        return ChunkedResult(summary=summary, unmapped_txns=unmapped,
                             linkage_exceptions=linkage_exc, dates_with_bills=dates_with_bills,
                             txn_agg=pool.txn_aggregate())
    finally:
        if pool is not None:
            pool.db.close()
        if own_tmp:
            shutil.rmtree(work, ignore_errors=True)
