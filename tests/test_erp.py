"""The read-only ERP connection (mop_reco/erp.py). No real ERP is touched: psycopg.connect is replaced by a fake."""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import erp

PASSWORD = "p@ss-VERY-secret-0123456789abcdef"
ENV = {"MOP_ERP_HOST": "10.0.0.82", "MOP_ERP_PORT": "5432", "MOP_ERP_USER": "ckpost", "MOP_ERP_PASSWORD": PASSWORD,
       "MOP_ERP_DBNAME": "erpdb"}


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def fetchmany(self, n):
        return list(self.rows[:n])


class _Conn:
    def __init__(self, read_only_answer="on"):
        self.answer, self.read_only, self.closed, self.executed, self.rolled_back = read_only_answer, None, False, [], 0

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if "transaction_read_only" in sql:
            return _Cur([(self.answer,)])
        return _Cur([(1,), (2,), (3,)])

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_connect(monkeypatch):
    calls = []

    def _connect(**kwargs):
        calls.append(kwargs)
        return _Conn()
    monkeypatch.setattr(psycopg, "connect", _connect)
    return calls


# --- configuration -----------------------------------------------------------------------------------------------

def test_missing_settings_are_named_and_no_value_is_leaked():
    env = dict(ENV)
    del env["MOP_ERP_PASSWORD"]
    del env["MOP_ERP_HOST"]
    with pytest.raises(erp.ErpConfigError) as exc:
        erp.settings(env)
    msg = str(exc.value)
    assert "MOP_ERP_PASSWORD" in msg and "MOP_ERP_HOST" in msg and "ckpost" not in msg


def test_describe_never_contains_the_password():
    text = erp.describe(ENV)
    assert PASSWORD not in text and "ckpost@10.0.0.82:5432/erpdb" in text and "read-only" in text


def test_a_database_name_is_required_to_connect(fake_connect):
    env = {k: v for k, v in ENV.items() if k != "MOP_ERP_DBNAME"}
    with pytest.raises(erp.ErpConfigError, match="database name"):
        erp.connect(env=env)
    assert fake_connect == []


# --- the connection ----------------------------------------------------------------------------------------------

def test_connect_is_read_only_and_tagged(fake_connect):
    conn = erp.connect(env=ENV, statement_timeout_s=60)
    kw = fake_connect[0]
    assert kw["application_name"] == "mop_reco" and kw["user"] == "ckpost" and kw["host"] == "10.0.0.82"
    assert "default_transaction_read_only=on" in kw["options"] and "statement_timeout=60000" in kw["options"]
    assert conn.read_only is True and conn.closed is False


def test_a_session_that_is_not_read_only_is_refused_and_closed(monkeypatch):
    made = []

    def _connect(**kwargs):
        made.append(_Conn(read_only_answer="off"))
        return made[0]
    monkeypatch.setattr(psycopg, "connect", _connect)
    with pytest.raises(erp.ErpReadOnlyError, match="read-only"):
        erp.connect(env=ENV)
    assert made[0].closed is True


def test_poolers_that_refuse_startup_options_get_the_same_guards_by_sql(monkeypatch):
    seen = []

    def _connect(**kwargs):
        seen.append(kwargs)
        if "options" in kwargs:
            raise psycopg.OperationalError('unsupported startup parameter in options: "default_transaction_read_only"')
        return _Conn()
    monkeypatch.setattr(psycopg, "connect", _connect)
    conn = erp.connect(env=ENV, statement_timeout_s=30)
    assert len(seen) == 2 and "options" not in seen[1]
    assert any("statement_timeout = 30000" in s for s in conn.executed)
    assert conn.read_only is True


def test_other_connection_errors_are_not_swallowed(monkeypatch):
    def _connect(**kwargs):
        raise psycopg.OperationalError("password authentication failed for user")
    monkeypatch.setattr(psycopg, "connect", _connect)
    with pytest.raises(psycopg.OperationalError, match="authentication"):
        erp.connect(env=ENV)


# --- the statement guard -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "select 1", "SELECT a, comment, set_ts, updated_at FROM t WHERE b = 1", "with x as (select 1) select * from x",
    "show transaction_read_only", "explain select * from t", "select 'delete me from t' as s -- drop table x",
    "select 1;", "/* insert */ select 2",
])
def test_plain_reads_pass(sql):
    assert erp.check_select_only(sql) == sql


@pytest.mark.parametrize("sql", [
    "update t set a = 1", "insert into t values (1)", "delete from t", "drop table t", "create table x (a int)",
    "truncate t", "grant all on t to x", "copy t to stdout", "call p()", "select 1; drop table t",
    "with a as (delete from t returning *) select * from a", "select * into x from t", "explain analyze select 1",
    "", "   ", "set role postgres", "begin", "vacuum t",
])
def test_anything_else_is_refused(sql):
    with pytest.raises(erp.ErpReadOnlyError):
        erp.check_select_only(sql)


def test_run_select_returns_rows_ends_the_transaction_and_caps_rows():
    conn = _Conn()
    assert erp.run_select(conn, "select 1") == [(1,), (2,), (3,)]
    assert erp.run_select(conn, "select 1", max_rows=2) == [(1,), (2,)]
    assert conn.rolled_back == 2
    with pytest.raises(erp.ErpReadOnlyError):
        erp.run_select(conn, "delete from t")
    assert all("delete" not in s for s in conn.executed)       # the refused statement never reached the server
