"""The UAT case catalogue (audit Phase 7). Each case is a business scenario in a person's own words: who does it, what they do, what they must see.

`method` says how it is executed in this audit:
  browser  - driven in the running application (local copy of the full April-November run)
  engine   - the same function the page calls, run against the UAT database, result compared with an independent SQL figure
  unit     - covered by an automated test (named in `source`), not repeated by hand
  manual   - needs a person, a real file picker or real infrastructure; NOT executed by the audit
Results live in docs/audit/uat_results.json (id -> status, evidence), filled in as cases are run; a case with no result is "Not run".
"""

PERSONAS = {
    "FA": "Finance analyst (reads Summary, Cash position)",
    "HO": "Head-office operator (Exceptions, matching, refunds)",
    "SL": "Store-operations lead (Store Task, Float)",
    "AD": "Administrator (Upload, Run, Masters, Users)",
    "AU": "Auditor (read-only checks, exports, history)",
}

C = []


def case(id, area, who, scenario, steps, expected, method, source=""):
    C.append(dict(id=id, area=area, who=who, scenario=scenario, steps=steps, expected=expected, method=method, source=source))


# --- access ---------------------------------------------------------------------------------------------------
case("UAT-A01", "Access", "AU", "Nobody sees data before signing in", "Open the application address in a new browser with no session.", "Only the sign-in card is shown: no menu, no figures, no filters.", "browser")
case("UAT-A02", "Access", "AU", "A wrong password gives one generic message", "Enter a real username and a wrong password; then a made-up username.", "The same message both times: 'The username or password is not correct.'", "browser")
case("UAT-A03", "Access", "FA", "A signed-in session survives a page refresh", "Sign in, press refresh.", "The Summary opens without asking again.", "browser")
case("UAT-A04", "Access", "FA", "Signing out ends the session", "Choose Sign out, then press the browser Back button.", "The sign-in card is shown; the previous page's figures are not.", "browser")
case("UAT-A05", "Access", "AU", "An ordinary user cannot open the Users page", "Sign in as a non-administrator, open /Users directly.", "Refused; the Users tab is not in the menu.", "unit", "tests/test_login_gate.py")
case("UAT-A06", "Access", "AU", "Five wrong passwords lock the account", "Enter a wrong password five times in a row.", "The account is locked for 15 minutes and the message says how long; an administrator can unlock it.", "unit", "tests/test_auth.py")
case("UAT-A07", "Access", "AD", "A temporary password must be replaced at first sign-in", "Create a user with a temporary password and sign in as them.", "They must choose their own password (10+ characters, letter and digit) before anything else.", "unit", "tests/test_auth.py")

