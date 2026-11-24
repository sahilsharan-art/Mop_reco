"""Static inventory of the whole code base (audit Phase 1). Read-only; writes docs/audit/inventory.json and docs/audit/MOP_MODULE_INVENTORY.md.

    python scripts/audit_inventory.py

For every Python file under app/, scripts/ and tests/: lines, functions, classes, first docstring line, which project modules it imports, which files import it,
how many test files mention it, which database tables it touches (by ORM class), and the code-quality signals the audit tracks: broad `except Exception` /
bare `except`, `print(` calls, string literals that look like a hardcoded ISO date, raw-SQL `text(` calls and `float(` conversions. It reports signals, not verdicts:
every one still has to be read and classified by a person (see docs/audit/MOP_AUDIT_TRACKER.md).
"""
import ast
import io
import json
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "audit"
OUT.mkdir(parents=True, exist_ok=True)
ISO_DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")


def py_files():
    for sub in ("app", "app/mop_reco", "app/pages", "scripts", "tests"):
        for p in sorted((ROOT / sub).glob("*.py")):
            yield p


def kind_of(p: Path) -> str:
    rel = p.relative_to(ROOT).as_posix()
    if rel.startswith("tests/"):
        return "test"
    if rel.startswith("scripts/"):
        return "script"
    if rel.startswith("app/pages/"):
        return "page"
    if rel.startswith("app/mop_reco/"):
        return "engine"
    return "app"


models = {}      # ORM class -> table
db_src = (ROOT / "app/mop_reco/db.py").read_text(encoding="utf-8")
for m in re.finditer(r"class (\w+)\(Base\):.*?__tablename__ = \"(\w+)\"", db_src, flags=re.S):
    models[m.group(1)] = m.group(2)

rows, importers, texts = {}, {}, {}
for p in py_files():
    rel = p.relative_to(ROOT).as_posix()
    src = p.read_text(encoding="utf-8")
    texts[rel] = src
    tree = ast.parse(src)
    doc = (ast.get_docstring(tree) or "").strip().splitlines()
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    internal = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mod = (n.module or "")
            if n.level:                                           # relative: from . import x / from .x import y
                if mod:
                    internal.add(mod.split(".")[0])
                else:
                    internal.update(a.name for a in n.names)
            elif mod.startswith("mop_reco"):
                parts = mod.split(".")
                internal.update([parts[1]] if len(parts) > 1 else [a.name for a in n.names])
            else:
                internal.add(mod.split(".")[0])
        elif isinstance(n, ast.Import):
            internal.update(a.name.split(".")[0] for a in n.names)
    broad_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and
                   (n.type is None or (isinstance(n.type, ast.Name) and n.type.id in ("Exception", "BaseException")))]
    broad = len(broad_nodes)

    def _speaks(h):                                            # does the handler raise, log, or show the error?
        for sub in ast.walk(h):
            if isinstance(sub, ast.Raise):
                return True
            if isinstance(sub, ast.Call):
                f = sub.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if name in ("warning", "error", "info", "exception", "debug", "critical", "warn") or                         (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("st", "logger", "log", "logging")):
                    return True
        return False
    silent_nodes = [(h.lineno, ast.get_source_segment(src, h.body[0]) or "") for h in broad_nodes if not _speaks(h)]
    dates = []
    if kind_of(p) in ("engine", "page", "app"):
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.STRING and "\n" not in tok.string and ISO_DATE.search(tok.string):
                    dates.append((tok.start[0], tok.string[:60]))
        except tokenize.TokenError:
            pass
    rows[rel] = {
        "kind": kind_of(p), "lines": src.count("\n") + 1, "functions": len(funcs), "classes": len(classes),
        "doc": doc[0][:140] if doc else "", "imports": sorted(internal), "broad_except": broad, "silent_except": silent_nodes,
        "prints": len(re.findall(r"^\s*print\(", src, flags=re.M)), "raw_sql": len(re.findall(r"\btext\(", src)),
        "floats": len(re.findall(r"\bfloat\(", src)), "hardcoded_dates": dates,
        "tables": sorted({models[c] for c in models if re.search(rf"\b{c}\b", src)}),
    }

_order = {"test": 0, "script": 1, "app": 2, "page": 3, "engine": 4}
stems = {}
for r in sorted(rows, key=lambda x: _order[rows[x]["kind"]]):          # an engine module wins over a script of the same name
    stems[Path(r).stem] = r
for rel, info in rows.items():
    for name in info["imports"]:
        if name in stems:
            importers.setdefault(stems[name], set()).add(rel)
for rel, info in rows.items():
    stem = Path(rel).stem
    tests = [t for t, d in rows.items() if d["kind"] == "test" and re.search(rf"\b{re.escape(stem)}\b", texts[t])] if info["kind"] != "test" else []
    info["imported_by"] = sorted(importers.get(rel, set()))
    info["tests_mentioning"] = len(tests)
    non_test_importers = [x for x in info["imported_by"] if rows[x]["kind"] != "test"]
    info["unused_by_app"] = info["kind"] == "engine" and not non_test_importers

