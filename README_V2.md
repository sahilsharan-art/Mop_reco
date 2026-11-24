# MOP Reconciliation V2 — React replacement + Python integration

## Important status

This is a separate source-code delivery for `sahilsharan-art/Mop_reco`, based on main commit `037525d0a3808594e121d0fadfe727e5014e1014`. **It has not been committed to GitHub or deployed.** The GitHub write tool rejected capability provisioning. The existing Festival/Board Planning Lovable app is unchanged.

All eight top-level screens are React/TanStack Start, not Streamlit. The midnight-to-teal executive band, light canvas, semantic metrics, indigo controls, full-width tables, mobile navigation and dialogs share one theme.

**Do not retire the old Streamlit app yet:** this V2 provides connected core workflows, not every nested workflow from the legacy app. The full cutover requires the remaining functionality and production-data acceptance checks listed below. No real database or raw source history was accessed.

## Apply this overlay in Mop_reco

Copy `frontend/`, `app/api_v2.py`, `tests/test_api_v2.py`, `requirements-v2.txt` and `deploy/nginx.conf` into the same paths in Mop_reco. Do not replace `app/mop_reco`, existing config, database, data folders or original launchers. Incorporate the short architecture rules from this delivery's `AGENTS.md` into the target repository's guidance.

### Development

From the Mop_reco root (use its existing Python environment):

```sh
python -m pip install -r requirements-v2.txt
PYTHONPATH=app MOP_V2_ORIGIN=http://localhost:8091 python -m uvicorn api_v2:app --app-dir app --host 127.0.0.1 --port 8092
```

On Windows PowerShell, set `$env:PYTHONPATH="app"` and `$env:MOP_V2_ORIGIN="http://localhost:8091"` before running the same uvicorn command.

In a second terminal, from `frontend/`:

```sh
bun install --frozen-lockfile
bun run dev
```

Open `http://localhost:8091`. Sign in using an **existing Mop_reco account**, never the test fixture values. Auth remains in Mop_reco's existing database and Python password/session functions. No new database schema is required.

### Production

Set `MOP_V2_ORIGIN` to the exact public HTTPS origin, for example your actual finance hostname with no trailing slash. Keep the API private on port 8092. From `frontend/`, run `bun run build` then `PORT=8093 bun serve.ts`. Use the supplied nginx example to route `/api/mop/` to 8092 and other requests to 8093 behind your HTTPS terminator. Secure cookies are enabled when `MOP_V2_ORIGIN` uses HTTPS. Never expose the frontend and API on different browser origins; do not disable CSRF or add broad CORS.

Vite's proxy is development-only. `serve.ts` serves compiled static assets and TanStack SSR in a separate Bun process. It does not run Python in the browser or in a Cloudflare Worker. Existing database environment variables and config are read by the Python adapter.

## Implemented and connected

| Screen | Existing functions reused |
|---|---|
| Sign in / password change / sign out | `auth.authenticate`, `create_session`, `resolve_session`, `change_password`, `end_session` |
| Summary | `mop_particulars_matrix`, `verdict`, `risk_split`, `daily_trend`, `exceptions_by_type`, `store_cash_summary` |
| Store Task | `tasks_page`, `tiles`, `history`, `add_remark`, `mark_under_review`, `close_task`, `preview_replies`, `apply_replies` |
| Cash Position | `load_fast`, `store_cash_summary`, existing export builder |
| Float | `build_float_grid`, `with_remarks`, `set_float_remark`, existing export builder |
| Exceptions | `exception_page`, `add_remark`, `exception_export` |
| Upload | source registry, `validate_upload`, explicit confirm, `store_file`, history |
| Masters | current rows/history, `save_version`, reason required and stale-version guard |
| Users | list/create, role, activate/deactivate, unlock, reset password |

Identity for all writes comes from the verified server-side session, never an operator field submitted by the browser. Users administration is protected at the API and navigation levels. Existing last-admin protection, temporary-password enforcement, lockouts, session expiry, file-name safety and audit functions remain authoritative.

Cash/float/summary selections without precomputed store-day rows return a visible coverage error, rather than triggering legacy raw-file reads. Decimal/NaN/date serialization is normalized. Exports use the existing builder (identifier handling, formula neutralization and metadata). Search labelled "this page" and header sorting apply to visible page records; server task search/status and exception status use backend filters.

## Remaining before a full Streamlit removal

- Manual/bulk matching, reversal, customer refunds, parking, CMS overrides and related drill-downs are not ported in this delivery.
- Bulk reply/remark spreadsheet flows, full master import/restore/source switching, upload deletion and reconciliation-run launch/progress need their V2 controls.
- The original exact store-sheet Excel formulas and all nested audit/control views require acceptance checks; current V2 cash export is the existing generic builder, not the specialized full store-sheet export.
- Master history is returned but not yet a full restore UI. General voluntary password change is not yet in the account menu (forced password change works).
- Cash/float currently return a paginated slice after loading filtered aggregate frames. Production-sized performance and export memory need verification.
- Verify the origin/proxy/TLS configuration, actual PostgreSQL concurrency, permissions and complete financial reconciliation against a COPY of production data.

These limits are why the package is **V2 core replacement**, not a claim of production-parity sign-off. No pretend success buttons or hardcoded live figures are included.

## Verification completed

- Vite production compilation and TypeScript checks passed.
- Built Bun production server returned HTTP 200 for the sign-in page.
- 15 adapter tests passed using a temporary, entirely synthetic SQLite database: auth, origin/CSRF, admin boundary, forced password change, audited task write/readback, required close reason, exact cash values, no raw fallback, logout revocation, last-admin safeguard, store filtering, validate/confirm upload with original-file bytes preserved, and stale master version refusal.
- Playwright signed in through the adapter, rendered all eight screens, saved a task note and read it back through the UI, downloaded an export, signed out, and checked 1440px/390px layouts without page errors or document overflow.
- Production data and hosted deployment were not available and are not verified.

Run tests from Mop_reco root:

```sh
python -m pytest tests/test_api_v2.py -q
```

Test fixtures never target your existing database. Keep original tests and run relevant reconciliation tests before accepting a live cutover.
