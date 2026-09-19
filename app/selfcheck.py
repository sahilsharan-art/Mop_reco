"""Startup self-check — does every function a page calls actually exist?

This exists because of a real failure. Streamlit re-runs the page scripts on
every interaction but keeps imported modules in `sys.modules`, so a process
started before a code change goes on serving the OLD module alongside the NEW
page script. The page calls `q.not_received_by_store(...)`, the cached module
has never heard of it, and the operator gets a raw AttributeError halfway down
the page — while the code on disk is perfectly correct and the test suite
passes. The change looks like it was never made.

So this checks two different things, and the second is the one that caught it:

  1. STATIC   — every `module.function(...)` a page calls exists in the module
                as written on disk. Catches a typo or a rename.
  2. IN-MEMORY — the module object this process actually holds is the one on
                disk, by comparing `__file__` and the module's mtime against
                the process start time. Catches a stale process.

It reports the function, the module and the RESOLVED FILE PATH, because
"AttributeError: no attribute 'not_received_by_store'" does not tell you which
of several possible files was loaded, and that is the only question worth
answering when it happens.
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# Which import aliases in the page sources refer to which module. Only modules
# whose functions a page calls by attribute need listing.
WATCHED = {
    "q": "mop_reco.queries",
    "queries": "mop_reco.queries",
    "mm": "mop_reco.manual_match",
    "manual_match": "mop_reco.manual_match",
    "r3a": "mop_reco.r3a_store_cash",
    "ss": "mop_reco.store_sheet",
    "src": "mop_reco.store_sheet_source",
}

# Modules a page imports names FROM, checked for those names existing.
FROM_MODULES = ("dashboard_common", "inline_match", "mop_reco.export",
                "mop_reco.exceptions", "mop_reco.formatting", "mop_reco.queries",
                "mop_reco.manual_match", "mop_reco.config", "mop_reco.masters",
                "mop_reco.ingest", "mop_reco.runs", "mop_reco.uploads",
                "mop_reco.db", "mop_reco.r3a_store_cash",
                "mop_reco.store_sheet", "mop_reco.store_sheet_source",
                "mop_reco.store_sheet_export")


@dataclass
class Problem:
    kind: str          # "missing_attribute" | "missing_name" | "stale_module"
    page: str
    module: str
    name: str
    resolved_path: str
    detail: str = ""

    def message(self) -> str:
        if self.kind == "stale_module":
            return (f"**{self.module}** was loaded before it was last changed. "
                    f"This process is running an OLD copy in memory.\n\n"
                    f"- resolved file: `{self.resolved_path}`\n"
                    f"- {self.detail}")
        if self.kind == "missing_attribute":
            return (f"**{self.page}** calls `{self.module}.{self.name}(...)`, "
                    f"which does not exist in that module.\n\n"
                    f"- resolved file: `{self.resolved_path}`")
        return (f"**{self.page}** imports `{self.name}` from "
                f"**{self.module}**, which does not define it.\n\n"
                f"- resolved file: `{self.resolved_path}`")


@dataclass
class CheckResult:
    problems: list = field(default_factory=list)
    pages_checked: int = 0
    calls_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


def _pages() -> list:
    pages = [APP_DIR / "Summary.py"]
    pages += sorted((APP_DIR / "pages").glob("*.py"))
    return [p for p in pages if p.exists()]


def _module_file(dotted: str):
    """The file a module resolves to, importing it if needed. None if it will
    not import at all — that is a different failure and raises normally."""
    try:
        module = importlib.import_module(dotted)
    except Exception:  # noqa: BLE001 — an unimportable module is reported below
        return None, None
    return module, getattr(module, "__file__", None)


def _stale_modules() -> list:
    """Modules whose file on disk is NEWER than this process started.

    A file changed after the process began is a file this process cannot be
    running — Python does not reload a module in `sys.modules`.
    """
    problems = []
    boot = getattr(sys, "_mop_reco_started_at", None)
    if boot is None:
        # The entrypoint has not called mark_process_start(), so we cannot tell
        # a stale process from a fresh one. Say nothing rather than guess.
        return problems

    for dotted in sorted(set(WATCHED.values()) | set(FROM_MODULES)):
        module = sys.modules.get(dotted)
        if module is None:
            continue
        path = getattr(module, "__file__", None)
        if not path or not os.path.exists(path):
            continue
        changed = os.path.getmtime(path)
        if changed > boot:
            problems.append(Problem(
                "stale_module", "-", dotted, "-", path,
                detail=(f"the file changed at "
                        f"{time.strftime('%d %b %H:%M:%S', time.localtime(changed))} "
                        f"but this process started at "
                        f"{time.strftime('%d %b %H:%M:%S', time.localtime(boot))}. "
                        "Restart the dashboard.")))
    return problems


def _attribute_calls(tree: ast.AST) -> set:
    """Every `alias.name(...)` call, as (alias, name)."""
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)):
            found.add((node.func.value.id, node.func.attr))
    return found


def _imported_names(tree: ast.AST) -> set:
    """Every `from <module> import name`, as (module, name)."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                found.add((node.module, alias.name))
    return found


def _aliases(tree: ast.AST) -> dict:
    """`import x.y as z` / `from x import y as z` -> {alias: dotted}."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = f"{node.module}.{alias.name}"
    return out


def run_check() -> CheckResult:
    """Every page's calls and imports against the modules as loaded."""
    result = CheckResult()
    result.problems.extend(_stale_modules())

    for page in _pages():
        result.pages_checked += 1
        try:
            tree = ast.parse(page.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            result.problems.append(Problem(
                "missing_name", page.name, page.name, "(syntax)", str(page),
                detail=str(exc)))
            continue

        aliases = _aliases(tree)

        # 1. module.function(...) calls
        for alias, name in _attribute_calls(tree):
            dotted = aliases.get(alias) or WATCHED.get(alias)
            if dotted not in set(WATCHED.values()):
                continue
            module, path = _module_file(dotted)
            if module is None:
                continue
            result.calls_checked += 1
            if not hasattr(module, name):
                result.problems.append(Problem(
                    "missing_attribute", page.name, dotted, name, str(path)))

        # 2. from module import name
        for dotted, name in _imported_names(tree):
            if dotted not in FROM_MODULES:
                continue
            module, path = _module_file(dotted)
            if module is None:
                continue
            result.calls_checked += 1
            if not hasattr(module, name):
                result.problems.append(Problem(
                    "missing_name", page.name, dotted, name, str(path)))

    return result


def mark_process_start():
    """Record when this process began, for the stale-module check. Called once
    from the entrypoint before any page renders."""
    if not hasattr(sys, "_mop_reco_started_at"):
        sys._mop_reco_started_at = time.time()


def render(st) -> bool:
    """Show any problem as a clear message instead of a raw traceback.

    Returns True when everything checks out. `st` is passed in so this module
    stays importable outside Streamlit (the tests import it directly).
    """
    result = run_check()
    if result.ok:
        return True

    st.error("### The app cannot render this page correctly")
    stale = [p for p in result.problems if p.kind == "stale_module"]
    if stale:
        st.markdown(
            "**This dashboard is running code older than the files on disk.** "
            "Streamlit re-runs the page scripts but keeps imported modules in "
            "memory, so a process started before a change goes on serving the "
            "old module — which shows up as a missing function halfway down a "
            "page. **Close this window and start the dashboard again.**"
        )
    for problem in result.problems:
        st.markdown(f"- {problem.message()}")
    st.caption(
        f"Checked {result.pages_checked} page(s) and {result.calls_checked} "
        "function reference(s). This check runs on every page load so the "
        "failure is named rather than thrown."
    )
    return False