(OUT / "inventory.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")

eng = {k: v for k, v in rows.items() if v["kind"] in ("engine", "app", "page")}
md = ["# MOP Reconciliation - module inventory (generated by scripts/audit_inventory.py)", "",
      f"{len(rows)} Python files: " + ", ".join(f"{sum(1 for v in rows.values() if v['kind'] == k)} {k}" for k in ("engine", "app", "page", "script", "test"))
      + f"; {sum(v['lines'] for v in rows.values()):,} lines in all.", "",
      "| File | Kind | Lines | Funcs | Imported by (non-test) | Tests naming it | Tables touched | Broad except | Hardcoded dates | Note |",
      "|---|---|---|---|---|---|---|---|---|---|"]
for rel, v in eng.items():
    nt = [Path(x).stem for x in v["imported_by"] if rows[x]["kind"] != "test"]
    note = "NOT IMPORTED by any app or script" if v["unused_by_app"] else ""
    md.append(f"| {rel.replace('app/', '')} | {v['kind']} | {v['lines']} | {v['functions']} | {', '.join(nt[:5]) + (' +%d' % (len(nt) - 5) if len(nt) > 5 else '') or '-'} | "
              f"{v['tests_mentioning']} | {', '.join(v['tables'][:4]) + (' +%d' % (len(v['tables']) - 4) if len(v['tables']) > 4 else '') or '-'} | "
              f"{v['broad_except'] or '-'} | {len(v['hardcoded_dates']) or '-'} | {note} |")
md += ["", "## Scripts", "", "| Script | Lines | Purpose (first docstring line) |", "|---|---|---|"]
for rel, v in rows.items():
    if v["kind"] == "script":
        md.append(f"| {rel.replace('scripts/', '')} | {v['lines']} | {v['doc'].replace('|', '/')} |")
md += ["", "## Code-quality signals (counts; each still needs a human read)", ""]
tot = lambda k: sum(v[k] for r, v in rows.items() if v["kind"] in ("engine", "app", "page"))  # noqa: E731
md += [f"- Broad `except Exception` / bare except in app code: **{tot('broad_except')}**, of which the handler neither logs, shows nor re-raises (SILENT): "
       f"**{sum(len(v['silent_except']) for v in rows.values() if v['kind'] in ('engine', 'app', 'page'))}**",
       f"- `print(` calls in app code (the brief says use the logger): **{tot('prints')}**",
       f"- Raw-SQL `text(` call sites in app code: **{tot('raw_sql')}**",
       f"- `float(` conversions in app code: **{tot('floats')}**",
       f"- String literals that look like a hardcoded ISO date in app code: **{sum(len(v['hardcoded_dates']) for r, v in rows.items() if v['kind'] in ('engine', 'app', 'page'))}**", ""]
dd = [(r, d) for r, v in rows.items() if v["kind"] in ("engine", "app", "page") for d in v["hardcoded_dates"]]
if dd:
    md += ["### Hardcoded-date literals (CLAUDE.md rule 8) - to classify", ""] + [f"- `{r}:{ln}` {s}" for r, (ln, s) in dd[:40]] + [""]
unused = [r for r, v in rows.items() if v["unused_by_app"] and not r.endswith("__init__.py")]
sil = [(r, ln, body) for r, v in rows.items() if v["kind"] in ("engine", "app", "page") for ln, body in v["silent_except"]]
md += ["### Silent broad handlers (SILENT = no log, no message, no re-raise) - each to be read and classified", ""] +       [f"- `{r}:{ln}` -> `{body.splitlines()[0][:70] if body else ''}`" for r, ln, body in sil] + [""]
md += ["### Engine modules not imported by any app module, page or script", ""] + ([f"- `{r}`" for r in unused] or ["- none"]) + [""]
md += ["## Page -> modules and tables", "", "| Page | Project modules used | Tables touched directly |", "|---|---|---|"]
for rel, v in rows.items():
    if v["kind"] in ("page", "app") and rel.endswith(("Summary.py", ".py")) and ("pages/" in rel or rel.endswith("Summary.py")):
        mods = [m for m in v["imports"] if m in stems and m != Path(rel).stem]
        md.append(f"| {rel.replace('app/', '')} | {', '.join(mods)} | {', '.join(v['tables']) or '-'} |")
(OUT / "MOP_MODULE_INVENTORY.md").write_text("\n".join(md) + "\n", encoding="utf-8")
print(f"{len(rows)} files; app-code signals: broad except {tot('broad_except')}, prints {tot('prints')}, raw SQL {tot('raw_sql')}, "
      f"floats {tot('floats')}, hardcoded dates {len(dd)}; unused engine modules: {unused}")
