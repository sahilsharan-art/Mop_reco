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

# Every module that belongs to this project (2026-10-15). The two hand-kept lists this replaced
# (WATCHED aliases, FROM_MODULES) went blind the moment a page imported a module nobody had
# listed - `exception_pairing as ep` was never checked, and a stale process served a page that
# called `ep.open_population`, which the module it had loaded did not have. Now ANY alias or name
# imported from one of these roots is checked, in pages and in the modules that call each other.
PROJECT_ROOTS = ("mop_reco", "dashboard_common", "inline_match", "run_panel", "match_strip")


def _is_project(dotted: str) -> bool:
    return bool(dotted) and dotted.split(".")[0] in PROJECT_ROOTS


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


def _sources() -> list:
    """Every file whose references are checked: the pages, the app's own modules, and every module of
    the mop_reco package (modules call each other too)."""
    files = _pages()
    files += [p for p in sorted(APP_DIR.glob("*.py")) if p.name not in ("Summary.py", "selfcheck.py")]
    files += sorted((APP_DIR / "mop_reco").glob("*.py"))
    return [p for p in files if p.exists()]


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

    for dotted in sorted(n for n in list(sys.modules) if _is_project(n)):
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


def _absolute(path: Path, node: ast.ImportFrom) -> str:
    """`from . import x` / `from .m import y` resolved against the file's package."""
    if not node.level:
        return node.module or ""
    package = "mop_reco" if (APP_DIR / "mop_reco") in path.parents else ""
    base = package.split(".") if package else []
    base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join(base + ([node.module] if node.module else []))


def _references(path: Path, tree: ast.AST):
    """(module_aliases {alias: dotted}, imported_names [(dotted, name)], rebound names).

    An alias is a module when `from pkg import name` names a SUBMODULE (importable), or `import a.b as c`.
    A rebound name (assigned or a function argument anywhere in the file) is never trusted as an alias,
    so a local variable that happens to be called `q` cannot raise a false alarm."""
    aliases, names, rebound = {}, [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            rebound.add(node.id)
        elif isinstance(node, ast.arg):
            rebound.add(node.arg)
        elif isinstance(node, ast.Import):
            for al in node.names:
                if _is_project(al.name) and al.asname:
                    aliases[al.asname] = al.name
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(path, node)
            if not _is_project(base):
                continue
            for al in node.names:
                if al.name == "*":
                    continue
                sub = f"{base}.{al.name}"
                try:
                    importlib.import_module(sub)
                    aliases[al.asname or al.name] = sub          # a submodule
                except ModuleNotFoundError:
                    names.append((base, al.name))                # a name defined in `base`
                except Exception:  # noqa: BLE001 - a module that fails to import is reported by Python itself
                    pass
    return aliases, names, rebound


def _attribute_loads(tree: ast.AST) -> set:
    """Every `alias.name` reference (call OR plain attribute), as (alias, name)."""
    return {(n.value.id, n.attr) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}


def run_check() -> CheckResult:
    """Every reference to a project module, in every page and module, against the module as LOADED."""
    result = CheckResult()
    result.problems.extend(_stale_modules())
    pages = {p.name for p in _pages()}

    for path in _sources():
        if path.name in pages:
            result.pages_checked += 1
        rel = str(path.relative_to(APP_DIR)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            result.problems.append(Problem("missing_name", rel, rel, "(syntax)", str(path), detail=str(exc)))
            continue
        aliases, names, rebound = _references(path, tree)

        for alias, attr in _attribute_loads(tree):
            dotted = aliases.get(alias)
            if not dotted or alias in rebound:
                continue
            module, mpath = _module_file(dotted)
            if module is None:
                continue
            result.calls_checked += 1
            if not hasattr(module, attr):
                result.problems.append(Problem("missing_attribute", rel, dotted, attr, str(mpath)))

        for dotted, name in names:
            module, mpath = _module_file(dotted)
            if module is None:
                continue
            result.calls_checked += 1
            if not hasattr(module, name):
                result.problems.append(Problem("missing_name", rel, dotted, name, str(mpath)))

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
