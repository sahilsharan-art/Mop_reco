"""Build the deployment package: one zip with exactly the code the production server needs, a version stamp and a
checksum for every file. NO data, databases, warehouse, exports, logs, secrets or tests go in.

    python scripts/build_package.py            -> dist/mop_reco_<yyyymmdd>_<git>.zip  (+ .sha256)

On the server, scripts/verify_manifest.py proves the unpacked files are exactly the ones that were built.
"""
import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE_DIRS = ["app", "scripts", "deploy", "frontend"]
INCLUDE_FILES = ["requirements.txt", "requirements.lock.txt", "requirements-v2.txt", ".streamlit/config.toml", "CLAUDE.md", "README.md"]
# node_modules and dist are built ON the server from frontend/bun.lock (deploy\install_v2.ps1), never shipped
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".git", "node_modules", "dist"}
EXCLUDE_NAMES = {"production.env", "reco.sqlite"}
EXCLUDE_SUFFIX = {".pyc", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".parquet", ".sec"}
# the one workbook the application itself needs (the Store Task summary template) - not business data
ALLOW_DATA = {"app/assets/task_summary_template.xlsx"}


def git_state() -> tuple:
    def run(*a):
        try:
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""
    commit = run("rev-parse", "--short", "HEAD") or "nogit"
    dirty = bool(run("status", "--porcelain", "--untracked-files=no"))
    return commit, dirty


def wanted(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    if rel in ALLOW_DATA:
        return True
    if set(path.parts) & EXCLUDE_PARTS or path.name in EXCLUDE_NAMES or path.suffix.lower() in EXCLUDE_SUFFIX:
        return False
    if path.suffix.lower() in {".xlsx", ".xlsb", ".xls", ".csv", ".zip"}:      # business data never travels in the package
        return False
    return True


def main() -> int:
    commit, dirty = git_state()
    stamp = datetime.now().strftime("%Y%m%d")
    files = []
    for d in INCLUDE_DIRS:
        files += [p for p in sorted((ROOT / d).rglob("*")) if p.is_file() and wanted(p)]
    files += [ROOT / f for f in INCLUDE_FILES if (ROOT / f).is_file()]
    manifest = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    version = {"built_at": datetime.now().isoformat(timespec="seconds"), "git_commit": commit,
               "uncommitted_changes": dirty, "files": len(manifest)}
    out_dir = ROOT / "dist"
    out_dir.mkdir(exist_ok=True)
    name = f"mop_reco_{stamp}_{commit}" + ("_dirty" if dirty else "")
    zpath = out_dir / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.relative_to(ROOT).as_posix())
        z.writestr("VERSION.json", json.dumps(version, indent=2))
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=2, sort_keys=True))
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    (out_dir / f"{name}.zip.sha256").write_text(f"{digest}  {zpath.name}\n", encoding="utf-8")
    print(f"package: {zpath}  ({zpath.stat().st_size / 1e6:.1f} MB, {len(manifest)} files)")
    print(f"sha256 : {digest}")
    print(f"commit : {commit}" + ("  (WARNING: uncommitted changes - commit first for a release build)" if dirty else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
