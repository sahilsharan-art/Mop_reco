"""The login gate covers the whole application - a page that reads data before it asks who you are would be an open door.

These are source-level checks (the pages need a browser session to run): they fail the moment someone adds a page, or reorders
a page, in a way that lets data be read before `page()` - which is where the sign-in gate runs.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

PAGES = [APP / "Summary.py"] + sorted((APP / "pages").glob("*.py"))


def _first_top_level_call_lines(path: Path):
    """Line of the first top-level `page(...)` call, and the lines of top-level statements that touch data before it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    page_line, risky = None, []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "page":
            page_line = node.lineno
            break
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Expr)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    name = getattr(sub.func, "id", "") or getattr(sub.func, "attr", "")
                    if name in {"db_session", "get_session", "latest_run", "load_config", "read_sql", "read_parquet", "query", "execute"}:
                        if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):     # a definition does not run
                            risky.append((name, node.lineno))
    return page_line, risky


def test_there_are_pages_to_check():
    assert len(PAGES) >= 8


def test_every_page_calls_page_before_touching_data():
    for path in PAGES:
        page_line, risky = _first_top_level_call_lines(path)
        assert page_line is not None, f"{path.name}: no top-level page(...) call - the sign-in gate would never run"
        assert not risky, f"{path.name}: reads data ({risky}) before page() - move it after the sign-in gate"


def test_page_runs_the_gate_before_the_navigation_and_any_content():
    src = (APP / "dashboard_common.py").read_text(encoding="utf-8")
    body = src[src.index("def page("):src.index("LOGO_PATH")]
    assert "auth_gate.require_login(" in body
    assert body.index("auth_gate.require_login(") < body.index("top_nav(")
    assert body.index("selfcheck.render(") < body.index("auth_gate.require_login(")


def test_the_users_page_refuses_non_administrators():
    src = (APP / "pages" / "9_Users.py").read_text(encoding="utf-8")
    assert "auth_gate.is_admin()" in src and "st.stop()" in src
    assert src.index("is_admin()") < src.index("auth.list_users")


def test_the_operator_key_is_shared_by_the_gate_and_the_pages():
    import dashboard_common as dc
    import auth_gate
    assert auth_gate.K_OPERATOR == dc.K_OPERATOR


def test_the_cookie_is_strict_same_site_and_never_holds_a_password():
    src = (APP / "auth_gate.py").read_text(encoding="utf-8")
    assert "SameSite=Strict" in src
    assert not re.search(r"cookie.*password", src, re.I)


def test_the_logo_file_is_a_real_png_the_brand_bar_can_use():
    import dashboard_common as dc
    raw = dc.LOGO_PATH.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(raw) < 60_000, "the logo is inlined into every page's CSS - keep it small"
