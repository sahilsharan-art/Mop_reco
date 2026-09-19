"""Export rules: whole filtered set, correct format, ids intact, header block."""

from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco.export import XLSX_ROW_LIMIT, ExportMeta, build_export, is_id_column

# A real Paytm transaction_id: 33 digits. Excel keeps only 15 significant
# digits for a number, so this is the value that proves ids stay text.
LONG_REF = "202607201911391000000082051077"


def frame(rows=3):
    return pd.DataFrame({
        "date": ["2026-07-20"] * rows,
        "store": ["ABD"] * rows,
        "bill_no": [f"SB58/0248{i:02d}/0726" for i in range(rows)],
        "paytm_ref": [LONG_REF] * rows,
        "amount": [100.5] * rows,
    })


def meta():
    return ExportMeta(run_id=7, run_timestamp=datetime(2026, 9, 9, 10, 30),
                      date_from="2026-07-01", date_to="2026-07-31",
                      filters={"store": "ABD", "status": "Any", "mop": "PAYTM EDC"},
                      value_column="amount", title="Transactions")


def test_id_columns_detected():
    assert is_id_column("paytm_ref") and is_id_column("bill_no")
    assert is_id_column("matched_txn") and is_id_column("transaction_id")
    assert not is_id_column("amount") and not is_id_column("store")


def test_small_export_is_xlsx_and_says_so():
    result = build_export(frame(), meta())
    assert result.fmt == "xlsx"
    assert result.filename.endswith(".xlsx")
    assert "Excel" in result.note


def header_text(result) -> str:
    """Everything in the header block, flattened to one searchable string."""
    raw = pd.read_excel(io.BytesIO(result.data), header=None,
                        nrows=result.header_row)
    return " ".join(str(v) for v in raw.to_numpy().ravel())


def test_header_block_carries_provenance():
    text = header_text(build_export(frame(), meta()))
    assert "#7" in text                       # run id
    assert "2026-09-09 10:30:00" in text      # run timestamp
    assert "2026-07-01 to 2026-07-31" in text  # date range
    assert "store=ABD" in text                # filters applied
    assert "mop=PAYTM EDC" in text
    assert "3" in text                        # row count
    assert "301.50" in text                   # total value


def test_reference_id_is_not_truncated():
    result = build_export(frame(), meta())
    body = pd.read_excel(io.BytesIO(result.data), header=result.header_row, dtype=str)
    assert body["paytm_ref"].iloc[0] == LONG_REF
    assert len(body["paytm_ref"].iloc[0]) == len(LONG_REF)
    assert "E+" not in body["paytm_ref"].iloc[0]


def test_filters_set_to_any_are_not_listed():
    assert "status=Any" not in header_text(build_export(frame(), meta()))


def test_above_the_threshold_switches_to_csv_and_says_why():
    big = frame(XLSX_ROW_LIMIT + 1)
    result = build_export(big, meta())
    assert result.fmt == "csv"
    assert result.filename.endswith(".csv")
    assert "CSV because" in result.note
    text = result.data.decode("utf-8-sig")
    assert text.startswith("# Application,Citykart MOP Reconciliation")
    assert "# Report,Transactions" in text
    assert LONG_REF in text          # still whole in the csv
    assert text.count("\n") > XLSX_ROW_LIMIT


def test_progress_is_reported():
    seen = []
    build_export(frame(), meta(), progress=lambda f, m: seen.append((f, m)))
    assert seen, "a large export must be able to show movement"
    assert seen[-1][0] == 1.0
    assert all(0 <= f <= 1 for f, _ in seen)


def test_blank_reference_becomes_empty_not_the_string_nan():
    df = frame()
    df.loc[1, "paytm_ref"] = None
    result = build_export(df, meta())
    body = pd.read_excel(io.BytesIO(result.data), header=result.header_row, dtype=str)
    assert pd.isna(body["paytm_ref"].iloc[1]) or body["paytm_ref"].iloc[1] == ""


def test_export_is_named_mop_reconciliation_not_cash():
    """Cash reconciliation is not running — the export must not claim it."""
    text = header_text(build_export(frame(), meta()))
    assert "Citykart MOP Reconciliation" in text
    assert "Cash Reconciliation" not in text
