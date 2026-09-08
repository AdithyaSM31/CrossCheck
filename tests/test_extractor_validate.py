"""Tests for the shape-validation gate a proposed fact must pass before grounding.

The undecomposed-row check exists because of a real failure: on a wide table row, some
models copy the whole row back as one "value" string instead of one fact per cell. That
string still contains real numbers, so it grounds perfectly, and normalize.values.parse_value
happily reads off the first number it finds -- typically a year, since row-major cells lead
with a period like "2021/22=..." -- as if it were the fact's value. The result is a
confidently wrong fact sourced from real evidence. See docs/four-cases.md, case 4.
"""

import pytest

from crosscheck.extract.extractor import _looks_like_undecomposed_row, validate


@pytest.mark.parametrize(
    "value",
    [
        "2021/22=-7.7; 2022/23=-8.2; 2023/24=-8.1; 2024/25=-7.9; 2025/26=-7.2; 2026/27=-7.1",
        "Q1 FY24=100; Q2 FY24=110; Q3 FY24=120; Q4 FY24=130",
        "2022=5.5; 2023=6.1; 2024=6.8",
    ],
)
def test_undecomposed_row_is_rejected(value):
    assert _looks_like_undecomposed_row(value)
    fact, why = validate({"attribute": "x", "value": value, "evidence_quote": "q" * 20})
    assert fact is None and "table row" in why


@pytest.mark.parametrize(
    "value",
    [
        "9.2", "₹8,142 Cr", "(6.3%)", "18,074", "1.4 Mn Tons", "Q4 FY24",
        "2023-24", "Rs. 127 Cr", "resigned", "FY2024/25", "9.7", "-8.7",
    ],
)
def test_legitimate_scalar_values_are_not_flagged(value):
    """A single value, however it's formatted, must never be caught by the row-dump
    guard -- a false positive here silently discards a real, correctly extracted fact."""
    assert not _looks_like_undecomposed_row(value)
    fact, why = validate({"attribute": "x", "value": value, "evidence_quote": "q" * 20})
    assert fact is not None


def test_validate_rejects_missing_fields():
    assert validate({})[0] is None
    assert validate({"attribute": "x"})[0] is None  # no value
    assert validate({"attribute": "x", "value": "1"})[0] is None  # no quote
    assert validate("not a dict")[0] is None


def test_validate_lowercases_the_attribute():
    fact, _ = validate(
        {"attribute": "Revenue From Services", "value": "1", "evidence_quote": "q" * 20}
    )
    assert fact["attribute"] == "revenue from services"


def test_validate_clamps_confidence_to_unit_range():
    fact, _ = validate(
        {"attribute": "x", "value": "1", "evidence_quote": "q" * 20, "confidence": 5}
    )
    assert fact["confidence"] == 1.0
    fact, _ = validate(
        {"attribute": "x", "value": "1", "evidence_quote": "q" * 20, "confidence": -1}
    )
    assert fact["confidence"] == 0.0


def test_validate_treats_null_like_strings_as_absent():
    fact, _ = validate(
        {"attribute": "x", "value": "1", "evidence_quote": "q" * 20, "period": "null"}
    )
    assert fact["period"] is None
