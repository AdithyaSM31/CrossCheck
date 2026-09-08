"""Layout reconstruction tests.

These are regressions for bugs that were real, silent and corrupting: a value bound to the
wrong year, a row label eaten by a phantom column, a figure deleted from a slide. Each one
would have produced confident, evidence-backed, wrong facts downstream.
"""

from pathlib import Path

import pytest

from crosscheck.ingest.blocks import _is_heading, build_blocks
from crosscheck.ingest.layout import (
    Line,
    Word,
    build_lines,
    find_running_lines,
    find_table_regions,
    is_numeric_token,
    reconstruct_paragraphs,
    reconstruct_table,
)

PAGE_W = 612.0
STARTER = Path(__file__).resolve().parent.parent / "starter-datasets"


def w(x0, y, text, width=30.0, size=10.0, bold=False):
    return Word(x0, y, x0 + width, y + 10, text, size, bold)


def _table_words():
    """A small financial table: two year columns, right-aligned numbers, ragged labels."""
    return [
        w(300, 100, "2022/23"), w(380, 100, "2023/24"),
        w(50, 120, "Revenue"), w(305, 120, "7.6", 20), w(385, 120, "9.2", 20),
        w(50, 140, "Profit"), w(305, 140, "1.2", 20), w(385, 140, "2.4", 20),
        w(50, 160, "Margin"), w(305, 160, "3.1", 20), w(385, 160, "4.5", 20),
    ]


def test_values_are_bound_to_their_column_header():
    lines = build_lines(_table_words())
    start, end = find_table_regions(lines)[0]
    rows, _ = reconstruct_table(lines[start : end + 1], PAGE_W)
    by_label = {r["label"]: r["cells"] for r in rows}
    assert by_label["Revenue"] == {"2022/23": "7.6", "2023/24": "9.2"}
    assert by_label["Margin"]["2023/24"] == "4.5"


def test_table_title_does_not_capture_column_headers():
    """The title sits above the body and contains a year range, so a first-come header fill
    lets it scatter its own words across the columns — 'Real GDP: Indicators,=7.6'."""
    words = [
        w(60, 80, "Table"), w(95, 80, "1."), w(115, 80, "Indicators,"),
        w(200, 80, "2021/22-2026/27"), w(300, 80, "1/"),
    ] + _table_words()
    lines = build_lines(words)
    start, end = find_table_regions(lines)[0]
    rows, caption = reconstruct_table(lines[start : end + 1], PAGE_W)
    headers = {h for r in rows for h in r["cells"]}
    assert headers == {"2022/23", "2023/24"}
    assert "Table" in caption


def test_footnote_markers_do_not_form_a_phantom_column():
    """'Pin-code reach(1)' — the '(1)' is a footnote marker, not data. Clustered as a
    column it sits on top of the label and truncates it to 'Pin-code'."""
    words = _table_words() + [
        w(50, 180, "Pin-code"), w(105, 180, "reach(1)"),
        w(305, 180, "18,074", 25), w(385, 180, "18,540", 25),
    ]
    lines = build_lines(words)
    start, end = find_table_regions(lines)[0]
    rows, _ = reconstruct_table(lines[start : end + 1], PAGE_W)
    labels = [r["label"] for r in rows]
    assert "Pin-code reach(1)" in labels


def test_split_decimals_are_rejoined():
    """PDF word extraction splits '456.1' into '456.' and '1'."""
    words = [
        w(300, 100, "2022/23"), w(380, 100, "2023/24"),
        w(50, 120, "Exports"), w(300, 120, "456.", 18), w(320, 120, "1", 8),
        w(385, 120, "441.4", 22),
        w(50, 140, "Imports"), w(305, 140, "618.6", 22), w(385, 140, "686.4", 22),
        w(50, 160, "Balance"), w(305, 160, "-38.7", 22), w(385, 160, "-26.0", 22),
    ]
    lines = build_lines(words)
    start, end = find_table_regions(lines)[0]
    rows, _ = reconstruct_table(lines[start : end + 1], PAGE_W)
    exports = next(r for r in rows if r["label"] == "Exports")
    assert exports["cells"]["2022/23"] == "456.1"


