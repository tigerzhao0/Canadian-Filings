"""TMX/QuoteMedia structured financials source (app.quotemedia.com).

TMX Money's "Financials & Filings" page (money.tmx.com) renders its Income
Statement / Balance Sheet / Cash Flow tables from QuoteMedia's public
"datatool" widget API -- a plain JSON GET, not a PDF and not something that
needs a rendered DOM. Reverse-engineered from a live browser session
(money.tmx.com -> DevTools -> Network -> search response bodies for a known
figure, e.g. a Total Revenue value, to find the real request among a page's
worth of ad/analytics noise).

Auth: requires a `datatool-token` header plus Origin/Referer set to
money.tmx.com -- all three are enforced server-side (confirmed 403 without
any one of them). IMPORTANT: the token is SHORT-LIVED, not a static per-site
credential -- confirmed by testing the exact same token+request again later
and getting a 403 where it previously returned 200. It IS reusable across
different symbols within one mint (SU, RY, SHOP all worked off one token),
just not indefinitely over time. Use `mint_token()` below to get a fresh one
via a real (headless) page load rather than hardcoding one.

The token is minted by JS running inside a cross-origin iframe embedded in
the TMX page (its src is a QuoteMedia URL carrying auth data in the query
string) -- that's why a plain requests-library approach can't derive it
without JS execution, and why browser-extension-based network loggers can
miss it too (iframe-scoped requests aren't always visible to them); Playwright
sees it fine via `page.on("request")`.

Never touches sedarplus.ca -- this is QuoteMedia/TMX's own data API.
"""
from __future__ import annotations

ENDPOINT = "https://app.quotemedia.com/datatool/getFinancialsEnhancedBySymbol.json"
MINT_URL = "https://money.tmx.com/en/quote/SU/financials-filings"  # any valid
                                                                    # symbol works
DEFAULT_WEBMASTER_ID = "101020"
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class TokenExpired(Exception):
    """Raised by fetch_annual_financials(raise_on_expired=True) so callers can
    distinguish 'token needs refreshing' from 'this symbol has no data'."""


