"""Discovery probe for the ERP database (read-only).   Run on the production server through deploy\\run_in_app_env.ps1:

    deploy\\run_in_app_env.ps1 scripts\\erp_probe.py --list-databases
    deploy\\run_in_app_env.ps1 scripts\\erp_probe.py --dbname <erp database>
    deploy\\run_in_app_env.ps1 scripts\\erp_probe.py --dbname <erp database> --keywords bill,mop,tender,tpedc,ptc,voucher

What it reports (CLAUDE.md 6.41; docs/ERP_INTEGRATION_BRIEF.md section 4):
  1. who it connected as, the server version, time zone, whether it is a replica, whether TLS is on;
  2. a PRIVILEGE AUDIT of the login: role attributes, and whether it holds any write right on any table or any CREATE right
     on a schema (it proves read-only-ness from the catalogue; it never tries to write);
  3. the databases and schemas it can see, with table counts and estimated rows;
  4. candidate tables whose NAME or COLUMN names contain the keywords (bills, MOPs, tender, terminal response, petty cash, vouchers);
  5. for the best candidates: columns, types, indexes, partitioning.

It reads ONLY the system catalogue. It never selects rows from a business table, so nothing sensitive (card numbers, customer
names) can appear in its output, and it never runs count(*) on a big table (estimates come from the catalogue). The report is
written to logs\\erp_probe_<timestamp>.txt as well as printed. All queries go through mop_reco.erp, which is read-only.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import erp  # noqa: E402

DEFAULT_KEYWORDS = ["bill", "mop", "tender", "tpedc", "edc", "pos", "ptc", "petty", "voucher", "cms", "deposit", "paytm", "axis", "bajaj"]
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--dbname", help="the ERP database (default: MOP_ERP_DBNAME)")
ap.add_argument("--list-databases", action="store_true", help="only list the databases this login can see, then stop")
ap.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS), help="comma-separated words to look for in table and column names")
ap.add_argument("--top", type=int, default=15, help="how many candidate tables to describe in detail")
args = ap.parse_args()

lines = []


def out(text=""):
    print(text)
    lines.append(text)


def table(rows, headers):
    if not rows:
        out("  (none)")
        return
    shown = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in shown)) for i, h in enumerate(headers)]
    out("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    out("  " + "  ".join("-" * w for w in widths))
    for r in shown:
        out("  " + "  ".join(v.ljust(w) for v, w in zip(r, widths)))


out(f"ERP probe {datetime.now():%d/%m/%Y %H:%M:%S}  -  " + erp.describe().replace("(database not set)", args.dbname or "(database not set)"))

# --- databases ---------------------------------------------------------------------------------------------------
if args.list_databases or not (args.dbname or erp.settings().get("dbname")):
    conn = erp.connect(dbname="postgres")
    out("\n== Databases this login can connect to")
    table(erp.run_select(conn, "select datname, pg_size_pretty(pg_database_size(datname)) from pg_database "
                               "where datallowconn and not datistemplate order by datname"), ["database", "size"])
    conn.close()
    if not args.dbname:
        out("\nPick the ERP database from the list and run again with --dbname <name>.")
        sys.exit(0)

conn = erp.connect(dbname=args.dbname)
q = lambda sql, params=None, n=None: erp.run_select(conn, sql, params, n)  # noqa: E731

# --- 1. identity -------------------------------------------------------------------------------------------------
out("\n== 1. Connection")
ident = q("select current_database(), current_user, session_user, version(), current_setting('TimeZone'), "
          "current_setting('server_encoding'), pg_is_in_recovery(), current_setting('transaction_read_only')")[0]
out(f"  database={ident[0]}  user={ident[1]}  session_user={ident[2]}")
out(f"  server: {ident[3]}")
out(f"  time zone={ident[4]}  encoding={ident[5]}  replica (standby)={ident[6]}  this session read-only={ident[7]}")
ssl = q("select ssl, version, cipher from pg_stat_ssl where pid = pg_backend_pid()")
out(f"  TLS: {'ON ' + str(ssl[0][1]) if ssl and ssl[0][0] else 'OFF (the connection is not encrypted)'}")

# --- 2. privilege audit ------------------------------------------------------------------------------------------
out("\n== 2. What this login is allowed to do (privilege audit - read from the catalogue, nothing is written)")
role = q("select rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolconnlimit "
         "from pg_roles where rolname = current_user")[0]
out(f"  role {role[0]}: superuser={role[1]}  create-role={role[2]}  create-db={role[3]}  replication={role[4]}  "
    f"bypass-RLS={role[5]}  connection-limit={role[6]}")
member_of = [r[0] for r in q("select b.rolname from pg_auth_members m join pg_roles b on b.oid = m.roleid "
                             "join pg_roles u on u.oid = m.member where u.rolname = current_user")]
out(f"  member of roles: {', '.join(member_of) or '(none)'}")
cfg = q("select coalesce(array_to_string(setconfig, '; '), '') from pg_db_role_setting s join pg_roles r on r.oid = s.setrole "
        "where r.rolname = current_user")
out(f"  role-level settings: {'; '.join(c[0] for c in cfg) or '(none)'}")
writers = q("select n.nspname, c.relname, "
            "has_table_privilege(c.oid, 'INSERT') as i, has_table_privilege(c.oid, 'UPDATE') as u, "
            "has_table_privilege(c.oid, 'DELETE') as d, has_table_privilege(c.oid, 'TRUNCATE') as t "
            "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where c.relkind in ('r','p') and n.nspname not in ('pg_catalog','information_schema','pg_toast') "
            "and (has_table_privilege(c.oid, 'INSERT') or has_table_privilege(c.oid, 'UPDATE') "
            "or has_table_privilege(c.oid, 'DELETE') or has_table_privilege(c.oid, 'TRUNCATE')) "
            "order by 1, 2", n=25)
creates = q("select nspname from pg_namespace where nspname not in ('pg_catalog','information_schema','pg_toast') "
            "and has_schema_privilege(oid, 'CREATE') order by 1")
readable = q("select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.relkind in ('r','p','v','m') "
             "and n.nspname not in ('pg_catalog','information_schema','pg_toast') and has_table_privilege(c.oid, 'SELECT')")[0][0]
out(f"  tables/views it can READ: {readable}")
if writers or creates or role[1]:
    out("  >>> WARNING: this login is NOT purely read-only at the database level:")
    if role[1]:
        out("      - it is a SUPERUSER")
    if writers:
        out(f"      - it holds a write right on {len(writers)}{'+' if len(writers) == 25 else ''} table(s), first ones:")
        table([(w[0], w[1], "I" if w[2] else "", "U" if w[3] else "", "D" if w[4] else "", "T" if w[5] else "") for w in writers[:10]],
              ["schema", "table", "insert", "update", "delete", "truncate"])
    if creates:
        out(f"      - it may CREATE objects in schema(s): {', '.join(c[0] for c in creates)}")
    out("      MOP still forces every ERP session read-only, so MOP itself cannot write - but tell the DBA.")
else:
    out("  RESULT: no write right on any table, no CREATE right on any schema, not a superuser - read-only at the database level.")

# --- 3. schemas --------------------------------------------------------------------------------------------------
out("\n== 3. Schemas this login can read (estimated rows come from the catalogue, no count(*) is run)")
table(q("select n.nspname, count(*) filter (where c.relkind in ('r','p')) as tables, count(*) filter (where c.relkind in ('v','m')) as views, "
        "coalesce(sum(c.reltuples) filter (where c.relkind in ('r','p') and c.reltuples > 0), 0)::bigint as est_rows "
        "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where c.relkind in ('r','p','v','m') and n.nspname not in ('pg_catalog','information_schema','pg_toast') "
        "and has_table_privilege(c.oid, 'SELECT') group by 1 order by 4 desc"), ["schema", "tables", "views", "est. rows"])

# --- 4. candidates -----------------------------------------------------------------------------------------------
words = [w.strip().lower() for w in args.keywords.split(",") if w.strip()]
out(f"\n== 4. Candidate tables/views: name or column name contains any of: {', '.join(words)}")
like = " or ".join(["lower(c.relname) like %s"] * len(words))
col_like = " or ".join(["lower(a.attname) like %s"] * len(words))
pats = [f"%{w}%" for w in words]
cands = q(f"""
    with hits as (
      select c.oid, n.nspname, c.relname, c.relkind, c.reltuples, c.relpages,
             ({like}) as name_hit
      from pg_class c join pg_namespace n on n.oid = c.relnamespace
      where c.relkind in ('r','p','v','m') and n.nspname not in ('pg_catalog','information_schema','pg_toast')
        and has_table_privilege(c.oid, 'SELECT')),
    cols as (
      select a.attrelid as oid, count(*) as col_hits, string_agg(a.attname, ', ' order by a.attnum) as hit_cols
      from pg_attribute a where a.attnum > 0 and not a.attisdropped and ({col_like}) group by 1)
    select h.nspname, h.relname, h.relkind, greatest(h.reltuples, 0)::bigint, pg_size_pretty(h.relpages::bigint * 8192),
           h.name_hit, coalesce(c.col_hits, 0), left(coalesce(c.hit_cols, ''), 70)
    from hits h left join cols c on c.oid = h.oid
    where h.name_hit or coalesce(c.col_hits, 0) > 0
    order by (h.name_hit::int * 100 + coalesce(c.col_hits, 0)) desc, h.reltuples desc nulls last
    limit 60""", pats + pats)
table([(r[0], r[1], {"r": "table", "p": "partitioned", "v": "view", "m": "matview"}[r[2]], f"{r[3]:,}", r[4],
        "name" if r[5] else "", r[6], r[7]) for r in cands],
      ["schema", "name", "kind", "est. rows", "size", "name hit", "col hits", "matching columns"])

# --- 5. detail for the best candidates ---------------------------------------------------------------------------
out(f"\n== 5. Detail for the top {args.top} candidates (structure only - no data rows are read)")
for r in cands[:args.top]:
    schema, name = r[0], r[1]
    out(f"\n-- {schema}.{name}   ({ {'r': 'table', 'p': 'partitioned table', 'v': 'view', 'm': 'matview'}[r[2]] }, ~{r[3]:,} rows, {r[4]})")
    cols = q("select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull from pg_attribute a "
             "where a.attrelid = %s::regclass and a.attnum > 0 and not a.attisdropped order by a.attnum", [f'"{schema}"."{name}"'])
    out("  columns: " + ", ".join(f"{c[0]} {c[1]}{'' if not c[2] else ' NN'}" for c in cols))
    if r[2] in ("r", "p"):
        idx = q("select indexdef from pg_indexes where schemaname = %s and tablename = %s", [schema, name])
        for i in idx:
            out("  index: " + i[0].replace("CREATE ", "").replace("INDEX ", "")[:170])
        if not idx:
            out("  index: (none)")
        parts = q("select count(*) from pg_inherits where inhparent = %s::regclass", [f'"{schema}"."{name}"'])[0][0]
        if parts:
            out(f"  partitions/children: {parts}")
    else:
        out("  (view: its definition is not read here - ask the DBA for it)")

out("\n== Done. Structure only; no business rows were read. Hand this report to the session or DBA working on the ERP brief.")
conn.close()

logs = ROOT / "logs"
logs.mkdir(exist_ok=True)
target = logs / f"erp_probe_{datetime.now():%Y%m%d_%H%M%S}.txt"
target.write_text("\n".join(lines), encoding="utf-8")
print(f"\nreport saved: {target}")
