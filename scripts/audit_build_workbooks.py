"""Build the audit workbooks from the generated inventory and the manual status file. Re-run after every audit phase.

    python scripts/audit_build_workbooks.py

Reads docs/audit/inventory.json (scripts/audit_inventory.py) and docs/audit/module_status.json (edited by hand, with evidence) and writes
docs/MOP_MODULE_WISE_AUDIT.xlsx. Later phases add the defect register, UAT workbook and others through the same pattern.
"""
import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
inv = json.loads((ROOT / "docs/audit/inventory.json").read_text(encoding="utf-8"))
status = json.loads((ROOT / "docs/audit/module_status.json").read_text(encoding="utf-8"))
status = {k: v for k, v in status.items() if not k.startswith("_")}

FILL = {"Verified and working": "D9EAD3", "Working with limitations": "FFF2CC", "Defective": "F4CCCC", "Not implemented": "F4CCCC",
        "Unable to verify": "FCE5CD", "Not yet audited": "EEEEEE"}
wb = Workbook()
ws = wb.active
ws.title = "Module audit"
head = ["File", "Kind", "Lines", "Functions", "Imported by (non-test)", "Tests naming it", "Tables touched", "Broad except", "Silent except",
        "Status", "Evidence", "Findings", "Severity", "Owner", "Phase audited"]
ws.append(head)
order = {"engine": 0, "app": 1, "page": 2, "script": 3, "test": 4}
for rel, v in sorted(inv.items(), key=lambda kv: (order[kv[1]["kind"]], kv[0])):
    if v["kind"] == "test":
        continue
    st = status.get(rel, {})
    nt = [Path(x).stem for x in v["imported_by"] if inv[x]["kind"] != "test"]
    ws.append([rel, v["kind"], v["lines"], v["functions"], ", ".join(nt), v["tests_mentioning"], ", ".join(v["tables"]), v["broad_except"] or "",
               len(v["silent_except"]) or "", st.get("status", "Not yet audited"), st.get("evidence", ""), st.get("findings", ""), st.get("severity", ""),
               "Audit team", st.get("phase", "")])
for c in ws[1]:
    c.font = Font(bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="1A4D8F")
    c.alignment = Alignment(wrap_text=True, vertical="top")
for row in ws.iter_rows(min_row=2):
    row[9].fill = PatternFill("solid", fgColor=FILL.get(row[9].value, "FFFFFF"))
    for c in row:
        c.alignment = Alignment(wrap_text=True, vertical="top")
for i, w in enumerate([34, 8, 7, 9, 30, 9, 26, 8, 8, 22, 60, 50, 9, 12, 8], 1):
    ws.column_dimensions[get_column_letter(i)].width = w
ws.freeze_panes = "B2"
ws.auto_filter.ref = ws.dimensions

s2 = wb.create_sheet("Status summary")
s2.append(["Status", "Modules"])
counts = {}
for r in ws.iter_rows(min_row=2, values_only=True):
    counts[r[9]] = counts.get(r[9], 0) + 1
for k in ["Verified and working", "Working with limitations", "Defective", "Not implemented", "Unable to verify", "Not yet audited"]:
    s2.append([k, counts.get(k, 0)])
s2.append(["Total (non-test files)", sum(counts.values())])
for c in s2[1]:
    c.font = Font(bold=True)
s2.column_dimensions["A"].width = 30
out = ROOT / "docs" / "MOP_MODULE_WISE_AUDIT.xlsx"
wb.save(out)
print("written", out, counts)
