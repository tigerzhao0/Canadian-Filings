"""Test the large-document section re-anchor in pdf_extract (monkeypatches
_page_texts so no PDF-writing dependency is needed).

Runs under pytest, or standalone:  python tests/test_pdf_extract_reanchor.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pdf_extract as pe  # noqa: E402

_PDF = b"%PDF-1.4 fake"


def _extract(pages):
    with mock.patch.object(pe, "_page_texts", return_value=pages):
        return pe.extract_statements(_PDF)


def _long(header, n=25):
    body = "\n".join(f"Line item {i} 1,234 5,678" for i in range(n))
    return f"{header}\n{body}"


def test_smallest_span_helper():
    assert pe._smallest_span_covering_all_types(
        {"income_statement": [15], "balance_sheet": [16], "cash_flow": [17]}) == (15, 17)
    # a scattered earlier income hit shouldn't widen the chosen span
    assert pe._smallest_span_covering_all_types(
        {"income_statement": [5, 15], "balance_sheet": [16], "cash_flow": [17]}) == (15, 17)
    # any type with zero header pages -> no span
    assert pe._smallest_span_covering_all_types(
        {"income_statement": [5], "balance_sheet": [6], "cash_flow": []}) is None


def test_reanchor_past_misleading_early_markers():
    # An EARLIER unrelated "auditor's report" + a stray early "notes to..." pull
    # the naive window onto filler pages with NO real statements; the real
    # statements sit much later. Must re-anchor onto them.
    filler = lambda i: f"Filler page {i}\nunrelated discussion\nmore text"  # noqa: E731
    pages = [filler(i) for i in range(20)]
    pages[3] = "Report of Independent Registered Public Accounting Firm\n(internal controls opinion)"
    pages[5] = "Notes to the Consolidated Financial Statements\n(a cross-reference in MD&A)"
    pages[14] = "Independent Auditor's Report\nWe have audited..."
    pages[15] = _long("Consolidated Statements of Income")
    pages[16] = _long("Consolidated Balance Sheets")
    pages[17] = _long("Consolidated Statements of Cash Flows")
    res = _extract(pages)
    assert res.ok
    assert "consolidated statements of income" in res.sections["income_statement"].lower()
    assert "consolidated balance sheets" in res.sections["balance_sheet"].lower()
    assert "consolidated statements of cash flows" in res.sections["cash_flow"].lower()


def test_normal_small_filing_unaffected():
    # naive window already contains headers -> re-anchor must NOT fire / regress.
    pages = [
        "Cover\nAnnual report",
        "Independent Auditor's Report\nWe have audited...",
        _long("Statements of Loss and Comprehensive Loss"),
        _long("Statements of Financial Position"),
        _long("Statements of Cash Flows"),
        "Notes to the Financial Statements\n1. Nature of operations",
    ]
    res = _extract(pages)
    assert res.ok
    assert "income_statement" in res.sections
    assert "balance_sheet" in res.sections
    assert "cash_flow" in res.sections


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
