"""Period normalisation.

The single most important module in the system. Three publishers in this corpus name the
same twelve months three different ways:

    Economic Survey   "FY25"
    RBI               "2024-25"
    IMF               "FY2024/25"

all of which are 1 April 2024 - 31 March 2025. A system that compares the *labels* rather
than the intervals manufactures contradictions between sources that actually agree, and
misses the real ones. So every period collapses to a concrete (start, end) interval on an
April-March fiscal year, and comparison happens there.

The inverse matters too: Rs.8,142 Cr (FY24) against Rs.2,076 Cr (Q4 FY24) is not a
contradiction, it is containment. ``relation()`` reports that, which is what lets the
reconciler explain an apparent conflict by period instead of flagging it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

FY_START_MONTH = 4  # India: 1 April - 31 March


class Granularity(str, Enum):
    INSTANT = "instant"
    MONTH = "month"
    QUARTER = "quarter"
    HALF = "half"
    NINE_MONTH = "nine_month"
    YEAR = "year"


class PeriodRelation(str, Enum):
    EQUAL = "equal"
    CONTAINS = "contains"  # a contains b
    CONTAINED_BY = "contained_by"
    OVERLAPS = "overlaps"
    DISJOINT = "disjoint"


_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    granularity: Granularity
    fiscal: bool
    label: str
    raw: str = ""
    ambiguous: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "granularity": self.granularity.value,
            "fiscal": self.fiscal,
            "label": self.label,
            "raw": self.raw,
            "ambiguous": self.ambiguous,
            "notes": list(self.notes),
        }

    def key(self) -> str:
        """Identity used inside a claim key: the interval, never the label."""
        return f"{self.start.isoformat()}/{self.end.isoformat()}"


def _end_of_month(y: int, m: int) -> date:
    return date(y + 1, 1, 1) - _DAY if m == 12 else date(y, m + 1, 1) - _DAY


from datetime import timedelta as _timedelta  # noqa: E402

_DAY = _timedelta(days=1)


def fiscal_year(end_year: int) -> tuple[date, date]:
    """The Indian fiscal year *ending* in ``end_year``: FY24 -> 2023-04-01..2024-03-31."""
    return date(end_year - 1, FY_START_MONTH, 1), date(end_year, FY_START_MONTH, 1) - _DAY


def _fy_label(end_year: int) -> str:
    return f"FY{end_year - 1}-{str(end_year)[2:]}"


def _expand_year(y: int) -> int:
    return y if y >= 100 else (2000 + y if y < 70 else 1900 + y)


def _fy_end_from_pair(first: int, second: int) -> int:
    """Resolve '2023-24', '2023/24', '23-24' to the fiscal year's ending calendar year."""
    first = _expand_year(first)
    if second < 100:
        second = first - (first % 100) + second
        if second < first:  # e.g. 1999-00
            second += 100
    return second


# --------------------------------------------------------------------------------------
# Patterns, tried in order. Longest / most specific first.
# --------------------------------------------------------------------------------------

_QTR = r"(?P<q>[1-4])"

# A year *pair* is only a fiscal year if it is either FY-prefixed ("FY23-24") or begins with
# a full four-digit year ("2023-24"). Without that guard a bare two-digit pair swallows page
# ranges, note references and numeric ranges — "page 12-14" would parse as FY2013-14.
_FYP = r"(?:FY\s*(?P<y1a>\d{2,4})|(?P<y1b>(?:19|20)\d{2}))\s*[-/]\s*(?P<y2>\d{2,4})"
_FYS = r"FY\s*(?P<y>\d{2,4})"


def _pair_start_year(m: re.Match) -> str | None:
    """The first year of a matched pair, from whichever alternative fired."""
    return m.groupdict().get("y1a") or m.groupdict().get("y1b")