def test_words_from_different_columns_form_one_row():
    """Row banding, not PyMuPDF's line index — a table row is often split across blocks."""
    lines = build_lines(_table_words())
    revenue = next(ln for ln in lines if "Revenue" in ln.text)
    assert "7.6" in revenue.text and "9.2" in revenue.text


def test_wrapped_lines_reflow_into_one_paragraph():
    lines = build_lines([
        w(52, 100, "Taking into account these factors, real GDP", 240),
        w(52, 116, "growth for 2025-26 is projected at 6.5 per", 240),
        w(52, 132, "cent, with risks evenly balanced.", 150),
    ])
    paras = reconstruct_paragraphs(lines, col_width=244)
    assert len(paras) == 1
    assert "real GDP growth for 2025-26 is projected at 6.5 per cent" in paras[0][0]


def test_hyphenated_line_break_is_joined():
    lines = build_lines([
        w(52, 100, "positively associated with productivity growth in the long-", 240),
        w(52, 116, "run. To further strengthen its capabilities the government", 240),
    ])
    text = reconstruct_paragraphs(lines, col_width=244)[0][0]
    assert "long-run." in text and "long- run" not in text


def test_running_headers_are_detected():
    pages = [
        build_lines([w(256, 20, "ANNUAL REPORT 2024-25", 100),
                     w(52, 400, f"body text on page {i}", 200),
                     w(300, 770, str(i), 10)])
        for i in range(10)
    ]
    furniture = find_running_lines(pages, page_height=792.0)
    assert "ANNUAL REPORT #-#" in furniture


@pytest.mark.parametrize("text", ["₹8,142 Cr", "1.4 Mn Tons", "Rs. 127 Cr", "18,793"])
def test_headline_figures_are_not_headings(text):
    """A slide's big number is short, large and does not fill its column, so it passes every
    stylistic heading test. Swallowed into the section path, it is a value nobody extracts."""
    line = build_lines([w(100, 100, text, 90, size=20.0, bold=True)])[0]
    assert not _is_heading(line, body_size=10.0, col_width=400.0)


def test_real_headings_still_register():
    line = build_lines([w(100, 100, "Assessment and Prospects", 120, size=14.0, bold=True)])[0]
    assert _is_heading(line, body_size=10.0, col_width=400.0)


@pytest.mark.parametrize(
    "tok,expected",
    [("9.7", True), ("18,074", True), ("(452)", True), ("-8.7", True), ("6.5%", True),
     ("2021/22", False), ("Revenue", False), ("", False)],
)
def test_numeric_token_detection(tok, expected):
    assert is_numeric_token(tok) is expected


# ------------------------------------------------------------------ integration
IMF = STARTER / "india-macroeconomy" / "03-imf-india-2025-article-iv-excerpt.pdf"


@pytest.mark.skipif(not IMF.exists(), reason="starter dataset not present")
def test_imf_table_one_extracts_with_correct_years():
    """End-to-end on a real borderless table. PyMuPDF's own extractor shreds this one into
    'Real GDP (a' | 't market price' | ')' with the values offset from their headers."""
    from crosscheck.ingest.pdf import read_document

    pages = read_document(IMF)
    blocks = build_blocks(pages[4:5])
    tables = [b for b in blocks if b.kind == "table"]
    assert tables, "no table found on IMF page 4"
    text = "\n".join(b.text for b in tables)
    assert "Real GDP (at market prices):" in text
    row = next(l for l in text.splitlines() if l.startswith("Real GDP (at market prices)"))
    for pair in ["2021/22=9.7", "2022/23=7.6", "2023/24=9.2", "2024/25=6.5"]:
        assert pair in row, f"{pair} missing from: {row}"
