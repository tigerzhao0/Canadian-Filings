"""TSX / TSX-V filings source (money.tmx.com) — labeled fallback for the tail.

TMX Money surfaces each issuer's SEDAR filings through a JavaScript "Filings"
widget (QuoteMedia): a month carousel you page backwards through, with a
"Load More" button per month. There is no clean bulk API (it's gated), so we
drive the widget with the existing headless browser: open the Filings tab, walk
back month-by-month until the most recent "Audited annual financial statements"
appears, and take its download link.

The resulting URL (app.quotemedia.com/data/downloadFiling?...) is a portable,
standalone PDF (verified: application/pdf, works without a Referer). As with the
CSE source, these documents originate from SEDAR (TMX/QuoteMedia-hosted); we
never touch sedarplus.ca. Used only after first-party discovery fails, and
labeled discovery_method='tmx_filings'.
"""
from __future__ import annotations

from urllib.parse import urlparse, parse_qs

TMX_URL = "https://money.tmx.com/en/quote/{symbol}/financials-filings"

# Robust selectors (class names carry build hashes -> match by prefix).
_FILINGS_TAB_JS = (
    "()=>{const b=[...document.querySelectorAll('button')]"
    ".filter(x=>((x.className&&x.className.baseVal!==undefined?x.className.baseVal:x.className)||'')"
    ".includes('FinancialsFilingsTabView')&&x.innerText.trim()==='Filings');b[0]&&b[0].click();}"
)
_PREV_ARROW = 'button[class*="CarouselMonthPicker__ArrowButton"]'
_MONTH_BTN = 'button[class*="CarouselMonthPicker__Month"]'


async def annual_statement_from_page(page, symbol: str, max_months: int = 14,
                                     timeout_ms: int = 45000) -> dict | None:
    """Drive the TMX Filings widget to the most recent annual financial statement.
    Returns {'url','date','year','description'} or None. `page` is a Playwright
    Page from the render browser."""
    await page.goto(TMX_URL.format(symbol=symbol), wait_until="domcontentloaded",
                    timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    await page.evaluate(_FILINGS_TAB_JS)
    await page.wait_for_timeout(2500)

    # Walk newest -> oldest; the first month containing an annual FS is the latest.
    for _ in range(max_months):
        await _load_all(page)
        links = await page.eval_on_selector_all(
            "a[href*=downloadFiling]", "els=>els.map(e=>e.href)")
        cand = pick_annual(links)
        if cand:
            return cand
        if not await _prev_month(page):
            break
    return None


async def _load_all(page, max_clicks: int = 8):
    for _ in range(max_clicks):
        try:
            lm = page.locator('button:has-text("Load More")').first
            if await lm.count() > 0 and await lm.is_visible():
                await lm.click(timeout=1500)
                await page.wait_for_timeout(500)
            else:
                return
        except Exception:
            return


async def _prev_month(page) -> bool:
    try:
        await page.locator(_PREV_ARROW).first.click(timeout=2000)
        await page.wait_for_timeout(500)
        await page.locator(_MONTH_BTN).first.click(timeout=2000)
        await page.wait_for_timeout(1100)
        return True
    except Exception:
        return False


def pick_annual(links: list[str]) -> dict | None:
    """Pick the most recent annual financial statement from downloadFiling links,
    parsing the formDescription/dateFiled query params. Excludes interim."""
    cands: list[tuple[str, str, str]] = []
    for url in links:
        q = parse_qs(urlparse(url).query)
        desc = (q.get("formDescription", [""])[0] or "").lower()
        date = q.get("dateFiled", [""])[0] or ""
        if "interim" in desc:
            continue
        if ("annual" in desc and "financial statement" in desc) or "annual report" in desc:
            cands.append((date, url, desc))
    if not cands:
        return None
    cands.sort(reverse=True)  # latest dateFiled first
    date, url, desc = cands[0]
    year = int(date[:4]) if date[:4].isdigit() else None
    return {"url": url, "date": date, "year": year, "description": desc}
