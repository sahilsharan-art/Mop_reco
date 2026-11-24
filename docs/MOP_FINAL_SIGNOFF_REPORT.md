# MOP Reconciliation - final sign-off report (24 Nov 2026)

For management. Technical detail is in `MOP_COMPLETE_APPLICATION_AUDIT.md`. Server names, addresses and credentials are deliberately not written in any audit document.

## 1. Classification

# C. Not Production Ready

**With a defined route to "B. Production Ready with Accepted Risks"** (section 6). The reason is not the quality of the calculations, which were checked independently and found sound once the defects were fixed. It is that **none of the fixes is on the server, the application is not running there, there is no database backup, and deployment and rollback have not been tried on the live system.** The audit itself is complete for everything the application implements; two of the four reconciliations in the original design (R2, and R3b as specified) are not built and are disclosed below.

## 2. The twelve acceptance criteria, one by one

| Criterion | Met? | Evidence / reason |
|---|---|---|
| All critical financial calculations independently verified | **Yes, for what is built** | R1, the cash book, CMS Reco, Float, the daily sheet and the Summary matrix were each re-calculated separately and agree. **R2 (settlement to bank) and R3b as specified are not implemented**, so they could not be verified; the application does not show them as done |
| All P0 defects closed | **Yes** | No P0 defect was found |
| All P1 defects closed or accepted by management | **No** | Two P1 defects (a CMS adjustment lost in 5 store-days, Rs 7.2 lakh; a file name that could write outside its folder) are fixed in the code but **not deployed**, so neither is closed. Management has not accepted either as a risk |
| Existing and new regression tests pass | **Yes** | Full suite on the final code: **1,968 passed, 0 failed** (26 minutes). Six package tests added after that run started also pass (1,974 in all). Two earlier full runs were also clean (1,309 and 1,948) |
| All core page workflows tested | **Mostly** | 83 acceptance scenarios, 82 pass, 0 fail, 1 not run (starting a run). A real file picker was not used |
| All financial controls reconcile | **Mostly, with disclosed exceptions** | Every formula identity holds on all 45,580 store-days. Control (d), no negative closing balance, reports 1,643 store-days (Rs -17.11 Cr), mainly the cut-off November POS file; it is shown, not hidden |
| No unexplained differences suppressed | **Yes** | Every known difference is listed with an amount and a state (section 5 of the financial report) |
| Audit trails remain intact | **Yes** | Sign-in trail, match, reversal, remark, upload and refund history are kept and not deletable from the application |
| Security controls verified | **Yes, with open items** | Five security defects fixed and proven; passwords, sessions and access control verified. The server firewall is off and the application runs over plain HTTP (needs IT) |
| Production deployment and rollback validated | **No** | Deployment steps and rollback are written (readiness checklist) and the rollback package is on the server, but nothing has been deployed or rolled back. A restore of a database copy was rehearsed successfully (8 minutes); there is no production backup |
| Outstanding dependencies disclosed | **Yes** | Section 5 |
| UAT evidence documented | **Yes, with a limit** | `MOP_UAT_TEST_CASES.xlsx`. The audit team ran every case; **no finance user has tested the application** |

## 3. What was found and fixed

17 defects, all fixed in the development copy with tests: 2 at P1, 7 at P2, 8 at P3, none at P0. The ones that matter most:

| | Defect | Effect | Status |
|---|---|---|---|
| D-001 P1 | A collection and its later adjustment in two monthly CMS files collided and one was dropped | Cash received wrong for 5 store-days, net Rs 7,22,748 | Fixed, not deployed, not yet in the stored run |
| S-5 P1 | An uploaded file's name decided where it was saved | Any signed-in person could write a file outside the upload folder (proven) | Fixed, not deployed |
| D-011 P2 | After a manual or bulk match the Summary matrix still counted the bill as receivable | Two tables on one page disagreed after every bulk accept | Fixed, not deployed |
| D-006 P2 | Two page sections re-read every CMS file and rewrote the warehouse on each view | Slow pages; seen live in the server log | Fixed, not deployed |
| D-008 P2 | A negative closing balance was shown as a positive figure | Misread cash position | Fixed, not deployed |
| S-3, S-4 P2 | Typed or uploaded text could inject markup, or become an Excel formula | Content injection, spreadsheet risk | Fixed, not deployed |