def _quarter_bounds(fy_end: int, q: int) -> tuple[date, date]:
    """Indian fiscal quarters: Q1 Apr-Jun, Q2 Jul-Sep, Q3 Oct-Dec, Q4 Jan-Mar."""
    start_month = FY_START_MONTH + 3 * (q - 1)
    year = fy_end - 1 + (start_month - 1) // 12
    start_month = (start_month - 1) % 12 + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    end_year = year + (end_month - 1) // 12
    end_month = (end_month - 1) % 12 + 1
    return start, _end_of_month(end_year, end_month)


def _mk(start, end, gran, fiscal, label, raw, ambiguous=False, notes=()):
    return Period(start, end, gran, fiscal, label, raw, ambiguous, tuple(notes))


def parse_period(text: str) -> Period | None:
    """Parse a period expression. Returns None when nothing period-like is present."""
    if not text:
        return None
    raw = " ".join(str(text).split())
    t = raw.strip().rstrip(".,;:")
    low = t.lower()

    # --- point in time: "as of March 31, 2024" / "as at 31 March 2024" ----------------
    m = re.search(
        r"(?:as\s+(?:of|at|on)\s+)?"
        r"(?:(?P<d1>\d{1,2})\s+(?P<mon1>[a-z]{3,9})\.?,?\s+(?P<y1>\d{4})"
        r"|(?P<mon2>[a-z]{3,9})\.?\s+(?P<d2>\d{1,2}),?\s+(?P<y2>\d{4}))",
        low,
    )
    if m:
        mon = (m.group("mon1") or m.group("mon2"))[:3]
        if mon in _MONTHS:
            d = int(m.group("d1") or m.group("d2"))
            y = int(m.group("y1") or m.group("y2"))
            try:
                dt = date(y, _MONTHS[mon], d)
            except ValueError:
                return None
            return _mk(dt, dt, Granularity.INSTANT, False, dt.isoformat(), raw)

    # --- quarter / half / nine months, against a fiscal year -------------------------
    m = re.search(rf"\bQ{_QTR}\s*(?:of\s*)?(?:{_FYP}|{_FYS})", t, re.I)
    if m:
        q = int(m.group("q"))
        y1 = _pair_start_year(m)
        fy_end = (
            _fy_end_from_pair(int(y1), int(m.group("y2")))
            if y1
            else _expand_year(int(m.group("y")))
        )
        s, e = _quarter_bounds(fy_end, q)
        return _mk(s, e, Granularity.QUARTER, True, f"Q{q} {_fy_label(fy_end)}", raw)

    m = re.search(rf"\bH(?P<h>[12])\s*(?:of\s*)?(?:{_FYP}|{_FYS})", t, re.I)
    if m:
        h = int(m.group("h"))
        y1 = _pair_start_year(m)
        fy_end = (
            _fy_end_from_pair(int(y1), int(m.group("y2")))
            if y1
            else _expand_year(int(m.group("y")))
        )
        s = _quarter_bounds(fy_end, 1 if h == 1 else 3)[0]
        e = _quarter_bounds(fy_end, 2 if h == 1 else 4)[1]
        return _mk(s, e, Granularity.HALF, True, f"H{h} {_fy_label(fy_end)}", raw)

    m = re.search(rf"\b(?:9M|nine\s+months?)\s*(?:of\s*)?(?:{_FYP}|{_FYS})", t, re.I)
    if m:
        y1 = _pair_start_year(m)
        fy_end = (
            _fy_end_from_pair(int(y1), int(m.group("y2")))
            if y1
            else _expand_year(int(m.group("y")))
        )
        s = _quarter_bounds(fy_end, 1)[0]
        e = _quarter_bounds(fy_end, 3)[1]
        return _mk(s, e, Granularity.NINE_MONTH, True, f"9M {_fy_label(fy_end)}", raw)

    # --- calendar year ---------------------------------------------------------------
    m = re.search(r"\b(?:CY|calendar\s+year)\s*(?P<y>\d{4})\b", t, re.I)
    if m:
        y = int(m.group("y"))
        return _mk(date(y, 1, 1), date(y, 12, 31), Granularity.YEAR, False, f"CY{y}", raw)

    # --- fiscal year written as a pair: 2023-24, 2023/24, FY2024/25, FY 2023-24 -------
    m = re.search(rf"\b{_FYP}\b", t, re.I)
    if m:
        y1, y2 = int(_pair_start_year(m)), int(m.group("y2"))
        # Guard against a range of calendar years ("2019-2023") spanning more than one year.
        if not (len(m.group("y2")) == 4 and y2 - _expand_year(y1) > 1):
            fy_end = _fy_end_from_pair(y1, y2)
            s, e = fiscal_year(fy_end)
            return _mk(s, e, Granularity.YEAR, True, _fy_label(fy_end), raw)

    # --- fiscal year written alone: FY24, FY2024 -------------------------------------
    m = re.search(rf"\b{_FYS}\b", t, re.I)
    if m:
        fy_end = _expand_year(int(m.group("y")))
        s, e = fiscal_year(fy_end)
        notes = ()
        ambiguous = False
        if len(m.group("y")) == 4:
            ambiguous = True
            notes = (
                "'FY2024' read as the Indian fiscal year ending March 2024; some "
                "publishers use it for the year beginning 2024",
            )
        return _mk(s, e, Granularity.YEAR, True, _fy_label(fy_end), raw, ambiguous, notes)

    # --- month: "March 2024" ---------------------------------------------------------
    m = re.search(r"\b(?P<mon>[a-z]{3,9})\.?\s+(?P<y>\d{4})\b", low)
    if m and m.group("mon")[:3] in _MONTHS:
        mon, y = _MONTHS[m.group("mon")[:3]], int(m.group("y"))
        return _mk(
            date(y, mon, 1), _end_of_month(y, mon), Granularity.MONTH, False,
            f"{y}-{mon:02d}", raw,
        )

    # --- bare year: genuinely ambiguous, and flagged as such --------------------------
    m = re.fullmatch(r"\s*(?P<y>(?:19|20)\d{2})\s*", t)
    if m:
        y = int(m.group("y"))
        return _mk(
            date(y, 1, 1), date(y, 12, 31), Granularity.YEAR, False, f"CY{y}", raw,
            ambiguous=True,
            notes=("bare year read as calendar year; the source may mean a fiscal year",),
        )

    return None


