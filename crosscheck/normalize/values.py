"""Value normalisation.

Turns the many ways this corpus writes a quantity into a comparable object:

    "Rs. (452 Cr)"      -> MONEY   INR  -4.52e9
    "Rs.8,142 Cr"       -> MONEY   INR   8.142e10
    "Rs.40,000.00 million" -> MONEY INR  4.0e10      (same corpus, different scale word)
    "(6.3%)"            -> PERCENT      -6.3
    "73 bps"            -> POINTS        0.73
    "1.4 Mn Tons"       -> QUANTITY tons 1.4e6
    "18,074"            -> NUMBER        18074

Two values are only ever compared inside the same ``unit_family``. Agreement is
significant-figure aware, so a source that rounds to "Rs.8,100 Cr" still corroborates a
source that reports "Rs.8,142 Cr", while 6.5 and 6.9 per cent do not.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum


class ValueKind(str, Enum):
    MONEY = "money"
    PERCENT = "percent"
    POINTS = "points"  # basis points / percentage points: a *difference*, not a level
    QUANTITY = "quantity"  # a count with a physical or domain unit
    NUMBER = "number"  # a bare number; context supplies the unit
    TEXT = "text"


# Scale words. Order matters only for the regex alternation, which is built longest-first.
SCALES: dict[str, float] = {
    "hundred": 1e2,
    "thousand": 1e3,
    "k": 1e3,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "million": 1e6,
    "millions": 1e6,
    "mn": 1e6,
    "mln": 1e6,
    "m": 1e6,
    "crore": 1e7,
    "crores": 1e7,
    "cr": 1e7,
    "billion": 1e9,
    "billions": 1e9,
    "bn": 1e9,
    "trillion": 1e12,
    "tn": 1e12,
    # Indian compound scales, which appear throughout the macro documents.
    "lakh crore": 1e12,
    "lakh crores": 1e12,
    "thousand crore": 1e10,
    "thousand crores": 1e10,
}

CURRENCIES: list[tuple[str, str]] = [
    ("₹", "INR"),
    ("rs.", "INR"),
    ("rs", "INR"),
    ("inr", "INR"),
    ("us$", "USD"),
    ("usd", "USD"),
    ("$", "USD"),
    ("€", "EUR"),
    ("eur", "EUR"),
    ("£", "GBP"),
    ("gbp", "GBP"),
]

# Domain units seen as suffixes. Anything unmatched but alphabetic is still kept as a
# free-form unit, so a new document can introduce a unit we have never seen.
_UNIT_ALIASES = {
    "ton": "tons",
    "tons": "tons",
    "tonne": "tons",
    "tonnes": "tons",
    "mt": "tons",
    "kg": "kg",
    "km": "km",
    "sqft": "sqft",
    "sq ft": "sqft",
    "days": "days",
    "day": "days",
    "x": "times",
    "times": "times",
}

_SCALE_RE = "|".join(
    re.escape(s) for s in sorted(SCALES, key=len, reverse=True)
)
_NUM_RE = r"[0-9][0-9,  ]*(?:\.[0-9]+)?"

_VALUE_RE = re.compile(
    rf"""
    (?P<neg>-|−)?\s*
    (?P<num>{_NUM_RE})\s*
    (?P<scale>{_SCALE_RE})?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class ParsedValue:
    kind: ValueKind
    number: float | None
    unit: str | None
    unit_family: str
    raw: str
    scale: str | None = None
    sig_figs: int = 0
    ambiguous: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "number": self.number,
            "unit": self.unit,
            "unit_family": self.unit_family,
            "raw": self.raw,
            "scale": self.scale,
            "sig_figs": self.sig_figs,
            "ambiguous": self.ambiguous,
            "notes": self.notes,
        }


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(" ", " ").replace("−", "-")
    return " ".join(text.split())


def _count_sig_figs(num_text: str) -> int:
    """Significant figures, using the standard convention.

    Trailing zeros in an integer with no decimal point are *not* significant, which is what
    lets "approximately Rs.8,000 crore" agree with "Rs.8,142 crore" while keeping 18,074
    pinned to five figures.
    """
    s = num_text.replace(",", "").replace(" ", "").lstrip("+-")
    if "." in s:
        digits = s.replace(".", "").lstrip("0")
        return len(digits) or 1
    stripped = s.lstrip("0")
    if not stripped:
        return 1
    return len(stripped.rstrip("0")) or 1


def _detect_currency(text: str) -> tuple[str | None, str]:
    """Return (currency_code, text_with_symbol_removed)."""
    low = text.lower()
    for token, code in CURRENCIES:
        idx = low.find(token)
        if idx == -1:
            continue
        # "rs" and "$" must not match inside a longer word (e.g. "hours", "US$" handled above).
        if token in {"rs", "inr", "usd", "eur", "gbp"}:
            if idx > 0 and low[idx - 1].isalpha():
                continue
            after = idx + len(token)
            if after < len(low) and low[after].isalpha():
                continue
        return code, text[:idx] + text[idx + len(token) :]
    return None, text


