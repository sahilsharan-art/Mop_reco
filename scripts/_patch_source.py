from pathlib import Path
p = Path('app/mop_reco/store_sheet_source.py')
s = p.read_text(encoding='utf-8')

old_bundle = '''@dataclass
class SheetBundle:
    sheet: pd.DataFrame = None
    summary: pd.DataFrame = None
    excluded: pd.DataFrame = None
    checks: ss.SheetChecks = None
    cms_type_totals: pd.DataFrame = None
    unresolved_pickups: pd.DataFrame = None
    notes: list = field(default_factory=list)'''

new_bundle = '''@dataclass
class SheetBundle:
    sheet: pd.DataFrame = None
    summary: pd.DataFrame = None            # block 1 — by date, all stores
    summary_by_store: pd.DataFrame = None   # block 2 — by store, whole range
    grand_total: pd.DataFrame = None        # block 3 — one row
    excluded: pd.DataFrame = None           # TYPE = ZPO only, kept visible
    contribution: pd.DataFrame = None       # how J was built, by TYPE and column
    checks: ss.SheetChecks = None
    cms_type_totals: pd.DataFrame = None
    unresolved_pickups: pd.DataFrame = None
    notes: list = field(default_factory=list)'''

assert old_bundle in s, "SheetBundle anchor"
s = s.replace(old_bundle, new_bundle, 1)

old_build_tail = '''    bundle.summary = ss.summarise_by_date(bundle.sheet)
    bundle.excluded = ss.excluded_credit_types(cms)'''
new_build_tail = '''    bundle.summary = ss.summarise_by_date(bundle.sheet)
    bundle.summary_by_store = ss.summarise_by_store(bundle.sheet)
    bundle.grand_total = ss.grand_total_row(bundle.sheet)
    bundle.excluded = ss.excluded_credit_types(cms)
    bundle.contribution = ss.cms_contribution_by_type(cms)'''
assert old_build_tail in s, "build tail anchor"
s = s.replace(old_build_tail, new_build_tail, 1)

p.write_text(s, encoding='utf-8')
print("store_sheet_source.py updated")
