"""Tests for content-derived fiscal-year detection (the fix for URL-derived
year labels being wrong). Covers rule_extract.detect_cover_year and the
verify_pdf year/interim gate's decision logic.

The pure regexes are unit-tested directly; the async verify path is tested via
a tiny fake httpx client so no network is touched.

Runs under pytest, or standalone:  python tests/test_content_year.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rule_extract import detect_cover_year  # noqa: E402
import verify_pdf  # noqa: E402


# ---- cover-year regex --------------------------------------------------------

def test_cover_year_english_year_ended():
    assert detect_cover_year("Annual Report 2025\nFor the year ended March 31, 2025") == 2025


def test_cover_year_takes_latest_when_two_present():
    # "years ended December 31, 2024 and 2023" -> the report's headline year 2024
    assert detect_cover_year("For the years ended December 31, 2024 and 2023") == 2024


def test_cover_year_french():
    assert detect_cover_year("Rapport annuel 2023") == 2023
    assert detect_cover_year("Exercice clos le 31 mars 2022") == 2022


def test_cover_year_none_when_absent():
    assert detect_cover_year("Consolidated balance sheet\ntotal assets 1,234") is None


def test_cover_year_ignores_implausible_year():
    assert detect_cover_year("annual report 1789") is None


# ---- verify_pdf year / interim gate -----------------------------------------

_PDF_MAGIC = b"%PDF-1.7 fake"


class _FakeResp:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status


class _FakeClient:
    def __init__(self, content: bytes, status: int = 200):
        self._content, self._status = content, status

    async def get(self, url, headers=None, timeout=None, follow_redirects=False):
        return _FakeResp(self._content, self._status)


def _run(content, *, expected_year, years, headline, is_interim, monkeypatch_target):
    """Patch content_fiscal_years so the gate's year/interim branch is exercised
    deterministically without a real PDF."""
    import pdf_extract
    orig = pdf_extract.content_fiscal_years
    pdf_extract.content_fiscal_years = lambda _b: (years, headline, is_interim)
    # doc_classify would run on the fake bytes; force the PRIMARY-accept path by
    # making classify raise so verify_pdf falls back, then FS signals accept.
    try:
        client = _FakeClient(content)
        accept, reason = asyncio.run(verify_pdf.looks_like_financial_statement(
            client, "http://x/y.pdf", "ua", 5, company_name=None,
            expected_year=expected_year))
    finally:
        pdf_extract.content_fiscal_years = orig
    return accept, reason


def test_verify_rejects_interim_when_year_requested():
    accept, reason = _run(_PDF_MAGIC + b" balance sheet total assets",
                          expected_year=2024, years=[], headline=None,
                          is_interim=True, monkeypatch_target=None)
    assert accept is False and reason == "interim_or_quarterly_report"


def test_verify_rejects_wrong_year_content():
    # requested 2020 but the PDF's content is the 2025 report -> wrong document
    accept, reason = _run(_PDF_MAGIC + b" statement of financial position",
                          expected_year=2020, years=[2025, 2024], headline=2025,
                          is_interim=False, monkeypatch_target=None)
    assert accept is False and reason.startswith("wrong_year_content")


def test_verify_tolerates_off_by_one():
    # a report filed in 2026 covers fiscal 2025; requested 2026, content [2025]
    accept, _reason = _run(_PDF_MAGIC + b" consolidated statement total assets",
                           expected_year=2026, years=[2025, 2024], headline=2025,
                           is_interim=False, monkeypatch_target=None)
    assert accept is True


def test_verify_accepts_exact_year_match():
    accept, _reason = _run(_PDF_MAGIC + b" consolidated statement total liabilities",
                           expected_year=2024, years=[2024, 2023], headline=2024,
                           is_interim=False, monkeypatch_target=None)
    assert accept is True


def test_verify_no_expected_year_skips_year_check():
    # without expected_year the content-year branch is never entered
    accept, _reason = _run(_PDF_MAGIC + b" balance sheet total assets shareholders equity",
                           expected_year=None, years=[1999], headline=1999,
                           is_interim=True, monkeypatch_target=None)
    # is_interim above must NOT reject because expected_year is None (branch skipped)
    assert accept is True


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
