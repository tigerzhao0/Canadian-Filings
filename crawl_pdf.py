"""Stage 3: crawl an IR homepage for the most recent annual-report PDF.

From the homepage we follow up to `max_hops` links toward pages that look like
"annual report / financial statements / reports & filings / investors", collect
links that point at PDFs, and score each PDF candidate by how strongly its link
text / filename says "annual report" plus the most recent year mentioned.

Respects robots.txt and applies a per-domain minimum delay. Static HTML only
(httpx + BeautifulSoup); JS-rendered sites that yield nothing here are escalated
to the Tier-2 Playwright pass (render.py), which reuses the scoring helpers here.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from discover_ir import _registrable_domain

CURRENT_YEAR = datetime.now().year

# Link text / URL hints that a link leads TOWARD a reports page (navigation),
# weighted: reaching an "annual report" nav link matters more than "investor".
NAV_HINT_WEIGHTS: dict[str, float] = {
    "annual report": 5.0, "annual-report": 5.0, "annualreport": 5.0,
    "annual & financial": 5.0, "annual and financial": 5.0,
    "financial report": 4.0, "financial-report": 4.0, "financial statement": 4.0,
    "financial-statement": 4.0, "reports & filings": 4.0, "reports and filings": 4.0,
    "annual filing": 4.0, "annual information": 4.0,
    "financials": 3.0, "financial information": 3.0, "reports": 3.0,
    "filings": 3.0, "sec filings": 3.0, "sedar": 2.0, "regulatory": 2.0,
    "investor": 2.0, "investors": 2.0, "investor relations": 2.5,
    "shareholder": 2.0, "quarterly": 1.5, "results": 1.5, "documents": 1.5,
    "financial-reports": 4.0, "annual-reports": 5.0, "annual reports": 5.0,
}

# Strong hints that a specific link IS the annual report itself. Kept specific:
# bare "annual" is intentionally excluded so "annual meeting minutes" / "annual
# general meeting" do NOT register as the annual report. Filenames like
# "ar_2025.pdf" are recognised via _AR_FILENAME_RE below.
AR_HINTS = ("annual report", "annual-report", "annualreport", "10-k", "10k",
            "form 10-k", "form10k")
FINANCIAL_HINTS = ("financial statement", "financial-statements", "financials",
                   "consolidated financial", "mdna", "md&a", "-fs-", "_fs_",
                   "-fs.", "annual information form", "aif")
_AR_FILENAME_RE = re.compile(r"\bar[_\-. ]?20\d\d\b")

# Document types that are decisively NOT the annual report.
NEGATIVE_HINTS = ("proxy", "circular", "information circular", "presentation",
                  "transcript", "factsheet", "fact sheet", "webcast",
                  "corporate governance", "annual meeting", "annual general",
                  "meeting minutes", "minutes", "agm", "notice of meeting",
                  "no material change", "nomrd", "material change", "news release",
                  "press release", "prospectus", "q1 ", "q2 ", "q3 ", "q1-", "q2-",
                  "q3-", "interim", "first quarter", "second quarter", "third quarter")

# Minimum score for a PDF to count as a confident 'found'. Below this (but with
# a report hint) it becomes needs_review as a weak match.
MIN_FOUND_SCORE = 2.0

_YEAR_RE = re.compile(r"(19|20)\d{2}")


@dataclass
class PDFCandidate:
    url: str
    link_text: str
    score: float
    year: int | None
    has_report_hint: bool  # matched an annual-report / financial-statement keyword


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #
class DomainThrottle:
    """Per-domain minimum delay + shared global concurrency semaphore."""

    def __init__(self, max_concurrency: int, per_domain_delay: float):
        self._sem = asyncio.Semaphore(max_concurrency)
        self._delay = per_domain_delay
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def acquire(self, host: str):
        await self._sem.acquire()
        async with self._lock_for(host):
            wait = self._delay - (time.monotonic() - self._last.get(host, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()

    def release(self):
        self._sem.release()


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #
class Crawler:
    def __init__(self, client, throttle: DomainThrottle, cfg: dict):
        self._client = client
        self._throttle = throttle
        self._user_agent = cfg.get("user_agent", "CanadianARFinder/1.0")
        self._respect_robots = cfg.get("respect_robots", True)
        self._max_hops = int(cfg.get("max_hops", 3))
        self._max_pages = int(cfg.get("max_pages", 25))
        self._timeout = float(cfg.get("timeout_seconds", 20))
        self._robots: dict[str, RobotFileParser | None] = {}

    async def find_annual_report_pdf(self, homepage_url: str) -> list[PDFCandidate]:
        """BFS from the homepage up to max_hops, collecting PDF candidates.

        Follows links within the same registrable domain (so company.com can hop
        to ir.company.com / investors.company.com), prioritising report-ish nav
        links. Returns a ranked list of candidates (most-recent-first among
        confident ones; see rank_pdfs) so the caller can retry validation
        against #2, #3, ... if the top pick turns out dead/blocked — a single
        bad link should not sink an otherwise-successful crawl.
        """
        start_reg = _registrable_domain(urlparse(homepage_url).netloc)
        visited: set[str] = set()
        pdf_candidates: list[PDFCandidate] = []
        # priority queue as list of (neg_priority, url, depth); higher nav score first
        frontier: list[tuple[float, str, int]] = [(0.0, homepage_url, 0)]
        pages = 0

        while frontier and pages < self._max_pages:
            frontier.sort(key=lambda x: x[0])  # most-promising first (neg priority)
            _, url, depth = frontier.pop(0)
            if url in visited:
                continue
            visited.add(url)
            html = await self._fetch_html(url)
            if html is None:
                continue
            pages += 1

            links = extract_links(html, base_url=url)
            for href, text in links:
                if looks_like_pdf(href, text):
                    pdf_candidates.append(score_pdf(href, text))

            if depth < self._max_hops:
                for priority, href, _text in rank_nav_links(links, start_reg):
                    if href not in visited:
                        frontier.append((-priority, href, depth + 1))

            # Stop early once we have a clearly-annual-report PDF (after ≥1 hop).
            if depth >= 1 and any(c.score >= 6 for c in pdf_candidates):
                break

        return rank_pdfs(pdf_candidates)

    async def _fetch_html(self, url: str) -> str | None:
        if self._respect_robots and not await self._robots_allows(url):
            return None
        host = urlparse(url).netloc
        await self._throttle.acquire(host)
        try:
            resp = await self._client.get(
                url,
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
                follow_redirects=True,
            )
        except Exception:
            return None
        finally:
            self._throttle.release()
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200 or "html" not in ctype.lower():
            return None
        return resp.text

    async def _robots_allows(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            self._robots[base] = await self._load_robots(base)
        rp = self._robots[base]
        if rp is None:
            return True  # no robots.txt -> allowed
        return rp.can_fetch(self._user_agent, url)

    async def _load_robots(self, base: str) -> RobotFileParser | None:
        host = urlparse(base).netloc
        await self._throttle.acquire(host)
        try:
            resp = await self._client.get(
                urljoin(base, "/robots.txt"),
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
                follow_redirects=True,
            )
        except Exception:
            return None
        finally:
            self._throttle.release()
        if resp.status_code != 200:
            return None
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp


# --------------------------------------------------------------------------- #
# Reusable helpers (also used by render.py Tier-2)
# --------------------------------------------------------------------------- #
def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if not href.lower().startswith(("http://", "https://")):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        if not text:  # icon/image links: fall back to aria-label / title
            text = (a.get("aria-label") or a.get("title") or "").strip()
        out.append((href, text))
    return out


def looks_like_pdf(href: str, text: str) -> bool:
    path = urlparse(href).path.lower()
    if path.endswith(".pdf"):
        return True
    return ".pdf" in href.lower()


def rank_nav_links(links: list[tuple[str, str]], start_reg_domain: str) -> list[tuple[float, str, str]]:
    """Return (priority, href, text) for same-registrable-domain links whose
    text/url hints at reports pages, best first. Allows hops to IR subdomains."""
    scored: list[tuple[float, str, str]] = []
    for href, text in links:
        if _registrable_domain(urlparse(href).netloc) != start_reg_domain:
            continue
        blob = (text + " " + href).lower()
        weight = 0.0
        for hint, w in NAV_HINT_WEIGHTS.items():
            if hint in blob:
                weight = max(weight, w)
        if weight > 0:
            scored.append((weight, href, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:12]  # cap fan-out per page to stay polite


def score_pdf(href: str, text: str) -> PDFCandidate:
    blob = (text + " " + urlparse(href).path).lower()
    score = 0.0
    has_ar = any(h in blob for h in AR_HINTS) or bool(_AR_FILENAME_RE.search(blob))
    has_fin = any(h in blob for h in FINANCIAL_HINTS)
    if has_ar:
        score += 4.0
    if has_fin:
        score += 2.0
    year = _latest_year(blob)
    if year:
        # Recency is a bonus ON TOP of a real report hint — a recent date alone
        # (e.g. a press release dated this year) must not qualify a PDF.
        score += max(0, 3 - (CURRENT_YEAR - year))
    # A negative hint (interim, proxy, circular, meeting minutes, ...) is a hard
    # veto, not just a score nudge — an interim report can legitimately carry
    # "financial statement" + a fresh year (e.g. "Q1-F2026-Interim-Financial-
    # Statements.pdf") and would otherwise net a positive score that clears
    # MIN_FOUND_SCORE despite obviously NOT being the annual report.
    is_negative = any(bad in blob for bad in NEGATIVE_HINTS)
    if is_negative:
        score -= 8.0
    return PDFCandidate(url=href, link_text=text, score=score, year=year,
                        has_report_hint=(has_ar or has_fin) and not is_negative)


def _latest_year(blob: str) -> int | None:
    years = [int(m.group()) for m in _YEAR_RE.finditer(blob)]
    years = [y for y in years if 1995 <= y <= CURRENT_YEAR + 1]
    return max(years) if years else None


def rank_pdfs(candidates: list[PDFCandidate], limit: int = 6) -> list[PDFCandidate]:
    """Rank PDF candidates most-likely-correct first.

    Confident (real annual-report/financial-statement) candidates are sorted by
    MOST RECENT YEAR FIRST, tie-broken by score — once something is confirmed to
    actually be an annual report, the newest one wins outright, regardless of
    which one happens to score marginally higher on keyword match. Non-confident
    candidates are appended after (sorted by score) purely as a last-resort
    needs_review pointer if nothing confident is found."""
    confident = [c for c in candidates if is_confident(c)]
    weak = [c for c in candidates if not is_confident(c)]
    confident.sort(key=lambda c: (c.year or 0, c.score), reverse=True)
    weak.sort(key=lambda c: (c.score, c.year or 0), reverse=True)
    return (confident + weak)[:limit]


def is_confident(pdf: PDFCandidate | None) -> bool:
    """Whether a PDF candidate is a confident annual-report match ('found')."""
    return pdf is not None and pdf.has_report_hint and pdf.score >= MIN_FOUND_SCORE
