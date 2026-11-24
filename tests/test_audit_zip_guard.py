"""Audit S-6: a zip member that declares an enormous uncompressed size is refused before it is read (memory exhaustion).
Zips are read in memory, never extracted to disk, so there is no zip-slip; this bounds the memory instead."""
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import io_  # noqa: E402


def _zip(tmp_path, members):
    p = tmp_path / "t.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return p


def test_an_ordinary_zip_still_reads(tmp_path):
    p = _zip(tmp_path, {"part-0000.csv": "a,b\n1,2\n3,4\n"})
    res = io_._read_zip_csv(p, "utf-8")
    assert res.grid == [["a", "b"], ["1", "2"], ["3", "4"]]


def test_a_member_over_the_limit_is_refused_before_reading(tmp_path, monkeypatch):
    p = _zip(tmp_path, {"big.csv": "a,b\n" + "1,2\n" * 1000})
    monkeypatch.setattr(io_, "MAX_ZIP_MEMBER_BYTES", 100)             # the real limit is 4 GB; this stands in for a crafted member
    with pytest.raises(io_.IOError_, match="above the"):
        io_._read_zip_csv(p, "utf-8")


def test_the_limit_is_four_gigabytes():
    assert io_.MAX_ZIP_MEMBER_BYTES == 4 * 1024 ** 3


def test_the_cms_reader_uses_the_same_guard(tmp_path, monkeypatch):
    from mop_reco import cms_blocks
    p = _zip(tmp_path, {"cms.csv": "x\n" * 1000})
    monkeypatch.setattr(io_, "MAX_ZIP_MEMBER_BYTES", 100)
    with pytest.raises(io_.IOError_):
        cms_blocks._read_file(p)
