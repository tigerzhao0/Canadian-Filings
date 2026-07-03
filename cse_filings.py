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


async def fetch_annual_statements(client, symbol: str, user_agent: str, timeout: float = 20) -> list[dict]:
    """Return {'url','year','category','date'} candidates for the latest annual
    financial statement on the CSE, most-recent-first (may be empty). Returning
    more than one lets the caller retry if the top URL turns out dead/blocked.
    Tries the ticker and its base (sans suffix)."""
    for sym in _symbol_variants(symbol):
        filings_url = await _sedar_filings_url(client, sym, user_agent, timeout)
        if not filings_url:
            continue
        picked = await _pick_annuals(client, filings_url, user_agent, timeout)
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


async def _pick_annuals(client, filings_url, user_agent, timeout, limit_per_tier: int = 3) -> list[dict]:
    """Return up to `limit_per_tier` most-recent filings from the first category
    tier that has any hits (ANNUAL_FINANCIAL_STATEMENTS preferred, then Financial
    Statements, then ANNUAL_MDA) — tier preference still wins, but within a tier
    we keep a few so a dead/blocked top URL doesn't sink the company."""
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
                 if it.get("document_category") in categories and it.get("url")]
        if not cands:
            continue
        english = [it for it in cands
                   if (it.get("document_language") or "").lower().startswith("en")]
        pool = english or cands
        pool.sort(key=lambda x: x.get("public_date", ""), reverse=True)
        out = []
        for it in pool[:limit_per_tier]:
            date = it.get("public_date", "") or ""
            year = int(date[:4]) if date[:4].isdigit() else None
            out.append({"url": it["url"], "year": year,
                       "category": it.get("document_category"), "date": date})
        return out
    return []
