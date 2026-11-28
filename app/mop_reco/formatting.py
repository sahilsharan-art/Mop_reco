"""Number and money formatting. Pure functions — no Streamlit import.

Indian conventions throughout:
  - headlines/tiles: short form, Rs 54.14 Cr / Rs 2.29 L / Rs 61,996
  - tables: full number, Indian comma grouping (lakh/crore), right-aligned
  - never a bare integer for money
  - counts stay counts (plain grouping, no Rs)
"""

from __future__ import annotations

import datetime as _dt
from contextvars import ContextVar

import pandas as pd

CRORE = 10_000_000
LAKH = 100_000

DATE_DISPLAY_FORMAT = "%d/%m/%Y"

# How money is DISPLAYED (2026-11-28): "auto" keeps every page as it always was (the Summary matrix in crores, tables in full rupees,
# tiles in the best short unit); "crore" shows every displayed figure in crores; "full" shows every displayed figure as full rupees.
# One ContextVar, set at the start of each page run from the sidebar's choice; the dashboard serves each session in its own thread, so
# one person's choice never reaches another. Only DISPLAY forms follow it (money_cr, money_short, money_cell, money_note): exact
# figures - tooltips, MISMATCH messages, exports, anything stated to the paisa - use money_table / indian_group, which never change.
UNIT_AUTO, UNIT_CRORE, UNIT_FULL = "auto", "crore", "full"
_UNIT = ContextVar("money_unit", default=UNIT_AUTO)


def set_unit(unit: str) -> None:
    _UNIT.set(unit if unit in (UNIT_AUTO, UNIT_CRORE, UNIT_FULL) else UNIT_AUTO)


def get_unit() -> str:
    return _UNIT.get()


def display_date(value) -> str:
    """Every displayed/exported date on every page uses DD/MM/YYYY (2026-09-20,
    slashes since 2026-09-21 on instruction). Dates are stored as a date type
    internally; this is the one shared place that decides how a date is
    shown — never format one ad hoc per page."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        if not value.strip():
            return ""
        try:
            value = pd.to_datetime(value)
        except (ValueError, TypeError):
            return value
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return ""
        value = value.date()
    if isinstance(value, _dt.datetime):
        value = value.date()
    if isinstance(value, _dt.date):
        return value.strftime(DATE_DISPLAY_FORMAT)
    return str(value)


def blank(value) -> str:
    """A null must render as an empty cell, never the literal string 'nan'
    (2026-09-20 fix — the Manual matches detail panel printed a bare 'nan'
    whenever a field was None/NaN). The one shared place that decides this,
    so no page has to remember to check pd.isna() itself."""
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value)
    return "" if text.strip().lower() in ("nan", "none", "nat") else text


def indian_group(value) -> str:
    """1234567.5 -> '12,34,567.50' — last three digits, then pairs."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)

    sign = "-" if num < 0 else ""
    num = abs(num)
    whole = int(num)
    paise = round((num - whole) * 100)
    if paise == 100:  # rounding carried
        whole += 1
        paise = 0

    digits = str(whole)
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        if head:
            pairs.insert(0, head)
        grouped = ",".join(pairs) + "," + tail

    return f"{sign}{grouped}.{paise:02d}"


def money_table(value) -> str:
    """Money in a table cell: full number, Indian grouping, no unit prefix.
    Right-alignment and tabular figures are the view's job."""
    if value is None:
        return "—"
    return indian_group(value)


def money_short(value, unit_prefix: str = "Rs ") -> str:
    """Money in a tile or headline: Indian short form.

    >= 1 crore  -> 'Rs 54.14 Cr'
    >= 1 lakh   -> 'Rs 2.29 L'
    otherwise   -> 'Rs 61,996'  (still grouped, never bare)

    Follows the display unit: "crore" always shows crores, "full" always shows the full rupee figure."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)

    unit = _UNIT.get()
    if unit == UNIT_CRORE:
        return money_cr(num)
    sign = "-" if num < 0 else ""
    magnitude = abs(num)
    if unit == UNIT_FULL:
        return f"{sign}{unit_prefix}{indian_group(magnitude)}"

    if magnitude >= CRORE:
        return f"{sign}{unit_prefix}{magnitude / CRORE:,.2f} Cr"
    if magnitude >= LAKH:
        return f"{sign}{unit_prefix}{magnitude / LAKH:,.2f} L"
    # Below a lakh: whole rupees, grouped. Paise are noise at headline level.
    whole = indian_group(magnitude).split(".")[0]
    return f"{sign}{unit_prefix}{whole}"


def money_cr(value) -> str:
    """Money in crores, ALWAYS — never switches to Lakh for a small value
    (unlike `money_short`, which picks the biggest unit that fits). Used
    where a whole table or band must keep one consistent unit so cells stay
    comparable at a glance (2026-09-21, the Summary hero band and
    particulars matrix): 'Rs 103.10 Cr', 'Rs 0.02 Cr'. Display only — the
    underlying value is never rounded, and a total must always be computed
    from the unrounded values and formatted last, never summed from already-
    rounded Cr strings.

    The display unit can switch it to the full rupee figure ("Full amount"); crores is what it shows otherwise."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if num < 0 else ""
    if _UNIT.get() == UNIT_FULL:
        return f"{sign}Rs {indian_group(abs(num))}"
    return f"{sign}Rs {abs(num) / CRORE:,.2f} Cr"


def money_cell(value) -> str:
    """Money in a TABLE CELL, following the display unit: "auto" and "full" give the full figure with Indian grouping (as `money_table`
    always has); "crore" gives crores to four decimals ('0.0073 Cr' = Rs 73,000), because a store-day is far smaller than a crore and two
    decimals would flatten it to 0.00. Display only; use `money_table` where the exact figure must be stated."""
    if value is None:
        return "—"
    if _UNIT.get() != UNIT_CRORE:
        return indian_group(value)
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{'-' if num < 0 else ''}{abs(num) / CRORE:,.4f} Cr"


def money_note(value) -> str:
    """A visible figure next to a count ("Rs 27,14,365.00"), following the display unit: "auto" and "full" give 'Rs ' + the full
    figure (as before); "crore" gives 'Rs 0.27 Cr'."""
    if _UNIT.get() == UNIT_CRORE:
        return money_cr(value)
    return f"Rs {money_table(value)}"


def count(value) -> str:
    """A count is not money — grouped, no Rs, no decimals."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def percent(numerator, denominator, places: int = 2) -> str:
    if not denominator:
        return "—"
    return f"{numerator / denominator * 100:.{places}f}%"


def date_range_label(date_from, date_to) -> str:
    """'1-31 July' / '20 July' / '28 Jun - 3 Jul' — for the verdict line."""
    if date_from is None or date_to is None:
        return "—"
    if date_from == date_to:
        return f"{date_from.day} {date_from:%B}"
    if (date_from.year, date_from.month) == (date_to.year, date_to.month):
        return f"{date_from.day}-{date_to.day} {date_to:%B}"
    return f"{date_from:%-d %b} - {date_to:%-d %b}".replace("%-d", str(date_from.day))