def find_periods(text: str) -> list[Period]:
    """Scan free text for period expressions, for when an extraction omits the period."""
    found: dict[str, Period] = {}
    patterns = [
        rf"\bQ[1-4]\s*(?:{_FYP}|{_FYS})",
        rf"\bH[12]\s*(?:{_FYP}|{_FYS})",
        rf"\b(?:9M|nine\s+months?)\s*(?:{_FYP}|{_FYS})",
        r"\b(?:CY|calendar\s+year)\s*\d{4}\b",
        r"\bFY\s*\d{2,4}\s*[-/]\s*\d{2,4}\b",
        r"\b(?:19|20)\d{2}\s*[-/]\s*\d{2,4}\b",
        r"\bFY\s*\d{2,4}\b",
        r"\bas\s+(?:of|at|on)\s+[A-Za-z0-9,\s]{6,20}\d{4}\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            p = parse_period(m.group(0))
            if p:
                found.setdefault(p.key(), p)
    return list(found.values())


def relation(a: Period, b: Period) -> PeriodRelation:
    if a.start == b.start and a.end == b.end:
        return PeriodRelation.EQUAL
    if a.start <= b.start and a.end >= b.end:
        return PeriodRelation.CONTAINS
    if b.start <= a.start and b.end >= a.end:
        return PeriodRelation.CONTAINED_BY
    if a.start <= b.end and b.start <= a.end:
        return PeriodRelation.OVERLAPS
    return PeriodRelation.DISJOINT


def same_period(a: Period | None, b: Period | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return relation(a, b) is PeriodRelation.EQUAL
