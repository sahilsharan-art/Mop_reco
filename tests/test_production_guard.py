"""Production must never fall back to a local SQLite file when the PostgreSQL address is missing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import db


def test_missing_url_is_refused_when_postgres_is_required(monkeypatch):
    monkeypatch.delenv("MOP_DATABASE_URL", raising=False)
    monkeypatch.setenv("MOP_REQUIRE_POSTGRES", "1")
    with pytest.raises(RuntimeError, match="refusing to start on a local SQLite"):
        db.database_url()


def test_missing_url_is_fine_for_development(monkeypatch):
    monkeypatch.delenv("MOP_DATABASE_URL", raising=False)
    monkeypatch.delenv("MOP_REQUIRE_POSTGRES", raising=False)
    assert db.database_url() is None


def test_the_password_comes_from_the_environment_not_the_url(monkeypatch):
    monkeypatch.setenv("MOP_DATABASE_URL", "postgresql+psycopg://appuser@db.example:5432/appdb")
    monkeypatch.setenv("MOPFINANCE_DB_PASSWORD", "s3cret")
    monkeypatch.setenv("MOP_REQUIRE_POSTGRES", "1")
    url = db.database_url()
    assert "appuser:s3cret@db.example" in url
