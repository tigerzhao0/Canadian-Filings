"""annualreports.com structured-URL source.

The site hosts real ANNUAL REPORTS behind a fully deterministic URL scheme
(decoded and verified against Wayback CDX records -- live probes returned
HTTP 200 for every pattern below):

  current years : {BASE}/AnnualReports/PDF/{EXCH}_{TICKER}_{YEAR}.pdf
                  e.g. TSX_RY_2021.pdf, TSX-V_FIL_2020.pdf, OTC_ADDDF_2019.pdf
  older years   : {BASE}/AnnualReportArchive/{L}/{EXCH}_{TICKER}_{YEAR}.pdf
                  {L} = a single letter folder -- seen as BOTH the company
                  name's and the ticker's first letter, upper AND lower case
                  (/R/TSX_RY_2006.pdf, /r/TSX_RY_2018.pdf) -- probe the
                  variants, they're cheap ranged GETs.

Exchange prefixes seen in the wild: TSX, TSX-V (with the hyphen), OTC (many
CSE names trade OTC and are filed under their OTC symbol). A missing year is
a clean 404, so pattern probing needs no HTML scraping at all.

This module is pure URL construction (unit-testable); the async pass that
drives it lives in pipeline.py (`_run_annualreports_pass`).
"""
from __future__ import annotations

BASE = "https://www.annualreports.com/HostedData"


def exchange_prefixes(exchange: str | None) -> list[str]:
    """Most-likely-first annualreports.com exchange codes for one of OUR
    exchange labels (TSX / TSXV / CNSX|CSE / NEOE ...)."""
    e = (exchange or "").upper()
    if "TSXV" in e or "TSX-V" in e or "VENTURE" in e or e == "XTSX":
        return ["TSX-V", "OTC"]
    if "TSX" in e or e == "XTSE":
        return ["TSX", "OTC"]
    # CSE / NEO names usually appear under their OTC symbol when present
    return ["OTC", "TSX", "TSX-V"]


def ticker_variants(ticker: str) -> list[str]:
    """RAY.A -> [RAY.A, RAY, RAYA]; HR.UN -> [HR.UN, HR, HRUN]. Order matters:
    exact first."""
    t = (ticker or "").strip().upper()
    out = [t]
    base = t.split(".")[0]
    for v in (base, t.replace(".", "")):
        if v and v not in out:
            out.append(v)
    return out


def archive_letters(company_name: str | None, ticker: str) -> list[str]:
    letters: list[str] = []
    for src in ((company_name or "").strip(), (ticker or "").strip()):
        if src and src[0].isalpha():
            for L in (src[0].lower(), src[0].upper()):
                if L not in letters:
                    letters.append(L)
    return letters or ["a"]


def current_url(exch: str, tick: str, year: int) -> str:
    return f"{BASE}/AnnualReports/PDF/{exch}_{tick}_{year}.pdf"


def archive_urls(exch: str, tick: str, year: int,
                 company_name: str | None) -> list[str]:
    return [f"{BASE}/AnnualReportArchive/{L}/{exch}_{tick}_{year}.pdf"
            for L in archive_letters(company_name, tick)]


def year_urls(exch: str, tick: str, year: int,
              company_name: str | None) -> list[str]:
    """All candidate URLs for one (prefix, ticker, year), current path first."""
    return [current_url(exch, tick, year)] + archive_urls(exch, tick, year, company_name)
