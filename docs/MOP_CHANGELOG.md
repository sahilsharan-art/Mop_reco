# MOP Reconciliation - change log of the audit (24 Nov 2026)

Everything below is committed on the development branch and tested. **None of it is deployed to the application server yet**, and none of it is pushed. Baseline: tag `pre-audit-20261124` (rolling back = checking that tag out). The defect register with root causes, evidence and closure criteria is `docs/audit/MOP_DEFECT_REGISTER.md`; the running record of the audit is `docs/audit/MOP_AUDIT_TRACKER.md`.

No financial rule and no column meaning was changed. Where a fix changes what a person sees, it is stated.

## Financial and data correctness

| ID | Sev | Change | What a user sees |
|---|---|---|---|
| D-001 | P1 | CMS overlap key now includes the row TYPE (`ingest._cms_overlap_key`). A collection in one monthly file and its adjustment in a later file are different records | Cash received (column J) is correct for 5 store-days on the April-November run (net Rs 7,22,748 more received, cash receivable less by the same). Appears after the next run |
| D-002 | P3 | Stored store-day money rounded to 2 places at persistence | No visible change; removes noise such as 62552.9999999999 |
| D-003 | P3 | Plain error if R1 is handed duplicate Paytm ids | None in normal use |
| D-004 / D-005 | P3 | Six hidden failures now log their cause or stop with it (refund store-master read, warehouse reads in manual match, masters setting and alias, CMS dates in store tasks) | A real failure now says so instead of looking like "not found" |

## Security

| ID | Sev | Change |
|---|---|---|
| S-5 | **P1** | An uploaded file's name can no longer decide where it is written (it could escape the folder, even reach `C:\Windows\Temp` or a network share). Only a cleaned last path component is used, and the result is checked to sit inside the target folder |
| S-3 | P2 | File names, uploader names, store names and tile text are escaped before they reach raw HTML |
| S-4 | P2 | A typed remark beginning with `=` is kept as text in every Excel export and neutralised in CSV; URLs are no longer turned into links |
| S-1 | P2 | Response time no longer reveals whether a username exists |
| S-6 | P3 | A zip member declaring more than 4 GB is refused before it is read |
| - | - | `requirements.lock.txt`: the server's exact 63 packages, for a reproducible install |

## Performance and behaviour

| ID | Sev | Change | What a user sees |
|---|---|---|---|
| D-006 | P2 | Summary "Data notes" and Float "Credits exceeding deposits" no longer re-read every CMS file and rewrite warehouse files on each page view. The CMS part loads on a button press and is cached until a CMS file changes | Pages open quickly; the two sections show a button instead of loading automatically |
| D-007 | P2 | Float reads the precomputed store-day figures | Float opens in about 4 s warm (was far slower on the old path) |

## Interface

| ID | Sev | Change | What a user sees |
|---|---|---|---|
| D-008 | P2 | A negative closing balance on the Cash Position card keeps its minus sign | "-Rs 1.23 Cr" instead of "Rs 1.23 Cr" in red |
| D-009 | P3 | Darker text colours for text on tinted backgrounds (WCAG AA) | The amber Receivable figure and status pills are easier to read |
| D-010 | P3 | The +/- section toggles are 28x28 px | Easier to tap |

## Tests added by the audit (all pass)

Independent re-calculations of R1 (316), Float (81), CMS blocks (88) and the daily sheet and cash book (89); atomicity under forced failure (4); SQL injection (3); concurrency (2); page-load reads (18); statement card (4); contrast and touch targets (3); authentication timing (3); HTML escaping (5); export injection (8); upload paths (14); zip guard (4); regression tests for D-001 to D-004. Full-suite count is recorded in the tracker.

## To do before this reaches production

Deploy the build to the application server (the app is currently not running there), re-ingest the CMS source and re-run so D-001 and D-002 take effect, then compare the new run with run #2. Items that need a decision or IT are listed in the register (lock-out policy, firewall and HTTPS, the wide ERP login, retention).

## Added by the UAT (Phase 7)

| ID | Sev | Change | What a user sees |
|---|---|---|---|
| D-011 | P2 | A manual or bulk match now moves the Summary matrix whatever recorded the payment mode: the mode is carried through bulk accept, looked up when missing, and a matched bill is never left counted as receivable; an 'asserted, unverified' match still moves nothing | After a bulk accept the matrix's UPI / Total digital Receivable drops by the amount matched and agrees with the store-wise sheet (before: the two disagreed on the same page) |
| D-012 | P3 | Dates on the Masters page use DD/MM/YYYY | `14/11/2026` instead of `2026-11-14` |

## Added by the readiness review (Phase 8)

| Change | Why |
|---|---|
| `requirements.lock.txt` (the server's exact 63 packages) is included in the release package and preferred by `deploy/install.ps1` | A rebuild must install the proven set, not whatever is newest (S-9) |
| `tests/test_audit_package.py` (6) | The package must carry the lock file and never carry secrets, databases, warehouse data or business workbooks |
| `scripts/audit_backup_rehearsal.py` | Proves dump and restore and times them; refuses a production-named database |
