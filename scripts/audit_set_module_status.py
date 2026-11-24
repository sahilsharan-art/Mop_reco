"""Record the audit status of every application module in docs/audit/module_status.json (audit Phase 9).

Each status says what was actually done. 'Verified and working' means an independent re-calculation or an acceptance case proved it AND no open finding
remains against it; 'Working with limitations' means it works but a finding is open or part of it was not examined; the limit is written in `findings`.
Nothing is marked verified on the strength of reading alone. Existing entries are kept unless this file names the module.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "docs" / "audit" / "module_status.json"
st = json.loads(P.read_text(encoding="utf-8"))
V, L, N, U = "Verified and working", "Working with limitations", "Not yet audited", "Unable to verify"
M = "app/mop_reco/"

rows = [
    # (path, status, evidence, findings, severity, phase)
    (M + "r1_bill_txn.py", V, "Independent oracle written from the spec: 16 hand-asserted + 300 random collision scenarios agree (tests/test_audit_r1_oracle.py); live re-performance on 5.58M lines", "D-003 (un-deduplicated Paytm frame gave an obscure error) fixed", "P3", "2"),
    (M + "r1_chunked.py", V, "Full-range parity: 5,584,483 bill lines compared line by line against the per-row engine, 0 differences (CLAUDE.md 6.38); production run identical on 228 dates", "", "", "2"),
    (M + "store_sheet.py", V, "Whole-paise recomputation of C, G, K, M, N, carry-forward and store start (89 tests); live identities on 45,580 store-days", "O-1: control (g) hard-codes 20 July (needs approval)", "Observation", "2"),
    (M + "store_sheet_source.py", L, "Live and persisted figures agree; UAT cases S02, S10, S12, S19", "D-001, D-002, D-011 fixed; the slow legacy path (build) is still used by exports and is slow", "P2", "2,7"),
    (M + "store_sheet_export.py", V, "tests/test_store_sheet.py (five tabs, live formulas); string-as-formula guard added", "S-4 fixed for this writer", "P2", "5"),
    (M + "cms_blocks.py", V, "Whole-paise reference vs model: 88 tests; three structural controls hold over 225 stores x 228 days (UAT C02)", "", "", "2"),
    (M + "cms_blocks_export.py", L, "UAT C06: 14,329 live formula cells in the April workbook", "No dedicated unit test of the workbook; S-4 guard added", "P3", "7"),
    (M + "cms_reco.py", N, "", "Not examined in this audit (CMS Reco page uses cms_blocks)", "", ""),
    (M + "float_tracker.py", V, "Independent streak, threshold boundary and four years of calendar rules: 81 tests; live float grid identical on 45,580 store-days", "", "", "2"),
    (M + "mop_matrix.py", L, "Matrix engine verified by UAT S01-S09 and S19", "D-011 (matched bills with unknown payment mode stayed receivable) fixed", "P2", "7"),
    (M + "manual_match.py", V, "Concurrency (2 racing operators), atomicity under forced failure, UAT E03-E08", "D-005 (two silent warehouse reads) now logged", "P3", "3,6,7"),
    (M + "bulk_accept.py", L, "Atomicity proven; UAT E09 applies and refuses correctly", "D-011: validated without the payment mode (fixed)", "P2", "7"),
    (M + "exception_pairing.py", L, "Suggestions verified by bulk-accept UAT", "D-011: the pool dropped the payment mode (fixed)", "P2", "7"),
    (M + "exception_tables.py", V, "One column definition for screen, export and upload; UAT E13; round-trip test", "", "", "7"),
    (M + "exceptions.py", V, "Deterministic keys, remark history (UAT E12), register integrity checks on the live database", "", "", "3,7"),
    (M + "exception_remarks.py", N, "", "Not examined in this audit", "", ""),
    (M + "refunds.py", L, "Rules by test (tests/test_refunds.py, test_lifecycle.py); atomic apply", "D-004 fixed (a failed store-master read became an empty master). The folder-import rules were read, not exercised on the real folder", "P3", "3,6"),
    (M + "table_upload.py", L, "Atomic apply proven (mixed matches, refunds, remarks)", "Not read line by line", "", "6"),
    (M + "uploads.py", L, "Validation tests (type, month, missing column), delete and archive tests", "S-5 (P1) fixed; S-6 zip guard. A real file picker and the large-file fast path were not exercised in UAT", "P1", "5,7"),
    (M + "upload_prefetch.py", V, "Hostile-input test: parameters bound, nothing injected (C-005 disproved)", "", "", "5"),
    (M + "queries.py", L, "The paths UAT uses return figures equal to SQL", "1,941 lines; not read in full", "", "7"),
    (M + "gateway_txns.py", L, "Strip sums verified (UAT S07)", "", "", "7"),
    (M + "store_tasks.py", L, "Tiles, search, closure on match verified (UAT T01, T02, T04); reply never closes a task (tests)", "D-005 (CMS dates read now logged); not read line by line", "P3", "3,7"),
    (M + "store_task_summary.py", V, "UAT T06; HTML escaping added", "S-3 fixed here", "P2", "5"),
    (M + "lifecycle.py", V, "Read in full (40 lines); idempotent closure sync; tests", "", "", "6"),
    (M + "masters.py", L, "Tests; alias NLD to NEG; schema guard", "D-005 (alias lookup failure now logged)", "P3", "3"),
    (M + "master_gaps.py", V, "UAT Z04: unresolved identifiers listed by name", "", "", "7"),
    (M + "ingest.py", L, "D-001 found and fixed here; POS, Paytm, Axis, Bajaj, PTC and CMS proven by full-run comparison; gateway arithmetic checked", "2,100+ lines; not read in full", "P1", "2"),
    (M + "io_.py", L, "File reading tests", "S-6: zip size guard added", "P3", "5"),
    (M + "runs.py", L, "Proven by the full production run and its comparison (CLAUDE.md 6.39)", "A run was not started in this audit; failure handling read only", "", ""),
    (M + "run_coverage.py", V, "Readiness per month by test; banner behaviour seen on every page", "", "", "7"),
    (M + "pos_completeness.py", V, "Flagged the cut-off November file (209,757 of 1,187,732 bill numbers missing) on every page", "", "", "7"),
    (M + "parking.py", N, "", "Not exercised: Axis data is present, so nothing is parked", "", ""),
    (M + "config.py", N, "", "Not examined in this audit", "", ""),
    (M + "db.py", L, "Schema, constraints and live integrity checks passed (Phase 3)", "O-2 (no unique key on bill lines), O-3 (redundant indexes), O-4 (table growth)", "Observation", "3"),
    (M + "export.py", V, "tests/test_audit_export_formula_injection.py (8); 35-digit ids stay text; UAT E13", "S-4 fixed", "P2", "5,7"),
    (M + "formatting.py", L, "Date and money formats scanned on every page (UAT Z02)", "O-7: counts are grouped the Western way, money the Indian way", "Observation", "7"),
    (M + "r3a_store_cash.py", L, "tests/test_r3a_store_cash.py", "Not used by the application pages (the live cash book is store_sheet); kept for a script and tests", "", ""),
    (M + "__init__.py", N, "", "", "", ""),
    ("app/dashboard_common.py", L, "UI audit at 375, 768, 1366, 1920 px; chrome tests", "D-008, D-009, D-010, S-3, S-1-related fixed", "P2", "4,5"),
    ("app/inline_match.py", N, "", "Kept in the tree but no page uses it (CLAUDE.md 6.9c-ii); not examined", "", ""),
    ("app/masters_editor.py", L, "Rules by test (tests/test_masters_db.py)", "The editing screen was not exercised in UAT", "", ""),
    ("app/match_strip.py", V, "UAT S07: strips add up and flag a mismatch", "", "", "7"),
    ("app/run_panel.py", U, "", "UAT R01 not run: the Run section changes the database", "", "7"),
    ("app/selfcheck.py", V, "Checks every module reference on every page load; tests", "", "", "3"),
    ("app/store_detail_view.py", L, "Build-the-sheet button path read; export tested", "Drill-down not exercised in UAT", "", ""),
    ("app/Summary.py", L, "UAT S01-S17; UI audit; D-006", "D-006, D-008 fixed. Large page (about 900 lines) read in part", "P2", "4,7"),
    ("app/pages/1_Float.py", V, "UAT F01, F05, F06; D-006 and D-007 fixed", "", "", "4,7"),
    ("app/pages/2_Cash_position.py", V, "UAT C02, C03, C06, C07; the page loads in about 5 s (wide table)", "", "", "7"),
    ("app/pages/5_Masters.py", L, "Master tests; page loads at four widths", "D-012 (ISO dates) fixed; editing screen not exercised", "P3", "7"),
    ("app/pages/6_Upload.py", L, "Validation tests; history escaping", "S-3 fixed; a real file picker not exercised", "P2", "5"),
    ("app/pages/7_Store_Task.py", V, "UAT T01, T02, T04", "", "", "7"),
    ("app/pages/8_Exceptions.py", V, "UAT E02-E15", "", "", "7"),
    ("app/pages/9_Users.py", L, "Access refusal for non-administrators tested; trail tests", "The add-user screen was not exercised in the browser", "", ""),
]
for path, status, ev, findings, sev, phase in rows:
    st[path] = {"status": status, "evidence": ev, "findings": findings, "severity": sev, "phase": phase}
st["_comment"] = ("Statuses written by scripts/audit_set_module_status.py at Phase 9 and kept in step with the evidence; see docs/audit/MOP_AUDIT_TRACKER.md. "
                  "Scripts (the 52 files in scripts/) are operational tooling: those created by the audit are marked as such, the rest are not audited.")
P.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
from collections import Counter
print(len(rows), "modules recorded;", dict(Counter(r[1] for r in rows)))
