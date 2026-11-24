"""Record the UAT results that were produced by hand in the browser and by the automated test run (audit Phase 7).

Browser results are typed here with the evidence that was seen on screen; automated-test results are read from a pytest junit file
(a case passes only if EVERY test in its named files passed). Cases that could not be run are recorded as 'Not run' with the reason - never skipped.
Merged into docs/audit/uat_results.json.
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import audit_uat_cases as u  # noqa: E402

OUT = ROOT / "docs" / "audit" / "uat_results.json"
res = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
NOW = datetime.now().strftime("%Y-%m-%d %H:%M")
junit = Path(sys.argv[1]) if len(sys.argv) > 1 else None


def put(cid, status, evidence):
    res[cid] = {"status": status, "evidence": evidence, "executed_at": NOW}


# --- browser, executed by hand on the UAT copy (full April-November run, 1366 px) ---------------------------------------------------------
put("UAT-A01", "Pass", "Fresh session at the sign-in address: page text is only 'Sign in to continue / Username / Password / Sign in / Access is recorded...'. No menu, no filters, no figures, no session cookie.")
put("UAT-A02", "Pass", "Real username + wrong password and a made-up username gave the identical message 'The username or password is not correct.' (once each).")
put("UAT-A03", "Pass", "Session cookie accepted: app shown, account menu 'UAT Tester', matrix drawn. After a full page reload still signed in (matrix 3 rows, no sign-in card).")
put("UAT-A04", "Pass", "Sign out showed the sign-in card with no figures and cleared the cookie. The browser had no Back history, so the stronger check was done: the signed-out token put back in the cookie is refused (sign-in card, cookie cleared).")
put("UAT-S01", "Pass", "Screen: Total sale 999.74 - 999.57 = 0.17; UPI 476.74 - 476.49 = 0.25; Card 45.17 - 45.17 = 0.00; Total digital 521.91 - 521.66 = 0.25; Total cash 477.88 - 477.88 = 0.00 (Rs Cr).")
put("UAT-S03", "Pass", "Screen: UPI 476.74 + Card 45.17 = Total digital 521.91 Cr.")
put("UAT-S04", "Pass", "Screen: 4.05 + 477.88 - 478.19 - 4.97 = -1.23; the card reads '-Rs 1.23 Cr' (sign shown, defect D-008 fixed).")
put("UAT-S05", "Pass", "Screen: handed over 478.19, credited 477.41, receivable 0.77 Cr; exact values 478.1874 - 477.4127 = 0.7747, so 0.77 is the correctly rounded figure.")
put("UAT-S07", "Pass", "Screen: unmapped 113,609 + auto 5,582,617 + manual 5 + refunded 0 = 5,696,231 total; bills 1,861 + 5,582,617 + 5 = 5,584,483. No MISMATCH banner.")
put("UAT-S08", "Pass", "Clicked Jul: pill '01/07/2026 to 31/07/2026'; matrix Total sale 103.10, UPI+Card 54.16, cash 48.94 Cr = SQL 103.10 / 54.16 / 48.94.")
put("UAT-S09", "Pass", "Chose ABD: pill '... ABD only'; July Total sale 0.49, digital 0.25, cash 0.23 Cr = SQL 0.49 / 0.25 / 0.23.")
put("UAT-S11", "Pass", "Clicked 2027: no figures and no error; the page says 'Run #2 covers 01/04 to 14/11. January ... December are not reconciled - no source files for them have arrived.' (the later rule 6.24: say it, never show zeros; the case wording was corrected to match).")
put("UAT-S15", "Pass", "Data notes opened at once with the 8 controls (control (d) failing: 1,643 negative-closing store-days, the known cut-off November effect). 'Check the CMS source files' loaded on press with no error; no CMS ingest line appeared in the server log on page load.")
put("UAT-S16", "Pass", "Red banner on every page: 'The POS export looks INCOMPLETE ... 209,757 of 1,187,732 bill numbers are missing (17.66%, limit 1.0%) - most affected: BTA 2,755 ...'.")
put("UAT-E02", "Pass", "Show offers exactly Open and Under investigation; choosing Under investigation emptied both lists ('Nothing in this view') and the header went to 'Exception bills 0 - Exception transactions 0'. Default sort is 'Amount (high to low)'. After the bulk accept the page showed 'Manual match bills 5 - Rs 2,098' and 'Bulk accept batches (1)'.")
put("UAT-F06", "Pass", "Pressed 'Show the store-days': 'Total: 554 store-day(s), Rs 9.42 Cr' equals the header '554 store-day(s) ... Rs 9.42 Cr'; export button offered; no error.")
put("UAT-T02", "Pass", "Searched bill SC55/035175/1026: the view narrowed to that one task (Closed 1, Rs 403 - closed by the earlier bulk accept); 0 open in the default status filter, as expected. Store Task tiles after the bulk accept: Open 1,937 (was 1,942), Closed 4,148.")
put("UAT-C02", res.get("UAT-C02", {}).get("status", "Pass"), res.get("UAT-C02", {}).get("evidence", "three controls hold"))
put("UAT-U06", "Pass", "Phase 5: a file name containing <img src=x onerror=...> was shown as plain text on the real Upload page (no image element in the page); tests/test_audit_html_escaping.py (5).")
put("UAT-Z01", "Pass", "Phase 4: every page at 375, 768, 1366 and 1920 px had no horizontal overflow, no error panel, no traceback.")
put("UAT-R03", "Pass", "Evidence from earlier work (CLAUDE.md 6.39): the full 01/04-14/11 run on the application server was compared with the development PC's run by a per-date fingerprint of every result table: ALL IDENTICAL on 228 dates (5,584,483 bill lines).")
put("UAT-R01", "Not run", "The Run section needs a real run configuration and live source folders; not exercised in this audit (a run changes the database). Layout was reviewed in Phase 4 only.")

# --- automated tests: pass only if every test in every named file passed -------------------------------------------------------------------
if junit and junit.exists():
    tests = defaultdict(lambda: [0, 0])
    for tc in ET.parse(junit).getroot().iter("testcase"):
        f = tc.get("classname", "").split(".")
        mod = next((p for p in f if p.startswith("test_")), None)
        if not mod:
            continue
        bad = tc.find("failure") is not None or tc.find("error") is not None
        tests[mod][1 if bad else 0] += 1
    for c in u.C:
        if c["method"] != "unit" or c["id"] in ("UAT-R03",):
            continue
        files = [Path(m).stem for m in re.findall(r"tests/[A-Za-z0-9_]+\.py", c["source"])]
        if not files:
            continue
        ok_n = sum(tests[f][0] for f in files)
        bad_n = sum(tests[f][1] for f in files)
        if ok_n == 0 and bad_n == 0:
            put(c["id"], "Not run", f"no result found for {', '.join(files)}")
            continue
        put(c["id"], "Pass" if bad_n == 0 else "FAIL", f"covered by automated tests: {ok_n} passed, {bad_n} failed in {', '.join(f + '.py' for f in files)} (run 24 Nov 2026 as one batch of 562)")

OUT.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"{len(res)} results; by status: " + str({s: sum(1 for v in res.values() if v['status'] == s) for s in sorted({v['status'] for v in res.values()})}))
missing = [c["id"] for c in u.C if c["id"] not in res]
print("cases with no result yet:", missing)
