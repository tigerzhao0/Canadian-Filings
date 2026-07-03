"""Tier-2 pass: render JS-heavy IR sites with a headless browser (Playwright).

Only runs for companies Tier-1 couldn't resolve. Reuses the exact annual-report
scoring from crawl_pdf, but on the *rendered* DOM, so IR portals that build their
"Financial Reports" link lists client-side (Q4, many corporate CMSs) become
reachable. Also captures PDFs opened via JS (application/pdf responses).

Playwright is optional. If it isn't installed, RenderUnavailable is raised once
and the pipeline skips Tier-2 with an install hint — Tier-1 still stands alone.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from crawl_pdf import (
    PDFCandidate, rank_pdfs, extract_links, looks_like_pdf, rank_nav_links, score_pdf,
)
from discover_ir import _registrable_domain


class RenderUnavailable(RuntimeError):
    """Playwright (or its browser) is not installed."""


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except Exception:
        return False
    return True


class Renderer:
    def __init__(self, cfg: dict):
        crawl = cfg.get("crawl", {})
        render = cfg.get("render", {})
        # A real browser -> use a realistic browser UA (many IR CDNs 403 bots).
        self._user_agent = crawl.get("browser_user_agent") or crawl.get(
            "user_agent", "CanadianARFinder/1.0")
        self._timeout = float(render.get("timeout_seconds", 30)) * 1000  # ms
        self._max_hops = int(render.get("max_hops", 2))
        self._max_pages = int(render.get("max_pages", 12))
        self._concurrency = int(render.get("concurrency", 3))
        self._tmx_concurrency = int(cfg.get("tmx", {}).get("concurrency", 2))
        self._pw = None
        self._browser = None
        self._sem: asyncio.Semaphore | None = None
        self._tmx_sem: asyncio.Semaphore | None = None

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # noqa: BLE001
            raise RenderUnavailable(
                "Playwright is not installed. Enable Tier-2 rendering with:\n"
                "    pip install playwright && playwright install chromium"
            ) from exc
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - browser binary missing
            await self._pw.stop()
            raise RenderUnavailable(
                "Playwright is installed but the Chromium browser isn't. Run:\n"
                "    playwright install chromium"
            ) from exc
        self._sem = asyncio.Semaphore(self._concurrency)
        self._tmx_sem = asyncio.Semaphore(self._tmx_concurrency)
        return self

    async def __aexit__(self, *exc):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def find_annual_report_pdf(self, homepage_url: str) -> list[PDFCandidate]:
        """Returns a ranked list (most-recent-first among confident candidates;
        see rank_pdfs) so the caller can retry #2/#3 if the top pick is dead."""
        assert self._sem is not None
        async with self._sem:
            try:
                return await self._crawl_rendered(homepage_url)
            except Exception:  # noqa: BLE001 - never let one site kill the pass
                return []

    async def tmx_annual_statements(self, symbol: str, max_months: int = 24) -> list[dict]:
        """Drive the TMX Money Filings widget to a company's latest annual
        financial statement(s) (returns a ranked list, may be empty, so the
        caller can retry the next candidate if the top one is dead/blocked)."""
        assert self._tmx_sem is not None
        from tmx_filings import annual_statements_from_page
        async with self._tmx_sem:
            context = await self._browser.new_context(user_agent=self._user_agent)
            page = await context.new_page()
            try:
                return await annual_statements_from_page(
                    page, symbol, max_months, int(self._timeout))
            except Exception:  # noqa: BLE001
                return []
            finally:
                await context.close()

    async def _crawl_rendered(self, homepage_url: str) -> list[PDFCandidate]:
        start_reg = _registrable_domain(urlparse(homepage_url).netloc)
        context = await self._browser.new_context(user_agent=self._user_agent)
        # Speed: skip images/media/fonts; we only need the DOM + document requests.
        await context.route(
            "**/*",
            lambda route: asyncio.ensure_future(_maybe_abort(route)),
        )
        captured_pdfs: list[str] = []
        context.on("response", lambda r: _note_pdf(r, captured_pdfs))

        page = await context.new_page()
        visited: set[str] = set()
        candidates: list[PDFCandidate] = []
        frontier: list[tuple[float, str, int]] = [(0.0, homepage_url, 0)]
        pages = 0
        try:
            while frontier and pages < self._max_pages:
                frontier.sort(key=lambda x: x[0])
                _, url, depth = frontier.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                html = await self._render(page, url)
                if html is None:
                    continue
                pages += 1

                links = extract_links(html, base_url=url)
                for href, text in links:
                    if looks_like_pdf(href, text):
                        candidates.append(score_pdf(href, text))
                for pdf_url in captured_pdfs:
                    candidates.append(score_pdf(pdf_url, ""))
                captured_pdfs.clear()

                if depth < self._max_hops:
                    for priority, href, _t in rank_nav_links(links, start_reg):
                        if href not in visited:
                            frontier.append((-priority, href, depth + 1))

                if depth >= 1 and any(c.score >= 6 for c in candidates):
                    break
        finally:
            await context.close()

        return rank_pdfs(candidates)

    async def _render(self, page, url: str) -> str | None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout)
            # Scroll to trigger lazy-loaded report lists, then let the network settle.
            try:
                for _ in range(3):
                    await page.mouse.wheel(0, 4000)
                    await page.wait_for_timeout(400)
                await page.wait_for_load_state("networkidle", timeout=7000)
            except Exception:
                pass
            return await page.content()
        except Exception:
            return None


async def _maybe_abort(route):
    try:
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


def _note_pdf(response, sink: list[str]):
    try:
        ctype = response.headers.get("content-type", "").lower()
        url = response.url
        if "application/pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
            sink.append(url)
    except Exception:
        pass
