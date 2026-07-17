"""TSX / TSX-V filings source (money.tmx.com) — labeled fallback for the tail.

TMX Money's own Filings tab is rendered from a plain JSON GraphQL endpoint
(`app-money.tmx.com/graphql`, operation `getCompanyFilings`) -- discovered by
capturing the page's own network traffic. It needs NO auth token (unlike the
sibling `datatool` financials API in tmx_financials.py) -- just an
Origin/Referer pair identifying the request as coming from money.tmx.com.
Given a symbol + date range it returns each filing's date/description/name
and a direct `urlToPdf` (the same portable app.quotemedia.com/data/
downloadFiling?... link the old DOM-scraping approach produced), so no
browser is needed at all.

A single broad multi-year query can get its result window flooded by a
high-filing-volume issuer's routine disclosures (shelf prospectuses, exempt
distributions) before reaching back far enough to surface the annual
statement -- observed on RY. `_year_window` detects this (result count hits
the request `limit`) and re-queries that year as four quarters instead,
which self-heals for any similarly high-volume issuer, not just RY.

As with the CSE source, these documents originate from SEDAR (TMX/QuoteMedia-
hosted); we never touch sedarplus.ca. Used only after first-party discovery
fails, and labeled discovery_method='tmx_filings_api'.
"""
from __future__ import annotations

from urllib.parse import urlparse, parse_qs

import asyncio

GRAPHQL_URL = "https://app-money.tmx.com/graphql"
TMX_REFERER = "https://money.tmx.com/en/quote/{symbol}/financials-filings"

_QUERY = (
    "query getCompanyFilings($symbol: String!, $fromDate: String, $toDate: String, $limit: Int) {\n"
    "  filings: getCompanyFilings(\n"
    "    symbol: $symbol\n"
    "    fromDate: $fromDate\n"
    "    toDate: $toDate\n"
    "    limit: $limit\n"
    "  ) {\n"
    "    size\n"
    "    filingDate\n"
    "    description\n"
    "    name\n"
    "    urlToPdf\n"
    "    __typename\n"
    "  }\n"
    "}"
)

# Statuses worth one retry (transient) -- same convention as pdf_pipeline.py's
# _fetch_pdf: a brief blip is worth one retry, a permanent error is not.
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}

_QUARTERS = (("01-01", "03-31"), ("04-01", "06-30"), ("07-01", "09-30"), ("10-01", "12-31"))


async def annual_statements_via_api(client, symbol: str, user_agent: str, timeout: float,
                                    years: int, current_year: int) -> list[dict]:
    """Collect downloadFiling links across `years` one-year windows (most
    recent year first), then classify with the EXISTING pick_annuals /
    pick_secondary -- unchanged from the old DOM-scraping approach, since
    urlToPdf is the identical link shape those functions already parse."""
    links: list[str] = []
    for year in range(current_year, current_year - years, -1):
        links.extend(await _year_window(client, symbol, year, user_agent, timeout))
    annual = pick_annuals(links)
    return annual if annual else pick_secondary(links)


async def _year_window(client, symbol: str, year: int, ua: str, timeout: float,
                       limit: int = 250) -> list[str]:
    """Jan1-Dec31 for one year; if the result hits the row cap (RY-style
    truncation from a high filing volume), re-query as four quarters instead
    so a busy issuer's annual statement isn't pushed out of the window."""
    urls = await _graphql_request(client, symbol, f"{year}-01-01", f"{year}-12-31",
                                  limit, ua, timeout)
    if len(urls) >= limit:
        urls = []
        for start, end in _QUARTERS:
            urls.extend(await _graphql_request(
                client, symbol, f"{year}-{start}", f"{year}-{end}", limit, ua, timeout))
    return urls


async def _graphql_request(client, symbol: str, from_date: str, to_date: str, limit: int,
                           ua: str, timeout: float) -> list[str]:
    """POST getCompanyFilings and return its urlToPdf links. Retries ONCE on
    a transient failure (timeout/429/5xx), matching pdf_pipeline.py's
    _fetch_pdf posture; fails soft ([]) on anything else -- a bad symbol or
    an unexpected response shape must not kill the batch."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://money.tmx.com",
        "Referer": TMX_REFERER.format(symbol=symbol),
        "User-Agent": ua,
    }
    payload = {
        "operationName": "getCompanyFilings",
        "variables": {"symbol": symbol, "fromDate": from_date, "toDate": to_date, "limit": limit},
        "query": _QUERY,
    }
    for attempt in range(2):
        try:
            resp = await client.post(GRAPHQL_URL, json=payload, headers=headers, timeout=timeout)
        except Exception:  # noqa: BLE001 - network hiccup -> treat as unavailable
            resp = None
        else:
            if resp.status_code == 200:
                break
            if resp.status_code not in _TRANSIENT_STATUS:
                return []  # permanent -> don't retry
        if attempt == 0:
            await asyncio.sleep(1.5)  # brief backoff before the single retry
    else:
        return []
    if resp is None or resp.status_code != 200:
        return []
    try:
        filings = resp.json()["data"]["filings"] or []
    except Exception:  # noqa: BLE001 - unexpected shape -> treat as unavailable
        return []
    return [f["urlToPdf"] for f in filings if isinstance(f, dict) and f.get("urlToPdf")]


def _dedupe_years(cands: list[dict], limit: int) -> list[dict]:
    """One candidate per distinct fiscal year, most-recent first, up to `limit`.
    Undated (year=None) candidates are dropped (they can't key a per-year row)."""
    by_year: dict[int, dict] = {}
    for d in sorted(cands, key=lambda x: x.get("date", ""), reverse=True):
        y = d.get("year")
        if y is None or y in by_year:
            continue
        by_year[y] = d
    return [by_year[y] for y in sorted(by_year, reverse=True)[:limit]]


def _parse(url: str) -> tuple[str, str]:
    q = parse_qs(urlparse(url).query)
    desc = (q.get("formDescription", [""])[0] or "").lower()
    date = q.get("dateFiled", [""])[0] or ""
    return desc, date


def pick_annuals(links: list[str]) -> list[dict]:
    """All strict annual-report/financial-statement matches from downloadFiling
    links (excludes interim), most-recent-first. Year-dedup / capping is the
    caller's job (see _dedupe_years) so it can span multiple year-windows."""
    cands: list[tuple[str, str, str]] = []
    for url in links:
        desc, date = _parse(url)
        if "interim" in desc:
            continue
        if ("annual" in desc and "financial statement" in desc) or "annual report" in desc:
            cands.append((date, url, desc))
    cands.sort(reverse=True)  # latest dateFiled first
    return [_to_dict(date, url, desc) for date, url, desc in cands]


def pick_secondary(links: list[str]) -> list[dict]:
    """Broader fallback: unlabelled 'audited financial statements' for issuers
    that never file a filing matching pick_annuals' strict criteria. NOTE: the
    Annual Information Form branch was REMOVED -- an AIF contains no primary
    financial statements to extract, so returning it just yields empty output;
    better to fall through to another source that may find real statements."""
    out = []
    for url in links:
        desc, date = _parse(url)
        if "interim" in desc:
            continue
        if "annual information form" in desc or "information form" in desc:
            continue  # not statements
        if "audited" in desc and "financial statement" in desc:
            out.append(_to_dict(date, url, desc))
    return out


def _to_dict(date: str, url: str, desc: str) -> dict:
    year = int(date[:4]) if date[:4].isdigit() else None
    return {"url": url, "date": date, "year": year, "description": desc}
