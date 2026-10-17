"""The startup self-check.

This guard exists because four UI changes were reported as working while the
operator's app crashed on the Summary page: the code on disk was correct and
269 tests passed, but the running Streamlit process had been started hours
earlier and was serving cached modules. A green suite proved nothing about the
running app, so these tests check the guard that now does.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import selfcheck  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_process():
    """Pretend this process just started, so the stale-module check is quiet
    unless a test deliberately makes it fire."""
    sys._mop_reco_started_at = time.time()
    yield
    if hasattr(sys, "_mop_reco_started_at"):
        del sys._mop_reco_started_at


def test_the_app_as_it_stands_passes_its_own_check():
    result = selfcheck.run_check()
    assert result.ok, [p.message() for p in result.problems]
    # 5 pages since 2026-09-17 (Summary, Float, Manual matches, Run, Masters)
    # — Bill match, Transactions, Exceptions and Digital sale were removed,
    # folded into Summary as sections.
    assert result.pages_checked >= 5
    assert result.calls_checked > 50, "the check is not actually looking at much"


def test_a_missing_function_is_caught_and_named():
    """The exact reported failure: the page calls a function the loaded module
    does not have.

    `match_overview` is called from `Summary.py` itself (2026-09-17 —
    Summary now imports `queries` directly, for the Matched/Exceptions
    sections folded in from the removed Bill match, Transactions,
    Exceptions and Digital sale pages)."""
    from mop_reco import queries as q

    saved = q.match_overview
    del q.match_overview
    try:
        result = selfcheck.run_check()
        assert not result.ok
        missing = [p for p in result.problems
                   if p.kind == "missing_attribute"
                   and p.name == "match_overview"]
        assert missing, [p.message() for p in result.problems]
        problem = missing[0]
        assert problem.page == "Summary.py"
        assert problem.module == "mop_reco.queries"
        # The resolved path is the whole point — an AttributeError does not say
        # which of several possible files was loaded.
        assert problem.resolved_path.endswith("queries.py")
        assert "resolved file" in problem.message()
    finally:
        q.match_overview = saved


def test_a_missing_imported_name_is_caught():
    import dashboard_common as dc

    saved = dc.sidebar
    del dc.sidebar
    try:
        result = selfcheck.run_check()
        assert not result.ok
        assert any(p.kind == "missing_name" and p.name == "sidebar"
                   for p in result.problems)
    finally:
        dc.sidebar = saved


def test_a_stale_process_is_caught_by_mtime():
    """A module whose file changed AFTER the process started cannot be the
    module this process is running — Python does not reload sys.modules."""
    sys._mop_reco_started_at = time.time() - 86400
    result = selfcheck.run_check()
    stale = [p for p in result.problems if p.kind == "stale_module"]
    assert stale, "a day-old process against today's files must be flagged"
    problem = stale[0]
    assert "OLD copy in memory" in problem.message()
    assert "Restart the dashboard" in problem.message()
    # Both timestamps carry a date, or "changed 21:34 but started 22:13" reads
    # backwards when the process started the previous day.
    assert problem.detail.count(":") >= 4


def test_without_a_recorded_start_the_stale_check_says_nothing():
    """Guessing would be worse than silence."""
    del sys._mop_reco_started_at
    assert selfcheck._stale_modules() == []


def test_mark_process_start_is_idempotent():
    del sys._mop_reco_started_at
    selfcheck.mark_process_start()
    first = sys._mop_reco_started_at
    time.sleep(0.01)
    selfcheck.mark_process_start()
    assert sys._mop_reco_started_at == first


def test_every_page_is_covered():
    """Matched on the page NAME, not its number prefix — inserting a page
    renumbers the others, and a test that breaks on renumbering teaches
    nothing. What matters is that no page is missed."""
    found = {p.name for p in selfcheck._pages()}
    assert "Summary.py" in found

    def bare(name):
        return re.sub(r"^\d+_", "", name).removesuffix(".py").lower()

    names = {bare(n) for n in found}
    # Bill match, Transactions, Exceptions and Digital sale were removed
    # 2026-09-17, folded into Summary as sections — they are gone from disk
    # on purpose, not missing from this list by accident.
    for expected in ("float", "exceptions", "upload", "masters"):
        assert expected in names, f"{expected} is not covered ({sorted(names)})"
    for gone in ("bill_match", "transactions", "digital_sale", "manual_matches", "run"):
        assert gone not in names, f"{gone} was removed (Exceptions tab 2026-10-14 absorbed manual matches, Upload absorbed run)"

    # And every page file on disk really is in the list, so a new page cannot
    # be added without the check seeing it.
    on_disk = {p.name for p in (APP / "pages").glob("*.py")} | {"Summary.py"}
    assert found == on_disk, found ^ on_disk


def test_the_check_runs_on_every_page_load():
    """Wired into page(), which every page calls before rendering anything."""
    source = (APP / "dashboard_common.py").read_text(encoding="utf-8")
    assert "selfcheck.mark_process_start()" in source
    assert "if not selfcheck.render(st):" in source
    assert "st.stop()" in source


def test_a_module_nobody_listed_is_checked_too():
    """The failure of 2026-10-15: the Exceptions page called `ep.open_population`, a function the
    exception_pairing module loaded by that process did not have. The check used to know only a
    hand-kept list of aliases and never looked at `ep`. Now any project module is checked."""
    from mop_reco import exception_pairing as ep

    saved = ep.open_population
    del ep.open_population
    try:
        result = selfcheck.run_check()
        hits = [p for p in result.problems if p.kind == "missing_attribute" and p.name == "open_population"]
        assert hits and hits[0].module == "mop_reco.exception_pairing"
        assert any(p.page == "pages/8_Exceptions.py" for p in hits)
    finally:
        ep.open_population = saved


def test_modules_calling_each_other_are_checked_not_only_pages():
    from mop_reco import exception_pairing as ep

    saved = ep.assign
    del ep.assign
    try:
        result = selfcheck.run_check()
        assert any(p.name == "assign" and p.page == "mop_reco/exception_tables.py"
                   or p.name == "assign" and p.page == "pages/8_Exceptions.py" for p in result.problems)
    finally:
        ep.assign = saved


def test_a_missing_name_imported_from_a_project_module_is_caught():
    import ast
    import tempfile
    src = "from mop_reco.exception_pairing import this_does_not_exist" + chr(10)
    f = Path(tempfile.mkdtemp()) / "x.py"
    f.write_text(src, encoding="utf-8")
    aliases, names, _ = selfcheck._references(f, ast.parse(src))
    assert ("mop_reco.exception_pairing", "this_does_not_exist") in names


def test_every_page_and_module_reference_resolves_today():
    result = selfcheck.run_check()
    assert result.ok, [p.message() for p in result.problems]
    assert result.calls_checked > 300
