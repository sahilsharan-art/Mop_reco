# V2 architecture

- Keep existing `app/mop_reco` engines unchanged; `app/api_v2.py` adapts their callable interfaces so existing calculation and audit rules remain authoritative.
- Use TanStack Start file routes for every React screen; do not reintroduce Streamlit into the V2 browser path.
- Serve React and `/api/mop` behind one origin; HttpOnly session cookies and origin/CSRF checks protect every mutation.
- Keep semantic colours in `frontend/src/styles.css`; all screens share the executive brand bar, light canvas, controls and tables.
- Read only precomputed aggregates for cash/float/dashboard requests; refuse uncovered selections rather than calling the legacy raw-file fallback.
- Never present an unported operation as working: retain the old app for rollback until workflow parity is verified.