# --- summary --------------------------------------------------------------------------------------------------
case("UAT-S01", "Summary", "FA", "In every column of the matrix, Sale minus Collected equals Receivable", "Open Summary; read Sale, Collected and Receivable for UPI, Card, Total digital, Total cash and Total sale.", "Sale - Collected = Receivable in each column, to the displayed precision.", "browser")
case("UAT-S02", "Summary", "FA", "Total sale in the matrix equals the store sheet total", "Compare the Total sale figure with the sum of column C over all store-days.", "Equal.", "engine")
case("UAT-S03", "Summary", "FA", "Total digital equals UPI plus Card", "Read the three figures.", "Equal.", "browser")
case("UAT-S04", "Summary", "FA", "The Cash Position card adds up", "Opening + Add: cash sale - Less: deposit - Less: net cash expense, against Closing balance at store.", "Equal; a negative closing balance shows a minus sign.", "browser")
case("UAT-S05", "Summary", "FA", "The Cash With Agency card adds up", "Cash handed over - Cash credited by bank, against Cash receivable.", "Equal; never negative.", "browser")
case("UAT-S06", "Summary", "FA", "The bills strip adds up and agrees with the database", "Closed auto + Closed manual + Open against Total; compare each with a count of the bill lines.", "Equal on both counts.", "engine")
case("UAT-S07", "Summary", "FA", "The transactions strip adds up", "Mapped auto + Mapped manual + Refunded + Unmapped against Total.", "Equal; no MISMATCH banner.", "browser")
case("UAT-S08", "Summary", "FA", "Picking a month filters every figure", "Click Jul in the sidebar.", "The range reads 01/07/2026 to 31/07/2026 and the matrix equals the July totals from the database.", "browser")
case("UAT-S09", "Summary", "FA", "Choosing one store filters every figure", "Choose a store in the Store box.", "Totals equal that store's rows in the database; the pill says 'XXX only'.", "browser")
case("UAT-S10", "Summary", "FA", "A custom date range filters every figure", "Type From 10/07/2026 and To 15/07/2026.", "Totals equal the database for those six dates.", "engine")
case("UAT-S11", "Summary", "FA", "A year with no data shows an empty page, not an error", "Click 2027.", "No figures and no error; the page says the period is not reconciled (run covers 01/04 to 14/11).", "browser")
case("UAT-S12", "Summary", "FA", "Store-wise detail has one row per loaded date plus a total", "Open Store-wise detail; Expand all.", "228 date rows and a TOTAL row; the total equals the sum of the rows.", "engine")
case("UAT-S13", "Summary", "FA", "Matched section lists matched bills with the date lag flagged", "Open Matched.", "Bills with their transaction, gateway and lag; a non-zero lag is flagged.", "engine")
case("UAT-S14", "Summary", "HO", "Exception counts on Summary equal the Exceptions page", "Compare the Exceptions section counts with the Exceptions page tiles.", "Equal.", "engine")
case("UAT-S15", "Summary", "AU", "Data notes opens quickly and the CMS check loads on request", "Open Data notes; press 'Check the CMS source files'.", "Controls shown at once; the CMS part loads after the press with no error and no files rewritten on a normal view.", "browser", "tests/test_audit_page_load_reads.py")
case("UAT-S16", "Summary", "FA", "An incomplete source file is announced on every page", "Open any page with the November POS file in place.", "A red banner says the POS export looks incomplete, with the counts.", "browser")
case("UAT-S17", "Summary", "FA", "Cash receivable is never negative anywhere", "Scan the detail table and the cards.", "No negative cash receivable; credits above deposits are listed on Float as a data-quality item.", "engine")
case("UAT-S18", "Summary", "FA", "The Excel sheet export has five tabs with live formulas", "Press Build the Excel sheet.", "By date, By store and date, By store, Excluded types, Controls; formulas for C, G, K, M, N.", "unit", "tests/test_store_sheet.py")

case("UAT-S19", "Summary", "FA", "The matrix and the store-wise sheet agree after a bulk accept", "Bulk-accept several exact matches; compare the matrix Total digital Receivable with the store sheet column G total.", "Equal, and both lower by the amount matched (found as defect D-011: they disagreed).", "engine")

