"""Prove the unpacked application is exactly the package that was built: every file's checksum against MANIFEST.json.

    python scripts/verify_manifest.py          (run from the application folder on the server)
Exit 0 = identical; 1 = something is missing, altered or unexpected under app/ scripts/ deploy/.
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    mpath = ROOT / "MANIFEST.json"
    if not mpath.is_file():
        print("MANIFEST.json not found - nothing to verify against")
        return 1
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    bad = 0
    for rel, want in manifest.items():
        p = ROOT / rel
        if not p.is_file():
            print("MISSING ", rel)
            bad += 1
        elif hashlib.sha256(p.read_bytes()).hexdigest() != want:
            print("ALTERED ", rel)
            bad += 1
    expected = set(manifest)
    for d in ("app", "scripts"):
        for p in (ROOT / d).rglob("*"):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and rel not in expected:
                print("UNEXPECTED", rel)
                bad += 1
    version = json.loads((ROOT / "VERSION.json").read_text(encoding="utf-8")) if (ROOT / "VERSION.json").is_file() else {}
    print(f"version {version.get('git_commit', '?')} built {version.get('built_at', '?')}: "
          + ("all files identical to the package" if not bad else f"{bad} difference(s)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
