# MOP Reconciliation - security audit (24 Nov 2026)

Scope: the application code, its sign-in, uploads and exports, its database and ERP links, its dependencies, and the application server's network exposure. Method: code reading, tests that prove each finding with hostile input, read-only inspection of the server and database. No server was attacked and nothing on the server was changed. Server names, addresses and credentials are not written here.

## 1. Summary

| Severity | Found | Fixed in the code (not yet deployed) | Needs a decision or IT |
|---|---|---|---|
| High (P1) | 1 | 1 | 0 |
| Medium (P2) | 3 | 3 | 0 |
| Low (P3) | 1 | 1 | 0 |
| Infrastructure / decisions | 7 | 0 | 7 |

The one high finding (S-5) let any signed-in person choose where on the server's disk an uploaded file was written. It is fixed and tested, **but the fix is not on the server yet**.

## 2. Findings that were fixed (each proven first)

| ID | Sev | Finding | Proof | Fix | Test |
|---|---|---|---|---|---|
| S-5 | **High** | An uploaded file's name decided where it was saved. `..\..\x.csv` landed outside the source folder; an absolute name replaced the folder (a file really appeared in the system temp folder of the test PC); a network-share name opens a network path, which can leak credentials | The real storing function was called with 12 hostile names | Only a cleaned last path component is used; forbidden characters, reserved device names and over-long names are handled; the final path must lie inside the target folder or the upload is refused | `tests/test_audit_upload_path.py` (14) |
| S-3 | Medium | Text from the database or an uploaded file was put into raw HTML. An `<img>` in a file name appeared for every viewer. (The browser component strips script handlers, so no script ran, but any markup did: tracking images, fake links) | A probe row with a hostile file name was added to a test database and shown in the running page | Escaped at every place that shows file names, uploader, store and tile text; a test scans for any typed field placed in HTML unescaped | `tests/test_audit_html_escaping.py` (6) |
| S-4 | Medium | A typed remark starting with `=` became a live formula in exported workbooks | `=HYPERLINK(...)` and `=1+1` were formula cells in the export | Every workbook writer keeps strings as text and does not make links; CSV cells starting with `=`, `+`, `-`, `@` are prefixed with an apostrophe; a test fails if a new writer forgets | `tests/test_audit_export_formula_injection.py` (8) |
| S-1 | Medium | Response time revealed whether a username exists (an unknown name took twice as long: 337 ms against 171 ms) | Measured | One dummy hash made once, so both paths do one hash; ratio 1.00 | `tests/test_audit_auth_timing.py` (3) |
| S-6 | Low | No limit on a zip member's uncompressed size | Read | Members declaring more than 4 GB are refused before reading | `tests/test_audit_zip_guard.py` (4) |

## 3. Confirmed sound

* **Secrets:** no password, token or connection string with a password in the repository or any of its 39 commits at the time of scanning. Secrets on the server are stored encrypted for that machine only.
* **Passwords:** salted scrypt (cost 2^15), constant-time comparison, minimum length and character rules, a password may not contain the username.
* **Sessions:** 256-bit random tokens, only a hash is stored, they end after 120 idle minutes or 12 hours, on sign-out, on deactivation, on password change and on administrator reset. A signed-out token put back into the browser was refused.
* **Sign-in messages:** a wrong username and a wrong password give the same message. Nothing is shown before sign-in (checked: only the sign-in card).
* **Access control:** the Users page refuses non-administrators; every page checks sign-in before reading data (a test fails if a page does not); the development switch that turns sign-in off is ignored in production.
* **SQL:** the one place that builds SQL text can only produce bound parameters; a hostile-input test returned nothing and changed nothing.
* **Zips** are read in memory and never extracted to disk, so there is no zip-slip.
* **Two operators racing for one transaction:** exactly one wins; the database itself refuses a second active match.
* **The ERP link** is read-only in three layers (session setting, the server's own answer, and a guard that refuses anything but a plain SELECT) and reads one agreed schema.
* **Audit trail:** sign-ins, failures, locks, matches, reversals, remarks, uploads and refunds are recorded and not deletable from the application.

## 4. Open items

| ID | Item | Why it matters | Needs |
|---|---|---|---|
| S-2 | Five wrong passwords lock an account for 15 minutes, and anyone who can reach the sign-in page can do this to the only administrator. The "locked" message also confirms the username exists | Denial of service against the administrator | Policy decision; add a second administrator |
| S-7 | The application server's Windows firewall is off on every profile. Remote administration, file sharing and other applications' ports are open to the network; MOP listens directly on its own port in plain HTTP | Passwords and session cookies cross the network unencrypted; the sign-in is the only protection | IT: firewall rules, HTTPS through the server's existing web proxy |
| S-8 | The image library on the server has many known vulnerabilities (fixed only in a version the web framework does not yet allow). The pip tool has two | MOP never opens an uploaded image through that library, so exposure is low | Upgrade the web framework when a release allows it |
| S-9 | Only 17 of 63 packages were pinned | A rebuild could pull different versions | Done in the code: `requirements.lock.txt` is the server's exact set; the install script should use it |
| S-10 | Exception details (including file paths) are shown to signed-in users | Mild disclosure inside the company | Decide whether to show only the exception type |
| S-11 | Internal server addresses appear in 11 tracked files; the repository's visibility could not be checked from here | Harmless if private | Confirm the repository is private |
| S-12 | The shared ERP login can technically read HR and service-desk tables | MOP is restricted by its own code, not by the database | DBA creates a narrower login |

## 5. What was not tested

No penetration test of the live server or of the other applications on it; no test over HTTPS (there is none); lock-out and session timings were proven by unit tests, not on the server; the cookie is set by the page script, so it cannot be marked HttpOnly (a limit of the web framework).
