"""Audit D-006: a page view must never re-read the raw CMS files (and so rewrite warehouse partitions).

Streamlit executes the body of `with st.expander(...)` on EVERY run, open or closed, so a call inside one is an eager call. The Summary
"Data notes" expander and the Float "Credits exceeding deposits" expander each called `cms_frame()` that way: every view re-ingested all
CMS months and rewrote the Parquet files (CLAUDE.md 6.14 says a page reads precomputed rows only).

This test reads the page sources and demands that every call to `cms_frame` / `_cms_breakdown` is either inside a function cached with
`st.cache_data`, or inside an `if` that tests `st.session_state` / a button (an explicit action). It also checks the cache key.
"""
import ast
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

WATCHED = {"cms_frame", "_cms_breakdown", "ingest_axis_cms", "ingest_ptc", "ingest_pos_bill_wise", "ingest_paytm", "ingest_axis_edc", "ingest_bajaj"}
# Functions whose body is allowed to call them: an explicit action (a button handler) or the cached wrapper itself.
ALLOWED_FUNCS = {"_cms_cached", "_cms_breakdown_cached", "_cms_breakdown", "_export_button"}


def _parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node


def _enclosing(node):
    out = []
    while hasattr(node, "_parent"):
        node = node._parent
        out.append(node)
    return out


def _is_cached(fn):
    return any("cache_data" in ast.unparse(d) or "cache_resource" in ast.unparse(d) for d in fn.decorator_list)


def _is_gated(chain):
    """Inside an `if`/`elif` whose test reads session_state or is a bare button result - an explicit user action."""
    for n in chain:
        if isinstance(n, ast.If):
            t = ast.unparse(n.test)
            if "session_state" in t or "button" in t or t in {"build", "clicked"} or t.startswith("st.button"):
                return True
    return False


def _calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    _parents(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name in WATCHED:
                yield node, name, _enclosing(node)


PAGES = sorted([APP / "Summary.py", *APP.glob("pages/*.py"), *(p for p in APP.glob("*.py") if p.name not in {"Summary.py"})])


@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.name)
def test_no_page_reads_raw_sources_on_every_load(path):
    bad = []
    for node, name, chain in _calls(path):
        funcs = [n for n in chain if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if any(f.name in ALLOWED_FUNCS or _is_cached(f) for f in funcs):
            continue
        if _is_gated(chain):
            continue
        in_expander = any(isinstance(n, ast.With) and "expander" in ast.unparse(n.items[0]) for n in chain)
        bad.append(f"{path.name}:{node.lineno} calls {name}() {'inside an expander body' if in_expander else 'on every run'}")
    assert not bad, "raw sources must not be read on every page load: " + "; ".join(bad)


def test_the_cache_key_changes_when_a_cms_file_changes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mop_reco import store_sheet_source as src
    from mop_reco import config as cfgmod

    f = tmp_path / "cms.csv"
    f.write_text("a")
    monkeypatch.setattr(cfgmod, "load_config", lambda: SimpleNamespace(source_status=lambda s: SimpleNamespace(files=[f])))
    first = src.cms_source_fingerprint()
    assert first == src.cms_source_fingerprint(), "unchanged files give the same key, so the cached answer is reused"
    f.write_text("a longer file")
    assert src.cms_source_fingerprint() != first, "a changed file gives a new key, so it is re-read"
    f.unlink()
    assert src.cms_source_fingerprint()[0][1] == "missing", "a vanished file is reported in the key, not raised"


def test_the_watcher_would_catch_the_original_defect(tmp_path):
    """The test above is only worth having if it fails on the code that was wrong. This is that code, in miniature."""
    bad = tmp_path / "page.py"
    bad.write_text("import streamlit as st\nwith st.expander('Data notes'):\n    cms = src.cms_frame(session)\n", encoding="utf-8")
    found = [(name, [type(n).__name__ for n in chain]) for _, name, chain in _calls(bad)]
    assert found and found[0][0] == "cms_frame" and "With" in found[0][1]
    good = tmp_path / "page2.py"
    good.write_text("import streamlit as st\nif st.session_state.get('x'):\n    cms = src.cms_frame(session)\n", encoding="utf-8")
    assert all(_is_gated(chain) for _, _, chain in _calls(good))