# --- exceptions and matching ------------------------------------------------------------------------------------
case("UAT-E01", "Exceptions", "HO", "The tiles equal the database", "Open Exceptions; compare Open bills and Open transactions with counts.", "Equal (with the documented merged lines).", "engine")
case("UAT-E02", "Exceptions", "HO", "Show and Sort change the list", "Choose Under investigation; then Sort by amount.", "Filter applies; amounts descend.", "browser")
case("UAT-E03", "Exceptions", "HO", "A clean match closes both sides", "Match an open bill to its exact transaction.", "The bill and the transaction leave the open lists; both appear under manual matches with who, when and how.", "engine")
case("UAT-E04", "Exceptions", "FA", "A manual match updates Summary without a re-run", "After E03, reopen Summary.", "Closed - manually matched +1, Open -1 in the strips; digital receivable down by the bill amount.", "engine")
case("UAT-E05", "Exceptions", "HO", "A transaction already used cannot back a second bill", "Paste a used transaction against another bill.", "Refused, naming the bill that holds it.", "engine", "tests/test_manual_match.py")
case("UAT-E06", "Exceptions", "HO", "A mismatch needs a written reason", "Match with a different amount.", "Not accepted on one click: the problems are listed and an override reason is required.", "engine", "tests/test_manual_match.py")
case("UAT-E07", "Exceptions", "HO", "An id found nowhere is only asserted", "Paste an id that exists in no gateway.", "Recorded as 'asserted unverified'; the bill stays open.", "engine", "tests/test_manual_match.py")
case("UAT-E08", "Exceptions", "HO", "A match can be reversed", "Reverse the match from E03.", "Both return to the open lists; the record is kept as reversed; a new task is raised.", "engine")
case("UAT-E09", "Exceptions", "HO", "Bulk accept of exact suggestions is previewed, then applied", "Press the exact bulk-accept button; review; confirm.", "A preview lists every row; applying records them as one batch; refused rows are named with the reason.", "engine")
case("UAT-E10", "Exceptions", "HO", "Bulk accept of split payments", "Press the split button; confirm.", "Both transactions consumed, the bill closed once.", "unit", "tests/test_bulk_accept.py")
case("UAT-E11", "Exceptions", "HO", "A customer refund needs name, Zoho number and date", "Record a refund leaving one of the three blank.", "Refused; with all three it closes the transaction only, never a bill.", "unit", "tests/test_lifecycle.py")
case("UAT-E12", "Exceptions", "HO", "A remark is kept with history", "Add a remark to an exception, then a second one.", "Both appear, newest first, with who and when; none is lost.", "engine")
case("UAT-E13", "Exceptions", "AU", "Export covers every filtered row, ids as text, safe to open", "Export exception bills.", "Row count equals the filtered count; 35-digit ids intact; a remark starting '=' is text.", "engine", "tests/test_audit_export_formula_injection.py")
case("UAT-E14", "Exceptions", "HO", "An exported file filled in comes back as a validated upload", "Fill Txn 1 in the export and upload it.", "A preview first; any rejected row stops everything; one confirmation applies all.", "unit", "tests/test_lifecycle.py")
case("UAT-E15", "Exceptions", "AU", "Closed items remain reachable", "Choose Status Closed.", "Closed and auto-resolved items are listed, nothing deleted.", "engine")
case("UAT-E16", "Exceptions", "HO", "Two bills paid by one swipe", "Type the same transaction in both bills (amounts add up exactly).", "Both close against the one transaction; a third bill cannot take it; reversal reverses both.", "unit", "tests/test_manual_match.py")
case("UAT-E17", "Exceptions", "HO", "A failure half way through a bulk action leaves nothing behind", "(Forced) fail the third of five matches.", "No match, no batch, nothing closed; the same action then succeeds.", "unit", "tests/test_audit_atomicity.py")

# --- cash position ------------------------------------------------------------------------------------------------
case("UAT-C01", "Cash position", "FA", "Each store block chains correctly", "For a store, check Opening + Deposit - CMS (same day) - CMS (next day) = Closing, and next opening = this closing.", "Holds for every block.", "unit", "tests/test_audit_cms_blocks.py")
case("UAT-C02", "Cash position", "FA", "The structural controls hold", "Open Cash position.", "All three controls pass; none is shown in red.", "browser")
case("UAT-C03", "Cash position", "FA", "A credit later than the next day is listed, not hidden", "Open 'Credited later than day+1'.", "Listed with lag; T+10 or more flagged exceptional.", "engine")
case("UAT-C04", "Cash position", "FA", "A remark on a store and period is saved with history", "Add a remark twice.", "Both kept with who and when.", "unit", "tests/test_cms_blocks.py")
case("UAT-C05", "Cash position", "FA", "A bank-figure override needs a reason and can be undone", "Record an override, then revert it.", "Applied with reason and who; revert restores the bank's figure; both are in the history.", "unit", "tests/test_cms_blocks.py")
case("UAT-C06", "Cash position", "AU", "The workbook carries live formulas", "Export the blocks.", "Closing columns are formulas, not pasted numbers.", "engine")
case("UAT-C07", "Cash position", "FA", "ZPO and adjustment rows are listed outside the grid", "Open the excluded-types list.", "ZPO (no dates) and ADJ rows are shown with counts and values.", "engine", "tests/test_audit_cms_blocks.py")

