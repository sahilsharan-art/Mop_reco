"""Audit Phase 8: what the release package contains, and that its install list is the proven one.

The package is what goes to the server. It must carry the lock file (the exact packages proven there), must never carry secrets, databases, warehouse
data or business workbooks, and every direct pin in requirements.txt must agree with the lock."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_package as bp  # noqa: E402


def test_the_lock_file_is_part_of_the_package_and_exists():
    assert "requirements.lock.txt" in bp.INCLUDE_FILES and (ROOT / "requirements.lock.txt").is_file()


def test_every_direct_pin_agrees_with_the_lock():
    def pins(p):
        return {l.split("==")[0].strip().lower(): l.split("==")[1].strip() for l in p.read_text(encoding="utf-8").splitlines() if "==" in l and not l.lstrip().startswith("#")}
    direct, lock = pins(ROOT / "requirements.txt"), pins(ROOT / "requirements.lock.txt")
    assert direct and not [(k, v, lock.get(k)) for k, v in direct.items() if lock.get(k) != v]


def test_the_lock_pins_every_package_exactly():
    lines = [l for l in (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    assert len(lines) >= 60 and all("==" in l and not any(op in l for op in (">", "<", "~", "*")) for l in lines)


def test_secrets_databases_and_business_data_never_travel():
    for rel in ["deploy/production.env", "secrets/db.sec", "secrets/erp.sec", "db/reco.sqlite", "warehouse/ptc/2026-07-01.parquet", "data/POS July.xlsb",
                "x/__pycache__/m.cpython-314.pyc", "1. POS Sales/Jul/file.xlsx", "exports/out.csv", "a/b.zip", ".git/config"]:
        assert not bp.wanted(ROOT / rel), rel


def test_application_code_and_the_one_template_do_travel():
    for rel in ["app/Summary.py", "app/mop_reco/export.py", "deploy/install.ps1", "scripts/verify_manifest.py", "app/assets/task_summary_template.xlsx"]:
        assert bp.wanted(ROOT / rel), rel


def test_the_installer_prefers_the_lock_file():
    text = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8")
    assert "requirements.lock.txt" in text and text.index("requirements.lock.txt") < text.index("requirements.txt'")
