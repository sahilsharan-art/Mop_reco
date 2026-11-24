"""Backup and restore rehearsal (audit Phase 8): dump a database, restore it into a new one, time both, and compare the result table by table.

    python scripts/audit_backup_rehearsal.py <source_db> <restore_db> [--bin D:/pgdev/pgsql/bin] [--host 127.0.0.1] [--port 55432] [--user postgres]

Meant for the development PostgreSQL. It refuses a restore target that already exists and a source or target name that looks like production.
It proves that the method works and measures how long it takes; the production database is not touched by this script.
"""
import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("source")
ap.add_argument("target")
ap.add_argument("--bin", default="D:/pgdev/pgsql/bin")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", default="55432")
ap.add_argument("--user", default="postgres")
ap.add_argument("--out", default="D:/pgdev/backup")
a = ap.parse_args()
if "MOPfinanceapp".lower() in (a.source + a.target).lower():
    sys.exit("Refusing: this rehearsal is for the development database only.")
BIN = Path(a.bin)
conn = ["-h", a.host, "-p", a.port, "-U", a.user]
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
dump = out / f"{a.source}.dump"


def run(cmd, label):
    t = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.perf_counter() - t
    print(f"{label}: {dt:6.1f} s  (exit {r.returncode})")
    if r.returncode != 0:
        print(r.stderr[-800:])
        sys.exit(1)
    return dt


def sql(db, q):
    r = subprocess.run([str(BIN / "psql.exe"), *conn, "-d", db, "-At", "-c", q], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(r.stderr[-500:])
    return r.stdout.strip()


existing = sql("postgres", f"select count(*) from pg_database where datname = '{a.target}'")
if existing != "0":
    sys.exit(f"Refusing: database {a.target} already exists.")

t_dump = run([str(BIN / "pg_dump.exe"), *conn, "-Fc", "-Z", "6", "-f", str(dump), a.source], "pg_dump (custom format, compressed)")
print(f"dump file: {dump.stat().st_size / 1024 ** 3:.2f} GB; sha256 {hashlib.sha256(dump.read_bytes()[:50_000_000]).hexdigest()[:16]}... (first 50 MB)")
run([str(BIN / "psql.exe"), *conn, "-d", "postgres", "-c", f"CREATE DATABASE {a.target}"], "create empty target")
t_restore = run([str(BIN / "pg_restore.exe"), *conn, "-d", a.target, "-j", "4", "--no-owner", str(dump)], "pg_restore (4 parallel jobs)")

tables = sql(a.source, "select tablename from pg_tables where schemaname = 'public' order by 1").splitlines()
bad = []
print(f"\n{'table':28s} {'source':>12s} {'restored':>12s}")
for t in tables:
    s_n, r_n = sql(a.source, f"select count(*) from {t}"), sql(a.target, f"select count(*) from {t}")
    flag = "" if s_n == r_n else "   <-- DIFFERENT"
    if s_n != r_n:
        bad.append(t)
    if int(s_n) > 0 or flag:
        print(f"{t:28s} {s_n:>12s} {r_n:>12s}{flag}")
# content check on the tables that hold people's work: an md5 over every row
for t, key in (("manual_matches", "id"), ("exceptions", "id"), ("remarks", "id"), ("store_tasks", "id"), ("app_users", "id"), ("master_rows", "id"), ("store_day_agg", "id")):
    q = f"select coalesce(md5(string_agg(x::text, '|' order by {key})), 'empty') from {t} x"
    s_h, r_h = sql(a.source, q), sql(a.target, q)
    print(f"content md5 {t:20s} {'identical' if s_h == r_h else 'DIFFERENT'}")
    if s_h != r_h:
        bad.append(t + " (content)")
print(f"\nSUMMARY: dump {t_dump:.0f} s, restore {t_restore:.0f} s, total {t_dump + t_restore:.0f} s; {'ALL TABLES AND CHECKED CONTENT IDENTICAL' if not bad else 'DIFFERENCES: ' + str(bad)}")
sys.exit(1 if bad else 0)
