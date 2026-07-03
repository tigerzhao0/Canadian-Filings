"""CSE (Canadian Securities Exchange / XCNQ) filings source — labeled fallback.

CSE-listed micro-caps often have no usable IR website, but the CSE publishes each
issuer's filings through its own public API and mirrors the PDFs on its own
servers (sedar-filings-backup.thecse.com). Those documents ORIGINATE from SEDAR,
so this is used only as a clearly-labeled fallback (discovery_method='cse_filings')
AFTER the first-party cascade fails — never as a first-party source, and we never
touch sedarplus.ca.

Flow:
    GET https://thecse.com/api/webapi/company/?symbol=<SYM>   -> metadata.sedar_filings URL
    GET <that JSON>                                           -> {"list": [ {url, document_category,
                                                                 document_language, public_date, ...} ]}
    pick newest English ANNUAL_FINANCIAL_STATEMENTS (then Financial Statements, then ANNUAL_MDA).
"""
from __future__ import annotations

COMPANY_API = "https://thecse.com/api/webapi/company/?symbol={symbol}"

# Category preference, best first. Values seen in the CSE `list` feed.
_ANNUAL_CATEGORIES = (
    ("ANNUAL_FINANCIAL_STATEMENTS",),
    ("Financial Statements",),
    ("ANNUAL_MDA",),
)


async def fetch_annual_statements(client, symbol: str, user_agent: str,
                                  timeout: float = 20, years: int = 5) -> list[dict]:
    """Return {'url','year','category','date'} candidates for the CSE annual
    financial statements, most-recent-first, one per DISTINCT fiscal year, up
    to `years` years back (may be empty). Strictly ANNUAL — interim/quarterly
    filings are excluded (see _pick_annuals). Tries the ticker and its base
    (sans suffix)."""
    for sym in _symbol_variants(symbol):
        filings_url = await _sedar_filings_url(client, sym, user_agent, timeout)
        if not filings_url:
            continue
        picked = await _pick_annuals(client, filings_url, user_agent, timeout, years=years)
        if picked:
            return picked
    return []


def _symbol_variants(symbol: str) -> list[str]:
    s = (symbol or "").strip().upper()
    out = [s]
    base = s.split(".")[0]
    if base and base != s:
        out.append(base)
    return out


async def _sedar_filings_url(client, symbol, user_agent, timeout) -> str | None:
    try:
        resp = await client.get(
            COMPANY_API.format(symbol=symbol),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout, follow_redirects=True,
        )
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return None
    data = resp.json()
    # The API ships both "metadata" and a typo'd "metatdata"; try both.
    meta = data.get("metadata") or data.get("metatdata") or {}
    return meta.get("sedar_filings")


# Markers that a filing is interim/quarterly rather than annual — belt-and-
# braces on top of the category filter, since some issuers mis-tag and the
# caller strictly wants annual documents only.
_INTERIM_MARKERS = ("interim", "q1", "q2", "q3", "quarter", "three month",
                    "six month", "nine month", "third quarter", "first quarter",
                    "second quarter")


def _looks_interim(item: dict) -> bool:
    blob = " ".join(str(item.get(k, "")) for k in
                    ("document_category", "document_type", "url", "title")).lower()
    return any(m in blob for m in _INTERIM_MARKERS)


async def _pick_annuals(client, filings_url, user_agent, timeout, years: int = 5) -> list[dict]:
    """Return up to `years` filings, one per DISTINCT fiscal year (most-recent
    first), from the first category tier that has any hits
    (ANNUAL_FINANCIAL_STATEMENTS preferred, then Financial Statements, then
    ANNUAL_MDA). Interim/quarterly filings are dropped even if they slip into a
    tier, so the result is strictly annual."""
    try:
        resp = await client.get(filings_url, headers={"User-Agent": user_agent},
                                timeout=timeout, follow_redirects=True)
    except Exception:  # noqa: BLE001
        return []
    if resp.status_code != 200:
        return []
    items = resp.json().get("list") or []

    for categories in _ANNUAL_CATEGORIES:
        cands = [it for it in items
                 if it.get("document_category") in categories and it.get("url")
                 and not _looks_interim(it)]
        if not cands:
            continue
        english = [it for it in cands
                   if (it.get("document_language") or "").lower().startswith("en")]
        pool = english or cands
        pool.sort(key=lambda x: x.get("public_date", ""), reverse=True)
        out: list[dict] = []
        seen_years: set[int] = set()
        for it in pool:
            date = it.get("public_date", "") or ""
            year = int(date[:4]) if date[:4].isdigit() else None
            if year is None or year in seen_years:
                continue
            seen_years.add(year)
            out.append({"url": it["url"], "year": year,
                       "category": it.get("document_category"), "date": date})
            if len(out) >= years:
                break
        return out
    return []
