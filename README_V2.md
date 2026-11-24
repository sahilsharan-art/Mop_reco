# MOP Reconciliation V2 — React replacement + Python integration

## Important status

V2.3 extends the separate `sahilsharan-art/Mop_reco` React frontend with twelve workflow screens, using the existing Python reconciliation engines. The Festival/Board Planning Lovable application is unchanged. No production history or account data was accessed.

Keep Streamlit available as a rollback until production acceptance is complete. Synthetic verification is not a financial or deployment sign-off.

## Apply this overlay in Mop_reco

Copy `frontend/`, `app/api_v2.py`, `app/api_v2_workflows.py`, `tests/test_api_v2.py`, `tests/test_api_v2_workflows.py`, `requirements-v2.txt` and `deploy/nginx.conf` into the same paths in Mop_reco. Do not replace `app/mop_reco`, existing config, database, data folders or original launchers. Incorporate the short architecture rules from this delivery's `AGENTS_V2.md` into the target repository's guidance.

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

## V2.3 connected additions

- Matching: paginated bill search, single/split/shared validation, explicit discrepancy explanations, unverified assertions, audit history, individual and batch reversal, globally assigned exact/split candidates, spreadsheet preview/apply.
- Refunds: validated full refund or bill-overpayment split, pending/released register, reversals/history, ordinary and Customer Refund workbook uploads.
- CMS: effective deposit chain, controls, computed-versus-overridden values, batch validation, atomic override saves, revert, period remarks/history.
- Masters: history, stale-safe restore, complete six-master snapshot preview/import, original dates/fingerprints, database-only snapshot export after migration.
- Runs: source coverage, Axis parking note, master preflight, written stored-master fallback authorizations, explicit start, progress/failure history, guarded stalled-run recovery.
- Store Task bulk replies, parked Axis register, archived upload withdrawal, voluntary password change, and five-tab specialized store-sheet workbook with original formulas.
- Compact readable tables, semantic exception highlights, sortable headers, numeric heatmaps, horizontal scrolling, executive briefing, collection health/risk/payment summaries and store exposure.

## Acceptance limits

- Production PostgreSQL behavior, actual source files, large-population performance and hosted deployment remain unverified. Validate with a safe production copy before retiring Streamlit.
- Run execution uses one in-process worker plus the original engine's database running-run check. Deploy one API worker; multi-process/multi-host locking has not been validated. Stalled recovery requires confirming that the worker is really dead.
- Master snapshot imports preserve the original engine's progressive commits, not an all-or-nothing import. Failures may leave historical versions saved; inspect and revalidate before retrying.
- Matching spreadsheets preserve partial-success semantics; ordinary refund and CMS override batches use their original atomic behavior. Customer Refund folder uploads apply only eligible rows.
- The specialized workbook is an explicit raw-source audit/export operation. Ordinary dashboard/cash/float reads continue to refuse uncovered aggregate selections. Selected-store exports label whole-run controls; formulas remain authored by the original export engine.
- Some older legacy nested drilldowns are not separately recreated. This update covers the requested missing workflows, not a claim that every legacy display has identical parity.

## Verification completed

- Frontend TypeScript and Vite production compilation passed.
- 193 synthetic/core tests passed; one existing production-database-only test was deliberately excluded because its real data is unavailable. Two upstream deprecation warnings remain.
- Connected signed-in browser checks rendered all twelve screens at desktop, 390px phone and 890px tablet widths with no document overflow or page errors.
- UI readback verified task notes, matching/reversal, whole-population bulk matching, CMS override/revert and remarks, refund/reversal, master edit/restore and snapshot download, and a stubbed synthetic run/history.
- Tests exercise the real adapters against synthetic SQLite. Browser run execution uses a stub, not actual extraction. Actual reconciliation execution and hosted auth/deployment require environment acceptance.

```sh
python -m pytest tests/test_api_v2.py tests/test_api_v2_workflows.py -q
```
