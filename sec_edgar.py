"""SEC EDGAR helper: detect US/SEC filers and point at their latest annual filing.

Many Canadian issuers (Shopify, Tilray, ...) route their financials to the SEC
(10-K / 40-F / 20-F) rather than posting a PDF annual report on their own IR
site. For those, a first-party PDF often doesn't exist — so instead of dumping
them in the manual-review queue, we FLAG them as SEC filers and record a direct
link to their most recent annual filing on EDGAR (an official first-party source,
never SEDAR).

Detection is offline and cheap: SEC's public `company_tickers.json` (one cached
download) maps ticker/name -> CIK. The latest annual filing URL is fetched from
the free `data.sec.gov/submissions` API, only for companies we're about to flag.

SEC fair-access policy asks for a descriptive User-Agent with contact info and
<10 requests/second; both are honoured by the caller's config + concurrency cap.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ingest import name_tokens

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# Annual-report form types (incl. amendments), newest-first in the submissions feed.
ANNUAL_FORMS = ("40-F", "10-K", "20-F", "40-F/A", "10-K/A", "20-F/A")


class SecFilerIndex:
    """Ticker/name -> CIK lookup built from SEC's company_tickers.json."""

    def __init__(self, ticker_map: dict[str, tuple[int, str]], name_exact: dict[str, int]):
        self._ticker = ticker_map          # TICKER -> (cik, title)
        self._name_exact = name_exact      # sorted-token key -> cik

    @classmethod
    def load(cls, user_agent: str, cache_path: str = ".sec_company_tickers.json",
             max_age_days: float = 7) -> "SecFilerIndex":
        data = _load_cached_or_fetch(user_agent, Path(cache_path), max_age_days)
        ticker_map: dict[str, tuple[int, str]] = {}
        name_exact: dict[str, int] = {}
        for v in data.values():
            try:
                cik = int(v["cik_str"])
                ticker = str(v["ticker"]).upper()
                title = str(v["title"])
            except (KeyError, ValueError, TypeError):
                continue
            ticker_map.setdefault(ticker, (cik, title))
            key = _name_key(title)
            if key:
                name_exact.setdefault(key, cik)
        return cls(ticker_map, name_exact)

    def lookup(self, ticker: str, name: str) -> int | None:
        """Return the CIK if this company is an SEC filer, else None.

        Strong signals only, to avoid false flags: an exact name-token match, or
        a ticker match whose EDGAR title shares a name token (guards against
        unrelated companies that happen to share a ticker symbol)."""
        toks = set(name_tokens(name))
        key = _name_key(name)
        if key and key in self._name_exact:
            return self._name_exact[key]
        t = (ticker or "").upper()
        for cand in (t, t.split(".")[0]):  # strip TSX suffixes like .A / .H / .UN
            if cand and cand in self._ticker:
                cik, title = self._ticker[cand]
                if _similar(toks, set(name_tokens(title))):
                    return cik
        return None


async def latest_annual_filing(client, cik: int, user_agent: str, timeout: float = 20) -> dict | None:
    """Return {'form','date','url'} for the most recent annual filing, or None."""
    try:
        resp = await client.get(SUBMISSIONS_URL.format(cik=cik),
                                headers={"User-Agent": user_agent}, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200:
        return None
    recent = resp.json().get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    dates = recent.get("filingDate", [])
    for i, form in enumerate(forms):
        if form in ANNUAL_FORMS:
            adsh = accs[i].replace("-", "") if i < len(accs) else ""
            doc = docs[i] if i < len(docs) else ""
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{doc}"
            return {"form": form, "date": dates[i] if i < len(dates) else None, "url": url}
    return None


def filings_browse_url(cik: int) -> str:
    """Human-usable fallback pointer to a filer's EDGAR annual filings."""
    return (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
            "&type=40-F&dateb=&owner=include&count=40")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _name_key(name: str) -> str:
    return "_".join(sorted(name_tokens(name)))


def _similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    if a <= b or b <= a:
        return True
    inter = a & b
    return bool(inter) and len(inter) / len(a | b) >= 0.34


def _load_cached_or_fetch(user_agent: str, cache_path: Path, max_age_days: float) -> dict:
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < max_age_days * 86400:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    import httpx

    resp = httpx.get(TICKERS_URL, headers={"User-Agent": user_agent}, timeout=60,
                     follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    try:
        cache_path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return data