Defect closure: **0 closed on the live system; 17 fixed locally.** Full register: `MOP_DEFECT_REGISTER.xlsx`.

## 4. What changes for the business once deployed

Cash received for 5 store-days increases by Rs 7,22,748 in total (cash receivable lower by the same); the Summary matrix agrees with the store sheet after matches; a negative closing balance shows its minus sign; pages open faster. No schema change; nothing to migrate; rollback is a file swap.

## 5. Outstanding risk register

| # | Risk | Severity | Owner | What is needed |
|---|---|---|---|---|
| 1 | The fixes above are not on the server, and the server build is stopped | **High** | IT + Finance owner | Approval to deploy; run the checklist |
| 2 | No database backup for the application database (it holds matches, remarks, refunds, tasks, users, audit trail that cannot be rebuilt from files) | **High** | DBA | A scheduled backup and a restore test (the method is proven: 8 minutes) |
| 3 | Passwords and sessions cross the network unencrypted; the server firewall is off | **High** (internal network) | IT | HTTPS through the existing proxy; firewall rules |
| 4 | R2 (settlement to bank) is not built and has no bank data; it must not be reported as complete | **High** for completeness | Finance / bank | A bank statement feed with reference numbers; then build |
| 5 | The November POS file is cut off (209,757 of 1,187,732 bill numbers missing); Rs 13.36 Cr shows as unmapped; closing cash -Rs 1.23 Cr | High | Finance + IT | Switch POS and petty cash to the ERP (a settings change with its own comparison), or a complete file |
| 6 | No finance user has accepted the application | Medium | Finance owner | A user acceptance session using the 83 cases |
| 7 | A single administrator can be locked out by anyone reaching the sign-in page | Medium | IT + Finance | Second administrator; lock-out policy |
| 8 | Opening balances differ from the ERP for 21 stores (Rs 40,460.75, Rs 40,000 one store) | Medium | Finance | Which opening is right |
| 9 | 82 duplicate-reference gateway lines cannot be matched or refunded | Medium | Finance | A resolution for "duplicate line in the gateway file" |
| 10 | Only two roles; anyone signed in can upload, run, edit masters and match | Medium | Finance + IT | Role design |
| 11 | The shared ERP login can read more than MOP needs | Medium | DBA | A narrower login |
| 12 | Daily sheet control (g) hard-codes 20 July 2026 | Low | Finance | Approve moving it to configuration or retiring it |
| 13 | No alert when the application stops; it was found stopped with no one told | Medium | IT | A scheduled check that messages an owner |
| 14 | No holiday list; no CMS T+0/T+1 per store; one foreign-currency payment line | Low to medium | Finance | Data and rulings |
| 15 | The web framework's image library has known vulnerabilities (MOP never opens an uploaded image) | Low | IT | Upgrade when the framework allows |

## 6. Route to "B. Production Ready with Accepted Risks"

1. Finance owner approves deploying the audited build, in writing.
2. A database backup is taken before the change (and scheduled afterwards).
3. Deploy by the runbook; verify the file manifest; start the application; open every page.
4. Run 01/04 to 14/11 and compare with run #2: only the 5 cash-received store-days (D-001) and tidier stored amounts (D-002) may differ.
5. A finance user runs the acceptance cases and signs.
6. Management accepts in writing the risks that will remain: items 3 to 5 and 7 to 15 above, each with its owner. Items 1 to 2 and 6 will be closed by steps 1 to 5.

Then every P1 is closed on the live system, the tests pass, and what remains is disclosed and owned: that is classification B. "A" would additionally need R2 built, HTTPS, a role design and a monitored backup.

## 7. Honest limits of this audit

Nothing was run on the production server except read-only checks. Every acceptance case was run by the audit team. R2 and R3b as specified do not exist and were not audited. About 57 operating scripts and 6 small modules were not examined; three large modules (queries, ingest, store tasks) were tested but not read line by line. No screen-reader or keyboard-only testing; load tested to 4 users only.

## 8. Sign-off

| Role | Name | Decision (accept / accept with conditions / reject) | Date |
|---|---|---|---|
| Finance owner | | | |
| IT owner of the application server | | | |
| Database administrator | | | |
| Information security | | | |
| Audit lead | The audit team | Classification C; route to B in section 6 | 24 Nov 2026 |
