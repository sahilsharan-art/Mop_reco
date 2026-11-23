"""Compare two fingerprint files made by run_fingerprint.py.   python scripts/compare_fingerprints.py A.json B.json"""
import json
import sys

a, b = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
ok = True
for table in ("r1_bill_lines", "store_day_agg", "mop_day_agg", "digital_mode_agg", "txn_day_agg"):
    ta, tb = a[table], b[table]
    only_a, only_b = sorted(set(ta) - set(tb)), sorted(set(tb) - set(ta))
    diff = [d for d in ta if d in tb and ta[d] != tb[d]]
    rows_a, rows_b = sum(v[0] for v in ta.values()), sum(v[0] for v in tb.values())
    good = not (only_a or only_b or diff)
    ok &= good
    print(f"{table:18s} dates A={len(ta)} B={len(tb)} | rows A={rows_a:,} B={rows_b:,} | dates only in A {len(only_a)}, only in B {len(only_b)}, "
          f"dates whose rows differ {len(diff)}  -> {'IDENTICAL' if good else 'DIFFERENT'}")
    for d in diff[:5]:
        print("    ", d, ta[d], tb[d])
same_summary = a["r1_summary"] == b["r1_summary"]
ok &= same_summary
print("R1 summary          ", "IDENTICAL" if same_summary else f"DIFFERENT\n A={a['r1_summary']}\n B={b['r1_summary']}")
same_exc = a["exceptions_open_by_type"] == b["exceptions_open_by_type"]
ok &= same_exc
print("open exceptions     ", "IDENTICAL" if same_exc else f"DIFFERENT\n A={a['exceptions_open_by_type']}\n B={b['exceptions_open_by_type']}")
print("row counts A:", a["rows"]); print("row counts B:", b["rows"])
print("\nRESULT:", "ALL IDENTICAL" if ok else "DIFFERENCES FOUND")
sys.exit(0 if ok else 1)
