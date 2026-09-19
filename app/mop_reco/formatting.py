"""Number and money formatting. Pure functions — no Streamlit import.

Indian conventions throughout:
  - headlines/tiles: short form, Rs 54.14 Cr / Rs 2.29 L / Rs 61,996
  - tables: full number, Indian comma grouping (lakh/crore), right-aligned
  - never a bare integer for money
  - counts stay counts (plain grouping, no Rs)
"""

from __future__ import annotations

import datetime as _dt

import pandas as pd

CRORE = 10_000_000
LAKH = 100_000

DATE_DISPLAY_FORMAT = "%d/%m/%Y"


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
    """
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)

    sign = "-" if num < 0 else ""
    magnitude = abs(num)

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
    rounded Cr strings."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if num < 0 else ""
    return f"{sign}Rs {abs(num) / CRORE:,.2f} Cr"


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