async def mint_token(timeout_ms: int = 45000, poll_interval_ms: int = 300,
                     max_polls: int = 30) -> str | None:
    """Launch a headless Chromium page, load a TMX Money financials page, and
    capture the fresh `datatool-token` the page's own JS mints for its
    QuoteMedia widget request. Returns None if Playwright isn't installed, the
    page fails to load, or no matching request is seen within the timeout --
    callers should treat None as 'financials fetch unavailable this run', not
    crash the batch.
    """
    try:
        from playwright.async_api import async_playwright
    except Exception:  # noqa: BLE001 - Playwright not installed
        return None

    captured: dict[str, str] = {}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                async def on_request(request):
                    if "token" in captured:
                        return
                    if "getFinancialsEnhancedBySymbol" in request.url:
                        token = request.headers.get("datatool-token")
                        if token:
                            captured["token"] = token

                page.on("request", on_request)
                await page.goto(MINT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                for _ in range(max_polls):
                    if "token" in captured:
                        break
                    await page.wait_for_timeout(poll_interval_ms)
            finally:
                await browser.close()
    except Exception:  # noqa: BLE001 - never let a browser hiccup crash the caller
        return None
    return captured.get("token")


async def fetch_annual_financials(client, symbol: str, *, token: str,
                                  user_agent: str = DEFAULT_USER_AGENT,
                                  timeout: float = 20.0, num_years: int = 20,
                                  raise_on_expired: bool = False) -> list[dict]:
    """Fetch up to `num_years` of annual Income Statement / Balance Sheet /
    Cash Flow data for a TSX/TSXV `symbol`. `token` must be a freshly minted
    one (see `mint_token()`) -- there is no working default anymore, the
    token goes stale.

    NOTE on depth: QuoteMedia caps this endpoint at ~14 annual reports
    regardless of how high `num_years` goes (empirically deep filers like RY /
    BMO / ENB return 2025..2012 = 14 years for numberOfReports of 15, 30, or
    50 alike -- a rolling retention window, not a per-symbol limit). Asking for
    more than the cap is harmless: the API just returns the max available with
    no error. Default is 20 so we always pull the full window the API offers
    (and would automatically capture more if QuoteMedia ever extends it).

    Returns a list of yearly reports, most-recent-first, each shaped:
        {"year": int, "period_end": str | None, "currency": str | None,
         "income_statement": dict, "balance_sheet": dict, "cash_flow": dict}
    Field names inside each statement dict match QuoteMedia's own labels
    (e.g. "TotalRevenue", "NetIncome", "TotalAssets", "OperatingCashFlow").

    Returns an empty list on most failures (bad/delisted symbol, network
    error, unexpected response shape) -- callers should treat that as
    "financials unavailable for this company", not a fatal error. If
    `raise_on_expired` is True, a 403 response raises TokenExpired instead of
    returning [] , so a caller doing batch work can catch it, re-mint, and
    retry rather than silently recording every remaining company as failed.
    """
    params = {
        "symbol": symbol,
        "pathName": f"/en/quote/{symbol}/financials-filings",
        "typeList": "IncomeStatement,BalanceSheet,CashFlow",
        "reportType": "A",            # Annual (vs "Q" for quarterly)
        "reportOrder": "D",           # Descending -> most recent year first
        "numberOfReports": str(num_years),
        "groupReports": str(num_years),
        "currency": "true",
        "defaultUnit": "Millions",
        "latestfiscaldate": "true",
        "lowHigh": "false",
        "showLogo": "false",
        "lang": "en",
        "qmodTool": "Financials",
        "webmasterId": DEFAULT_WEBMASTER_ID,
        "tradeUrl": "",
    }
    headers = {
        "accept": "*/*",
        "datatool-token": token,
        "origin": "https://money.tmx.com",
        "referer": "https://money.tmx.com/",
        "user-agent": user_agent,
    }
    try:
        resp = await client.get(ENDPOINT, params=params, headers=headers, timeout=timeout)
    except Exception:  # noqa: BLE001 - network hiccup -> treat as unavailable
        return []
    if resp.status_code == 403 and raise_on_expired:
        raise TokenExpired(f"datatool-token rejected (403) fetching {symbol}")
    if resp.status_code != 200:
        return []
    try:
        reports = resp.json()["results"]["Company"]["Report"]
    except Exception:  # noqa: BLE001 - unexpected shape -> treat as unavailable
        return []

    out: list[dict] = []
    for r in reports:
        if not isinstance(r, dict):
            continue
        out.append({
            "year": r.get("reportYear"),
            "period_end": r.get("periodEndDate"),
            "currency": r.get("currency"),
            "income_statement": r.get("IncomeStatement") or {},
            "balance_sheet": r.get("BalanceSheet") or {},
            "cash_flow": r.get("CashFlow") or {},
        })
    return out


if __name__ == "__main__":
    import asyncio
    import json
    import sys

    async def _main():
        symbol = sys.argv[1] if len(sys.argv) > 1 else "SU"
        import httpx
        print("Minting a fresh datatool-token (headless page load)...")
        token = await mint_token()
        if not token:
            print("Could not mint a token -- is Playwright installed? "
                 "(pip install playwright && playwright install chromium)")
            return
        async with httpx.AsyncClient() as client:
            reports = await fetch_annual_financials(client, symbol, token=token)
        if not reports:
            print(f"No financials returned for {symbol} (bad symbol, expired "
                 "token, or network error).")
            return
        for r in reports:
            print(f"\n=== {symbol} FY{r['year']} (period end {r['period_end']}, "
                 f"{r['currency']}) ===")
            for section in ("income_statement", "balance_sheet", "cash_flow"):
                fields = r[section]
                print(f"  {section}: {len(fields)} fields")
        print(f"\nFull JSON for FY{reports[0]['year']} income_statement:")
        print(json.dumps(reports[0]["income_statement"], indent=2))

    asyncio.run(_main())
