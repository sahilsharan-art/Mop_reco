"""Build docs/MOP_DEFECT_REGISTER.xlsx from docs/audit/MOP_DEFECT_REGISTER.md (audit Phase 9).

    python scripts/audit_build_defect_workbook.py

The markdown register is the source of truth; this reads its two tables (defects, 9 columns; observations, 5 columns) and refuses to write a row
whose column count is wrong, so a stray '|' can never silently shift a cell.
"""
import re
import sys
from collections import Counter
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
lines = (ROOT / "docs" / "audit" / "MOP_DEFECT_REGISTER.md").read_text(encoding="utf-8").splitlines()


def cells(line):
    inner = line.strip()
    inner = inner[1:-1] if inner.startswith("|") and inner.endswith("|") else inner
    return [c.strip() for c in re.split(r"(?<!\\)\s\|\s", inner)]


tables, cur = [], None
for ln in lines:
    if ln.startswith("| ID |"):
        cur = {"head": cells(ln), "rows": []}
        tables.append(cur)
    elif ln.startswith("|---") or not ln.startswith("|"):
        if not ln.startswith("|---"):
            cur = None if not ln.startswith("|") else cur
    elif cur is not None:
        cur["rows"].append(cells(ln))
bad = [(t["head"][0], r[0], len(r), len(t["head"])) for t in tables for r in t["rows"] if len(r) != len(t["head"])]
if bad:
    sys.exit(f"rows with the wrong number of cells: {bad}")
defects = tables[0]["rows"]
obs = tables[1]["rows"] if len(tables) > 1 else []
print(f"{len(defects)} defect rows, {len(obs)} observation rows")

FILL = {"Fixed locally": "FFF2CC", "Closed": "D9EAD3", "Open": "F4CCCC"}
wb = Workbook()
ws = wb.active
ws.title = "Defects"
ws.append(tables[0]["head"])
for r in defects:
    ws.append(r)
    st = r[-1]
    key = "Fixed locally" if st.startswith("Fixed locally") else ("Closed" if st.startswith("Closed") else "Open")
    ws.cell(ws.max_row, len(r)).fill = PatternFill("solid", fgColor=FILL[key])
ws2 = wb.create_sheet("Observations and decisions")
ws2.append(tables[1]["head"] if len(tables) > 1 else ["ID"])
for r in obs:
    ws2.append(r)
for w_ in (ws, ws2):
    for c in w_[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1A4D8F")
    for row in w_.iter_rows():
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    w_.freeze_panes = "B2"
    w_.auto_filter.ref = w_.dimensions
for i, w in enumerate([8, 12, 38, 46, 46, 40, 36, 34, 18], 1):
    ws.column_dimensions[get_column_letter(i)].width = w
for i, w in enumerate([8, 52, 46, 46, 26], 1):
    ws2.column_dimensions[get_column_letter(i)].width = w

s = wb.create_sheet("Closure summary", 0)
sev = Counter(r[1].split()[0].replace("**", "") for r in defects)
status = Counter("Fixed locally (not deployed)" if r[-1].startswith("Fixed locally") else r[-1].split(";")[0] for r in defects)
s.append(["MOP Reconciliation - defect closure summary (24 Nov 2026)"])
s["A1"].font = Font(bold=True, size=14)
s.append(["A defect is 'Closed' only when its fix is deployed and verified on the live system. No defect is Closed yet: every code fix exists in the development copy only."])
s.append([])
s.append(["Rows in the register (defects, data findings and rulings)", len(defects)])
s.append(["By severity / kind", ""])
for k, v in sorted(sev.items()):
    s.append(["", f"{k}: {v}"])
s.append(["By status", ""])
for k, v in sorted(status.items()):
    s.append(["", f"{k}: {v}"])
s.append([])
s.append(["Observations needing a decision or another team", len(obs)])
s.column_dimensions["A"].width = 56
s.column_dimensions["B"].width = 60
for row in s.iter_rows():
    for c in row:
        c.alignment = Alignment(wrap_text=True, vertical="top")
out = ROOT / "docs" / "MOP_DEFECT_REGISTER.xlsx"
wb.save(out)
print("written", out.name, dict(sev), dict(status))