# --- float ----------------------------------------------------------------------------------------------------------
case("UAT-F01", "Float", "SL", "The tiles equal the grid", "Open Float; compare tile counts with the grid's last date.", "Equal.", "engine")
case("UAT-F02", "Float", "SL", "Exactly at the threshold is not high", "A store-day with float of exactly 10,000.00 and one with 10,000.01.", "Only the second is flagged.", "unit", "tests/test_audit_float.py")
case("UAT-F03", "Float", "SL", "Weekend float is explained automatically, and a person's remark wins", "View a Sunday; add your own remark.", "'Weekend' appears in grey; once you remark, your text stays and the system never overwrites it.", "unit", "tests/test_audit_float.py")
case("UAT-F04", "Float", "SL", "A bulk remark for a store-day that was not high is rejected", "Upload a remark for a store-day under threshold.", "That row is rejected with the reason; other rows are not blocked.", "unit", "tests/test_float_tracker.py")
case("UAT-F05", "Float", "FA", "Negative float is shown, never floored", "Open the negative-float section.", "Each store-day with deposit above opening, most negative first, with an export.", "engine")
case("UAT-F06", "Float", "FA", "The credits-above-deposits list loads on request and matches its header", "Press Show the store-days.", "The table total equals the header total.", "browser")

# --- store task --------------------------------------------------------------------------------------------------------
case("UAT-T01", "Store Task", "SL", "The status tiles add up", "Open Store Task; Open + Pending + WIP + Closed against all tasks.", "Equal.", "engine")
case("UAT-T02", "Store Task", "SL", "Search finds a task by bill number", "Type a bill number in Search.", "Only that task is listed.", "browser")
case("UAT-T03", "Store Task", "SL", "A store reply can only be uploaded for its own task", "Upload a reply naming another store.", "Rejected; others are unaffected.", "unit", "tests/test_store_tasks.py")
case("UAT-T04", "Store Task", "SL", "A task closes itself when the bill is matched", "Match the bill (E03); reopen Store Task.", "The open-bill task is closed 'resolved by the data'.", "engine")
case("UAT-T05", "Store Task", "SL", "A reply alone never closes a task", "Record a reply to an open bill task.", "Status becomes Pending, not Closed.", "unit", "tests/test_store_tasks.py")
case("UAT-T06", "Store Task", "FA", "The summary by type agrees with the database", "Open the type summary on Summary.", "Counts and values equal the task table.", "engine")

# --- upload ---------------------------------------------------------------------------------------------------------------
case("UAT-U01", "Upload", "AD", "A wrong file type is refused", "Upload a .pdf as a Paytm settlement.", "Refused with the allowed types.", "unit", "tests/test_audit_upload_validation.py")
case("UAT-U02", "Upload", "AD", "A file for the wrong month is refused", "Upload a July file as August.", "Refused, saying the dates are not in the chosen month.", "unit", "tests/test_audit_upload_validation.py")
case("UAT-U03", "Upload", "AD", "A missing column is named", "Upload a file without BILLDATE.", "Refused: names the missing column.", "unit", "tests/test_audit_upload_validation.py")
case("UAT-U04", "Upload", "AU", "A hostile file name cannot write outside the folder", "Upload a file named ..\\..\\x.csv.", "Stored inside the source folder under a safe name; nothing written elsewhere.", "unit", "tests/test_audit_upload_path.py")
case("UAT-U05", "Upload", "AD", "Delete moves the file and keeps the history line", "Delete an uploaded file.", "File moved to the archive; history keeps who/when; refused while a run is in progress.", "unit", "tests/test_uploads_delete.py")
case("UAT-U06", "Upload", "AD", "A file name is shown as text, not as markup", "View a history line whose name contains <img>.", "Shown literally.", "browser", "tests/test_audit_html_escaping.py")

