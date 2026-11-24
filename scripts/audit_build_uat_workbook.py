"""Build docs/MOP_UAT_TEST_CASES.xlsx from the case catalogue and the recorded results (audit Phase 7).

    python scripts/audit_build_uat_workbook.py
"""
import json
import sys
from collections import Counter
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import audit_uat_cases as u  # noqa: E402

results = json.loads((ROOT / "docs" / "audit" / "uat_results.json").read_text(encoding="utf-8"))
DEFECT = {"UAT-E04": "D-011", "UAT-S19": "D-011", "UAT-Z02": "D-012", "UAT-S15": "D-006", "UAT-F06": "D-006 / D-007", "UAT-S04": "D-008",
          "UAT-U04": "S-5", "UAT-U06": "S-3", "UAT-E13": "S-4", "UAT-E17": "D-004", "UAT-Z03": "D-007", "UAT-A02": "S-1"}
METHOD = {"browser": "Driven in the running application", "engine": "Same function the page calls, checked against an independent SQL figure",
          "unit": "Covered by named automated tests", "manual": "Needs a person / real infrastructure"}
FILL = {"Pass": "D9EAD3", "FAIL": "F4CCCC", "Not run": "FFF2CC"}
HEAD = PatternFill("solid", fgColor="1A4D8F")

wb = Workbook()

# --- summary ---------------------------------------------------------------------------------------------------------------------------
ws = wb.active
ws.title = "Summary"
total = len(u.C)
stat = Counter(results.get(c["id"], {}).get("status", "Not run") for c in u.C)
ws.append(["MOP Reconciliation - user acceptance test (audit Phase 7)"])
ws["A1"].font = Font(bold=True, size=14)
ws.append(["Run on 24 Nov 2026 against a throw-away COPY of the full April-November run (5,584,483 bill lines, 228 dates) on the development PC. Nothing was run against production data."])
ws.append([])
ws.append(["Cases", total])
for k in ("Pass", "FAIL", "Not run"):
    ws.append([k, stat.get(k, 0)])
ws.append([])
ws.append(["By area", "Pass", "FAIL", "Not run"])
for a in dict.fromkeys(c["area"] for c in u.C):
    cs = [results.get(c["id"], {}).get("status", "Not run") for c in u.C if c["area"] == a]
    ws.append([a, cs.count("Pass"), cs.count("FAIL"), cs.count("Not run")])
ws.append([])
ws.append(["By method", "Cases", "Pass"])
for m, label in METHOD.items():
    cs = [c for c in u.C if c["method"] == m]
    ws.append([label, len(cs), sum(1 for c in cs if results.get(c["id"], {}).get("status") == "Pass")])
ws.append([])
ws.append(["What this UAT found"])
ws.cell(ws.max_row, 1).font = Font(bold=True)
ws.append(["D-011 (P2)", "A manual or bulk match did not move the Summary matrix: after a bulk accept the matrix still counted matched bills as receivable while the store sheet did not. Found by case E04, fixed, re-run, and covered by 5 new tests."])
ws.append(["D-012 (P3)", "The Masters page showed dates in ISO form (2026-11-14). Found by case Z02, fixed, re-scanned."])
ws.append(["Case wording corrected", "S11 (a year with no data) now describes the later rule that says the period is not reconciled instead of showing zeros; T06 compares live tasks only."])
ws.append([])
ws.append(["What this UAT did NOT cover"])
ws.cell(ws.max_row, 1).font = Font(bold=True)
for line in ["Real business users: every case was executed by the audit team, not by finance staff. Their sign-off is still needed.",
             "The production server: nothing here was run on it. The application is currently not running there.",
             "A real file picker: uploads are checked by calling the same validation the page calls, not by choosing a file in the browser.",
             "A reconciliation run: not started from the Run page (it changes the database); an earlier full run was compared with the server's run on all 228 dates.",
             "Sign-in by typing a correct password: the session was created directly in the UAT database; the wrong-password path was typed.",
             "Screen readers and keyboard-only use.",
             "Concurrent users beyond the 4-user load measurement in the performance section of the tracker."]:
    ws.append(["", line])
ws.column_dimensions["A"].width = 34
ws.column_dimensions["B"].width = 120
for row in ws.iter_rows():
    for c in row:
        c.alignment = Alignment(wrap_text=True, vertical="top")

# --- cases ---------------------------------------------------------------------------------------------------------------------------------
ws = wb.create_sheet("UAT cases")
ws.append(["ID", "Area", "Who", "Scenario", "Steps", "Expected result", "How it was run", "Status", "What was seen (evidence)", "Date", "Defect found / related"])
for c in u.C:
    r = results.get(c["id"], {})
    ws.append([c["id"], c["area"], u.PERSONAS[c["who"]], c["scenario"], c["steps"], c["expected"], METHOD[c["method"]],
               r.get("status", "Not run"), r.get("evidence", "No result recorded."), r.get("executed_at", ""), DEFECT.get(c["id"], "")])
    ws.cell(ws.max_row, 8).fill = PatternFill("solid", fgColor=FILL.get(r.get("status", "Not run"), "EEEEEE"))
for cell in ws[1]:
    cell.font = Font(bold=True, color="FFFFFF")
    cell.fill = HEAD
for row in ws.iter_rows():
    for c in row:
        c.alignment = Alignment(wrap_text=True, vertical="top")
for i, w in enumerate([10, 14, 26, 38, 44, 44, 26, 9, 80, 16, 14], 1):
    ws.column_dimensions[get_column_letter(i)].width = w
ws.freeze_panes = "B2"
ws.auto_filter.ref = ws.dimensions

# --- not run -------------------------------------------------------------------------------------------------------------------------------
ws = wb.create_sheet("Not run")
ws.append(["ID", "Scenario", "Reason"])
for c in u.C:
    r = results.get(c["id"], {})
    if r.get("status", "Not run") != "Pass":
        ws.append([c["id"], c["scenario"], r.get("evidence", "No result recorded.")])
for cell in ws[1]:
    cell.font = Font(bold=True, color="FFFFFF")
    cell.fill = HEAD
for row in ws.iter_rows():
    for c in row:
        c.alignment = Alignment(wrap_text=True, vertical="top")
ws.column_dimensions["A"].width, ws.column_dimensions["B"].width, ws.column_dimensions["C"].width = 10, 60, 100

out = ROOT / "docs" / "MOP_UAT_TEST_CASES.xlsx"
wb.save(out)
print(f"{out.name}: {total} cases; {dict(stat)}")
