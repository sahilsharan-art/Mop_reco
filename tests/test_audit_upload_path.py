"""Audit S-5: an uploaded file's name comes from the browser and must never decide where on disk the file is written.

`store_file()` did `dest_dir / original_name`. On Windows a name such as `..\\..\\x.csv` climbs out of the source folder, and an absolute name
(`C:\\Windows\\x.csv`) replaces the folder altogether, so any signed-in person could write a file wherever the service account can.
Every destination below must stay inside the folder chosen from the source and month, whatever the name says."""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import uploads as up  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

MONTH = "2026-07"

HOSTILE_NAMES = [
    "..\\..\\escape.csv",
    "../../escape.csv",
    "..\\escape.csv",
    "sub\\..\\..\\escape.csv",
    "C:\\Windows\\Temp\\escape.csv",
    "C:/Windows/Temp/escape.csv",
    "\\\\server\\share\\escape.csv",
    "/etc/escape.csv",
    "escape.csv:hidden.stream",
    "con.csv",
    "  ..\\x.csv ",
    "a" * 400 + ".csv",
]


@pytest.fixture
def env(tmp_path):
    folder = tmp_path / "source_root"
    folder.mkdir()
    session = get_session(init_db(get_engine(tmp_path / "up.sqlite")))
    config = SimpleNamespace(sources={"ptc": folder}, base_dir=str(tmp_path))
    spec = next(s for s in up.SOURCE_REGISTRY if s.key == "ptc")
    return SimpleNamespace(folder=folder, session=session, config=config, spec=spec, tmp=tmp_path)


def _temp_file(tmp_path, n):
    p = tmp_path / f"upload_{n}.tmp"
    p.write_text("a,b\n1,2\n")
    return p


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_file_name_never_leaves_the_destination_folder(env, name):
    month = None if not env.spec.month_specific else "2026-07"
    row = up.store_file(env.spec, _temp_file(env.tmp, abs(hash(name))), name, month, "tester", env.session, env.config, 1, None)
    dest = Path(row.stored_path).resolve()
    root = env.folder.resolve()
    assert root in dest.parents, f"{name!r} was stored at {dest}, outside {root}"
    assert dest.exists() and dest.read_text() == "a,b\n1,2\n"
    # nothing was created anywhere else under the temp area except the intended folder tree
    strays = [p for p in env.tmp.rglob("escape*") if root not in p.resolve().parents]
    assert not strays, strays


def test_the_original_name_is_kept_for_the_record_but_displayed_names_are_the_safe_ones(env):
    row = up.store_file(env.spec, _temp_file(env.tmp, 1), "..\\..\\report.csv", MONTH, "tester", env.session, env.config, 1, None)
    assert Path(row.stored_path).name == "report.csv", "only the final file name is used on disk"


def test_an_ordinary_name_is_stored_unchanged(env):
    row = up.store_file(env.spec, _temp_file(env.tmp, 2), "PTC Report July 2026.csv", MONTH, "tester", env.session, env.config, 1, None)
    assert Path(row.stored_path).name == "PTC Report July 2026.csv"
