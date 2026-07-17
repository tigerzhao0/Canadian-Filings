"""Regression test for the year-pattern-probe corruption bug: CSE/TMX filing
URLs carry a '&dateFiled=YYYY-MM-DD' query param, which used to false-positive
as a "year in the filename" and get rewritten across up to 10 fake fiscal
years, all resolving to the SAME real document (confirmed: 220 fabricated
filing_pdfs rows across 32 tickers in production data). Fixed in pipeline.py's
_year_probe_candidates: operate on the URL PATH only (never the query string),
and exclude opaque per-filing-ID hosts (CSE/TMX/SEC) entirely.

Runs under pytest, or standalone:  python tests/test_year_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pipeline import _year_probe_candidates  # noqa: E402

TARGETS = set(range(2017, 2027))  # a 10-year window


def test_cse_query_string_year_never_treated_as_pattern():
    # the exact real-world shape that corrupted 32 tickers: a 'dateFiled'
    # query param containing a year-shaped substring, on an opaque CSE host.
    rows = [(2026,
             "https://sedar-filings-backup.thecse.com/000022839/06408296-"
             "00000001-000022839-Digicann_Fin.pdf?formDescription=Annual"
             "+report&dateFiled=2026-07-02",
             "Digicann Ventures Inc", "CNSX")]
    assert _year_probe_candidates(rows, TARGETS) == []


def test_tmx_quotemedia_url_never_probed():
    rows = [(2026,
             "https://app.quotemedia.com/data/downloadFiling?webmasterId="
             "101020&ref=69b880e4d8e31e3b077c",
             "LunR Royalties Corp", "TSXV")]
    assert _year_probe_candidates(rows, TARGETS) == []


def test_sec_url_never_probed():
    rows = [(2025, "https://www.sec.gov/Archives/edgar/data/12345/ar2025.pdf",
             "Some Corp", "TSX")]
    assert _year_probe_candidates(rows, TARGETS) == []


def test_genuine_filename_pattern_still_works():
    # rbc.com's real, verified pattern -- must keep working.
    rows = [(2025, "https://www.rbc.com/investor-relations/_assets-custom/"
                   "pdf/ar_2025_e.pdf", "Royal Bank of Canada", "TSX")]
    out = dict(_year_probe_candidates(rows, TARGETS))
    assert out[2024] == ("https://www.rbc.com/investor-relations/_assets-custom/"
                         "pdf/ar_2024_e.pdf")
    assert out[2017] == ("https://www.rbc.com/investor-relations/_assets-custom/"
                         "pdf/ar_2017_e.pdf")
    assert 2025 not in out          # already have it, not re-probed
    assert len(out) == len(TARGETS) - 1


def test_never_produces_duplicate_urls_for_different_years():
    # the actual failure mode: every candidate URL must be DISTINCT.
    rows = [(2026,
             "https://sedar-filings-backup.thecse.com/x/06408296-"
             "annual.pdf?dateFiled=2026-07-02", "X Corp", "CNSX"),
            (2025,
             "https://sedar-filings-backup.thecse.com/x/06408296-"
             "annual.pdf?dateFiled=2025-07-02", "X Corp", "CNSX")]
    out = _year_probe_candidates(rows, TARGETS)
    urls = [u for _y, u in out]
    assert len(urls) == len(set(urls)), "duplicate candidate URLs produced"
    assert out == []  # both rows are on the opaque host -- no candidates at all


def test_query_string_year_does_not_block_genuine_pattern():
    # a real filename-year host that ALSO happens to carry a year-shaped query
    # param elsewhere -- the path match must still work and the query string
    # must be left untouched (not corrupted by the .replace()).
    rows = [(2025, "https://ir.example.com/reports/ar_2025_e.pdf?v=2025",
             "Example Corp", "TSX")]
    out = dict(_year_probe_candidates(rows, TARGETS))
    assert out[2020] == "https://ir.example.com/reports/ar_2020_e.pdf?v=2025"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