def parse_value(raw: str) -> ParsedValue:
    """Parse a value as written in a document into a normalised, comparable form."""
    original = raw
    text = _normalise(raw)
    if not text:
        return ParsedValue(ValueKind.TEXT, None, None, "text", original)

    notes: list[str] = []

    # Parenthesised values are negative in financial reporting: "(452)", "Rs.(452) Cr", "(6.3%)".
    negative = False
    if re.search(r"\(\s*[^)]*\d[^)]*\)", text):
        negative = True
        text = text.replace("(", " ").replace(")", " ")
        notes.append("parenthesised value read as negative")
    if re.match(r"^\s*-", text):
        negative = True

    currency, text = _detect_currency(text)

    low = text.lower()
    is_bps = bool(re.search(r"\bbps\b|\bbasis points?\b", low))
    is_pp = bool(re.search(r"\bpercentage points?\b|\bppt?s?\b", low))
    is_percent = bool(re.search(r"%|\bper\s?cents?\b|\bpercent(?:age)?\b", low)) and not is_pp

    match = _VALUE_RE.search(text)
    if not match:
        return ParsedValue(ValueKind.TEXT, None, None, "text", original, notes=notes)

    num_text = match.group("num").strip()
    try:
        number = float(num_text.replace(",", "").replace(" ", ""))
    except ValueError:
        return ParsedValue(ValueKind.TEXT, None, None, "text", original, notes=notes)

    sig_figs = _count_sig_figs(num_text)
    scale_word = (match.group("scale") or "").lower().strip() or None

    # Catch compound Indian scales ("lakh crore") that the token regex splits.
    compound = re.search(r"\b(lakh|thousand)\s+crores?\b", low)
    if compound:
        scale_word = f"{compound.group(1)} crore"
    if scale_word:
        number *= SCALES[scale_word]

    if negative:
        number = -abs(number)

    # --- classify -------------------------------------------------------------
    if is_bps:
        return ParsedValue(
            ValueKind.POINTS, number / 100.0, "pp", "points", original,
            scale=scale_word, sig_figs=sig_figs,
            notes=notes + ["basis points converted to percentage points"],
        )
    if is_pp:
        return ParsedValue(
            ValueKind.POINTS, number, "pp", "points", original,
            scale=scale_word, sig_figs=sig_figs, notes=notes,
        )
    if is_percent:
        return ParsedValue(
            ValueKind.PERCENT, number, "%", "percent", original,
            scale=scale_word, sig_figs=sig_figs, notes=notes,
        )
    if currency:
        return ParsedValue(
            ValueKind.MONEY, number, currency, f"currency:{currency}", original,
            scale=scale_word, sig_figs=sig_figs, notes=notes,
        )

    # Remaining trailing words become the unit, so unseen units still round-trip.
    tail = text[match.end():].strip().lower()
    tail = re.sub(r"^[^a-z]+", "", tail)
    unit_word = tail.split(",")[0].split(".")[0].strip()
    unit_word = " ".join(unit_word.split()[:2]) if unit_word else ""
    if unit_word:
        unit = _UNIT_ALIASES.get(unit_word, _UNIT_ALIASES.get(unit_word.split()[0], unit_word))
        return ParsedValue(
            ValueKind.QUANTITY, number, unit, f"quantity:{unit}", original,
            scale=scale_word, sig_figs=sig_figs, notes=notes,
        )

    return ParsedValue(
        ValueKind.NUMBER, number, None, "number", original,
        scale=scale_word, sig_figs=sig_figs, notes=notes,
    )


# --------------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------------

# A bare NUMBER is compared against anything numeric, because a table cell often loses the
# unit that its column header carried.
_COMPATIBLE_WITH_NUMBER = {"number", "percent", "points"}


def comparable(a: ParsedValue, b: ParsedValue) -> bool:
    if a.number is None or b.number is None:
        return False
    if a.unit_family == b.unit_family:
        return True
    fams = {a.unit_family, b.unit_family}
    if "number" in fams and fams - {"number"} <= _COMPATIBLE_WITH_NUMBER:
        return True
    return False


def _round_sig(x: float, sig: int) -> float:
    if x == 0:
        return 0.0
    from math import floor, log10

    return round(x, -int(floor(log10(abs(x)))) + (sig - 1))


def values_agree(
    a: ParsedValue, b: ParsedValue, rel_tol: float = 0.005
) -> tuple[bool, str]:
    """Do two values state the same thing?

    Returns (agree, reason). Significant-figure aware, so a rounded restatement of a figure
    corroborates rather than contradicts it.
    """
    if not comparable(a, b):
        return False, f"incomparable units ({a.unit_family} vs {b.unit_family})"

    x, y = a.number, b.number
    assert x is not None and y is not None

    if x == y:
        return True, "exact match"

    denom = max(abs(x), abs(y))
    if denom == 0:
        return True, "both zero"
    if abs(x - y) / denom <= rel_tol:
        return True, f"within {rel_tol:.1%} relative tolerance"

    # Floor at 2 significant figures: without it "Rs.1,000 Cr" would be 1 sig fig and would
    # agree with almost anything.
    sig = max(2, min(a.sig_figs or 15, b.sig_figs or 15))
    if _round_sig(x, sig) == _round_sig(y, sig):
        return True, f"agree at {sig} significant figures (one source is rounded)"

    return False, f"{x:g} vs {y:g} differ beyond rounding"
