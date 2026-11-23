"""Read-only access to the ERP database (PostgreSQL), 2026-11-23.

Pure engine - no Streamlit. Every connection the application makes to the ERP goes through `connect()`, which makes it
read-only THREE ways, so that even if the login (`ckpost`, shared with other applications) were ever given write rights,
nothing in MOP could write:

  1. the session is opened with `default_transaction_read_only=on` (and `conn.read_only = True`);
  2. after connecting it ASKS the server (`SHOW transaction_read_only`) and refuses to continue unless the answer is `on`;
  3. `run_select()` refuses anything that is not a single SELECT / WITH / SHOW / EXPLAIN (no ANALYZE).

It also sets a statement time limit, an idle-in-transaction limit and `application_name = 'mop_reco'`, so the DBA can tell
MOP's queries from every other application that uses the same login.

The password is never logged, never part of an error message, and never part of anything `describe()` returns. It arrives only
as the environment variable MOP_ERP_PASSWORD, which `deploy\\common.ps1` fills from the machine-encrypted `secrets\\erp.sec`.
"""

from __future__ import annotations

import os
import re
from typing import Optional

import psycopg

APPLICATION_NAME = "mop_reco"
DEFAULT_STATEMENT_TIMEOUT_S = 900          # 15 minutes: a month's pull, never an unbounded query
REQUIRED_KEYS = ("MOP_ERP_HOST", "MOP_ERP_PORT", "MOP_ERP_USER", "MOP_ERP_PASSWORD")


class ErpConfigError(RuntimeError):
    """The ERP connection is not configured (names what is missing - never a value)."""


class ErpReadOnlyError(RuntimeError):
    """The session is not read-only, or a statement was not a plain read."""


def settings(env: Optional[dict] = None) -> dict:
    env = os.environ if env is None else env
    missing = [k for k in REQUIRED_KEYS if not (env.get(k) or "").strip()]
    if missing:
        raise ErpConfigError("The ERP connection is not set up: missing " + ", ".join(missing)
                             + ". Store the password with deploy\\set_erp_secret.ps1 and the host/port/user in deploy\\production.env.")
    return {
        "host": env["MOP_ERP_HOST"].strip(), "port": int(env["MOP_ERP_PORT"]), "user": env["MOP_ERP_USER"].strip(),
        "password": env["MOP_ERP_PASSWORD"], "dbname": (env.get("MOP_ERP_DBNAME") or "").strip(),
        "sslmode": (env.get("MOP_ERP_SSLMODE") or "prefer").strip(),
    }


def describe(env: Optional[dict] = None) -> str:
    """A safe one-line description for logs and screens: never the password."""
    s = settings(env)
    return f"{s['user']}@{s['host']}:{s['port']}/{s['dbname'] or '(database not set)'} (read-only, sslmode={s['sslmode']})"


def _guard_options(timeout_s: int) -> str:
    return (f"-c default_transaction_read_only=on -c statement_timeout={int(timeout_s) * 1000} "
            "-c idle_in_transaction_session_timeout=120000 -c lock_timeout=5000")


def connect(dbname: Optional[str] = None, statement_timeout_s: int = DEFAULT_STATEMENT_TIMEOUT_S, env: Optional[dict] = None):
    """A read-only connection to the ERP. Raises ErpConfigError / ErpReadOnlyError, never returns a writable session."""
    s = settings(env)
    name = (dbname or s["dbname"]).strip()
    if not name:
        raise ErpConfigError("The ERP database name is not set (MOP_ERP_DBNAME / ERP_DB_NAME in deploy\\production.env).")
    kwargs = dict(host=s["host"], port=s["port"], dbname=name, user=s["user"], password=s["password"], sslmode=s["sslmode"],
                  connect_timeout=15, application_name=APPLICATION_NAME)
    try:
        conn = psycopg.connect(options=_guard_options(statement_timeout_s), **kwargs)
        used_options = True
    except psycopg.OperationalError as exc:
        # Some connection poolers refuse startup options. Fall back to setting the same guards with plain SQL after connecting.
        if "option" not in str(exc).lower():
            raise
        conn = psycopg.connect(**kwargs)
        used_options = False
    conn.read_only = True
    if not used_options:
        conn.execute(f"SET statement_timeout = {int(statement_timeout_s) * 1000}")
        conn.execute("SET idle_in_transaction_session_timeout = 120000")
        conn.execute("SET lock_timeout = 5000")
    answer = (conn.execute("SHOW transaction_read_only").fetchone() or [""])[0]
    if str(answer).lower() != "on":
        conn.close()
        raise ErpReadOnlyError("The ERP session did not come up read-only; refusing to use it.")
    return conn


# --- the statement guard ------------------------------------------------------------------------------------------

# Words that could change data or create objects when they appear inside an otherwise-plain statement (a data-modifying WITH,
# SELECT ... INTO). The FIRST word is checked separately (only select / with / show / explain pass), and the server session is
# read-only anyway - this is the third, cheapest layer, so it only lists words that matter.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|copy|call|execute|vacuum|reindex|cluster|refresh|into)\b",
    re.IGNORECASE)


def _strip_literals_and_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    return sql


def check_select_only(sql: str) -> str:
    """Return the statement if it is one plain read; raise ErpReadOnlyError otherwise."""
    bare = _strip_literals_and_comments(sql).strip().rstrip(";").strip()
    if not bare:
        raise ErpReadOnlyError("Empty statement.")
    if ";" in bare:
        raise ErpReadOnlyError("Only one statement at a time is allowed.")
    first = bare.split(None, 1)[0].lower()
    if first not in ("select", "with", "show", "explain"):
        raise ErpReadOnlyError(f"Only reads are allowed on the ERP (statement starts with '{first}').")
    if first == "explain" and re.search(r"\banalyze\b", bare, re.I):
        raise ErpReadOnlyError("EXPLAIN ANALYZE runs the statement; only plain EXPLAIN is allowed.")
    if first != "explain" and _FORBIDDEN.search(bare):
        # words like UPDATE inside a SELECT (e.g. a column called update_ts is fine; the bare word is not)
        raise ErpReadOnlyError("The statement contains a word that is not allowed on the ERP: "
                               + _FORBIDDEN.search(bare).group(1).upper())
    return sql


def run_select(conn, sql: str, params=None, max_rows: Optional[int] = None) -> list:
    """Run one read and return its rows (a list of tuples). `max_rows` stops a runaway query reading everything."""
    check_select_only(sql)
    cur = conn.execute(sql, params)
    rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
    conn.rollback()
    return rows