# --- masters --------------------------------------------------------------------------------------------------------------
case("UAT-M01", "Masters", "AD", "An edit makes a new version and keeps the old", "Edit a master with a reason.", "A new version with who/why/when; the previous stays and can be restored.", "unit", "tests/test_masters_db.py")
case("UAT-M02", "Masters", "AD", "A new identifier mapped to two stores is blocked", "Add a MID that already maps to another store.", "Refused with the conflict named.", "unit", "tests/test_masters_db.py")
case("UAT-M03", "Masters", "FA", "A past date uses the master as it was then", "Resolve an identifier as at an earlier date.", "The earlier version answers; a later correction does not rewrite it.", "unit", "tests/test_masters_db.py")
case("UAT-M04", "Masters", "FA", "Replaced store codes count as one store", "A POS row for NEG and a gateway row for NLD.", "Both resolve to NEG.", "unit", "tests/test_store_aliases.py")

# --- run ---------------------------------------------------------------------------------------------------------------------
case("UAT-R01", "Run", "AD", "The Run page says what a run would read", "Open Upload, scroll to Run.", "Lists each source, files, dates and any gap.", "manual")
case("UAT-R02", "Run", "AD", "A month missing a required source is not offered for a run", "Look at a month with no POS file.", "Warns it is incomplete; the Run button is disabled until confirmed.", "unit", "tests/test_run_coverage.py")
case("UAT-R03", "Run", "AD", "A re-run reproduces the same figures", "Compare two runs table by table.", "Identical (development PC against the server on 228 dates).", "unit", "docs/BASELINE and CLAUDE.md 6.39")

# --- users ---------------------------------------------------------------------------------------------------------------------
case("UAT-V01", "Users", "AD", "An administrator adds a user and sees the temporary password once", "Add a user.", "Password shown once; not stored; user must change it.", "unit", "tests/test_auth.py")
case("UAT-V02", "Users", "AD", "Deactivating a user ends their sessions", "Deactivate a signed-in user.", "Their next action asks them to sign in.", "unit", "tests/test_auth.py")
case("UAT-V03", "Users", "AD", "The last administrator cannot be removed", "Deactivate or demote the only admin.", "Refused.", "unit", "tests/test_auth.py")
case("UAT-V04", "Users", "AU", "The sign-in trail is kept", "Open the trail after several sign-ins and failures.", "Every sign-in, failure and lock is listed; nothing is deletable.", "unit", "tests/test_auth.py")

# --- cross-cutting -------------------------------------------------------------------------------------------------------------
case("UAT-Z01", "All pages", "AU", "No page overflows sideways at 375, 768, 1366 and 1920 px", "Open each page at each width.", "No horizontal page scroll; no error panel.", "browser", "Phase 4")
case("UAT-Z02", "All pages", "FA", "Dates read DD/MM/YYYY and money uses Indian grouping everywhere", "Scan every page.", "01/07/2026; Rs 1,03,10,45,827.", "browser")
case("UAT-Z03", "All pages", "FA", "A page opens within a few seconds", "Open each page (warm).", "Under 5 seconds on the whole range; under 1 second for one month's data.", "engine", "Phase 5 timings")
case("UAT-Z04", "All pages", "AU", "Nothing is silently dropped: unmapped items are named", "Look at unresolved pickups / unmapped MIDs.", "Listed by name and value, never guessed.", "engine")
