"""Orchestrates the two-tier cascade with SQLite checkpointing.

Tier 1 (fast, no browser), over every to-do company:
    search -> pick IR homepage -> deep static crawl -> if that fails, a direct
    "<name> annual report filetype:pdf" search -> HEAD-validate -> store.

Tier 2 (heavy, Playwright), only over rows Tier 1 left un-found:
    render the JS-heavy IR site and re-run the same PDF heuristics.

SQLite is the source of truth and every company is upserted the moment it is
resolved, so a crash mid-run loses nothing, re-runs skip 'found' rows, and you
see Tier-1 results land before Tier-2 starts grinding.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from crawl_pdf import Crawler, DomainThrottle, PDFCandidate, is_confident
from discover_ir import choose_ir_homepage, load_blocklist, _registrable_domain
from ingest import Company
from pdf_search import direct_pdf_search
import sec_edgar
import cse_filings
from search_provider import SearchProvider, SearchRateLimited, SearchError
from validate import validate_pdf

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _migrate(self):
        """Add columns introduced after a DB was first created."""
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(filings)")}
        for col, ddl in (("sec_filer", "sec_filer INTEGER DEFAULT 0"),
                         ("sec_filing_url", "sec_filing_url TEXT")):
            if col not in have:
                self._conn.execute(f"ALTER TABLE filings ADD COLUMN {ddl}")

    def already_found(self, ticker: str) -> bool:
        cur = self._conn.execute("SELECT status FROM filings WHERE ticker = ?", (ticker,))
        row = cur.fetchone()
        return bool(row) and row[0] == "found"

    async def upsert(self, **fields) -> None:
        fields["last_checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{c}=excluded.{c}" for c in fields if c != "ticker")
        sql = (f"INSERT INTO filings ({cols}) VALUES ({placeholders}) "
               f"ON CONFLICT(ticker) DO UPDATE SET {updates}")
        async with self._lock:
            self._conn.execute(sql, tuple(fields.values()))
            self._conn.commit()

    def rows_needing_tier2(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT ticker, company_name, exchange, ir_homepage_url, discovery_method "
            "FROM filings WHERE status != 'found'"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def rows_for_sec_pass(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT ticker, company_name, exchange FROM filings "
            "WHERE status != 'found' AND COALESCE(sec_filer, 0) = 0"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def rows_for_cse_pass(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT ticker, company_name, exchange FROM filings "
            "WHERE status != 'found' AND UPPER(exchange) IN ('CSE', 'XCNQ')"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def summary(self) -> dict[str, int]:
        cur = self._conn.execute("SELECT status, COUNT(*) FROM filings GROUP BY status")
        counts = {"found": 0, "not_found": 0, "needs_review": 0}
        for status, n in cur.fetchall():
            counts[status] = n
        row = self._conn.execute(
            "SELECT COUNT(*) FROM filings WHERE sec_filer = 1").fetchone()
        counts["sec_filer"] = row[0] if row else 0
        row = self._conn.execute(
            "SELECT COUNT(*) FROM filings WHERE discovery_method LIKE 'cse_filings%'").fetchone()
        counts["cse_filings"] = row[0] if row else 0
        return counts

    def close(self):
        self._conn.close()


# --------------------------------------------------------------------------- #
# Shared validation
# --------------------------------------------------------------------------- #
def _method(base: str, verified: bool) -> str:
    """Tag the discovery method so unverified (bot-blocked) finds are visible."""
    return base if verified else f"{base}+unverified"


async def _first_validating(cands: list[PDFCandidate], client, ua, timeout, browser_ua=None, limit=3):
    """Pick the best confident candidate.

    Returns (candidate, verified) where verified is True for a confirmed PDF and
    False for a 'blocked' one (exists but the CDN forbids bots — accepted as an
    unverified find since these are trusted own-domain annual-report filenames).
    Returns (None, False) if nothing usable.
    """
    blocked: PDFCandidate | None = None
    for cand in cands[:limit]:
        if not is_confident(cand):
            continue
        state, _reason = await validate_pdf(client, cand.url, ua, timeout, browser_ua)
        if state == "ok":
            return cand, True
        if state == "blocked" and blocked is None:
            blocked = cand
    if blocked is not None:
        return blocked, False
    return None, False


# --------------------------------------------------------------------------- #
# Tier 1 — fast path
# --------------------------------------------------------------------------- #
async def _tier1(company, *, provider, crawler, client, store, blocklist, cfg):
    ticker = company.ticker
    ua = cfg["crawl"]["user_agent"]
    browser_ua = cfg["crawl"].get("browser_user_agent")
    timeout = float(cfg["crawl"]["timeout_seconds"])
    query = cfg["search"]["query_template"].format(name=company.legal_name)
    max_results = int(cfg["search"]["max_results"])
    base = dict(ticker=ticker, company_name=company.legal_name, exchange=company.exchange)

    # Stage 1: search for the IR homepage.
    try:
        results = await provider.search(query, max_results)
    except SearchRateLimited:
        await store.upsert(**base, status="needs_review", failure_reason="search_rate_limited")
        return
    except SearchError as exc:
        await store.upsert(**base, status="needs_review",
                           failure_reason=f"search_error: {str(exc)[:120]}")
        return

    # Stage 2: pick the IR homepage (may be None).
    candidate = choose_ir_homepage(results, company.legal_name, blocklist) if results else None
    homepage_url = candidate.url if candidate else None
    reg = candidate.domain if candidate else None
    method = (f"crawl:{candidate.platform}" if candidate and candidate.platform
              else "crawl") if candidate else None

    # Stage 3: deep static crawl of the homepage.
    crawl_pdf_cand = None
    if candidate:
        try:
            crawl_pdf_cand = await crawler.find_annual_report_pdf(candidate.url)
        except Exception:  # noqa: BLE001
            crawl_pdf_cand = None

    good, verified = await _first_validating(
        [crawl_pdf_cand] if crawl_pdf_cand else [], client, ua, timeout, browser_ua)
    if good:
        await store.upsert(**base, ir_homepage_url=homepage_url, pdf_url=good.url,
                           fiscal_year_guess=good.year,
                           discovery_method=_method(method, verified),
                           status="found", failure_reason=None if verified else "unverified_403")
        return

    # Stage 3b: direct PDF search fallback (one extra query).
    pdf_cands = await direct_pdf_search(provider, company, blocklist, reg, cfg)
    good, verified = await _first_validating(pdf_cands, client, ua, timeout, browser_ua)
    if good:
        # If we never found a homepage, derive one from the PDF's own host.
        ir_url = homepage_url
        if not ir_url and "//" in good.url:
            ir_url = f"https://{_registrable_domain(good.url.split('/')[2])}"
        await store.upsert(**base, ir_homepage_url=ir_url, pdf_url=good.url,
                           fiscal_year_guess=good.year,
                           discovery_method=_method("pdf_search", verified),
                           status="found", failure_reason=None if verified else "unverified_403")
        return

    # Not resolved in Tier 1 — record the most informative partial state.
    weak = crawl_pdf_cand or (pdf_cands[0] if pdf_cands else None)
    if candidate is None and not pdf_cands:
        await store.upsert(**base, status="needs_review", failure_reason="no_corporate_domain")
    elif weak is not None:
        await store.upsert(**base, ir_homepage_url=homepage_url, pdf_url=weak.url,
                           fiscal_year_guess=weak.year, discovery_method=method or "pdf_search",
                           status="needs_review", failure_reason="weak_pdf_match")
    else:
        await store.upsert(**base, ir_homepage_url=homepage_url, discovery_method=method,
                           status="needs_review", failure_reason="no_pdf_on_ir_site")


# --------------------------------------------------------------------------- #
# Tier 2 — Playwright render pass
# --------------------------------------------------------------------------- #
async def _tier2(row, *, renderer, provider, client, store, blocklist, cfg):
    ua = cfg["crawl"]["user_agent"]
    browser_ua = cfg["crawl"].get("browser_user_agent")
    timeout = float(cfg["crawl"]["timeout_seconds"])
    base = dict(ticker=row["ticker"], company_name=row["company_name"],
                exchange=row["exchange"])
    homepage = row.get("ir_homepage_url")

    # If Tier 1 never found a homepage, try discovery once more (could have been
    # rate-limited earlier), so rendering has somewhere to go.
    if not homepage:
        query = cfg["search"]["query_template"].format(name=row["company_name"])
        try:
            results = await provider.search(query, int(cfg["search"]["max_results"]))
            cand = choose_ir_homepage(results, row["company_name"], blocklist) if results else None
            homepage = cand.url if cand else None
        except (SearchError, SearchRateLimited):
            homepage = None
    if not homepage:
        return  # nothing renderable; leave Tier-1 status untouched

    pdf = await renderer.find_annual_report_pdf(homepage)
    good, verified = await _first_validating([pdf] if pdf else [], client, ua, timeout, browser_ua)
    if good:
        await store.upsert(**base, ir_homepage_url=homepage, pdf_url=good.url,
                           fiscal_year_guess=good.year,
                           discovery_method=_method("render", verified),
                           status="found", failure_reason=None if verified else "unverified_403")
        return
    # Rendered and still nothing — mark it so the review queue shows Tier-2 ran.
    await store.upsert(**base, ir_homepage_url=homepage, status="needs_review",
                       failure_reason="no_pdf_after_render")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
async def run_pipeline(companies, provider: SearchProvider, cfg, *,
                       use_render=True, render_only=False, progress=None):
    store = Store(cfg["storage"]["db_path"])
    blocklist = load_blocklist(cfg.get("blocklist_path", "blocklist.txt"))
    throttle = DomainThrottle(
        max_concurrency=int(cfg["crawl"]["max_concurrency"]),
        per_domain_delay=float(cfg["crawl"]["per_domain_delay_seconds"]),
    )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        crawler = Crawler(client, throttle, cfg["crawl"])

        if not render_only:
            todo = [c for c in companies if not store.already_found(c.ticker)]
            skipped = len(companies) - len(todo)
            if progress:
                progress(f"{len(companies)} companies loaded; {skipped} already found, "
                         f"{len(todo)} to process.")
                progress("Tier 1 (fast: search + deep crawl + PDF search) ...")
            worker_sem = asyncio.Semaphore(int(cfg["crawl"]["max_concurrency"]))
            done = 0
            total = len(todo)

            async def w1(company: Company):
                nonlocal done
                async with worker_sem:
                    try:
                        await _tier1(company, provider=provider, crawler=crawler,
                                     client=client, store=store, blocklist=blocklist, cfg=cfg)
                    except Exception as exc:  # noqa: BLE001
                        await store.upsert(ticker=company.ticker, company_name=company.legal_name,
                                           exchange=company.exchange, status="needs_review",
                                           failure_reason=f"unexpected: {type(exc).__name__}")
                    finally:
                        done += 1
                        if progress and (done % 25 == 0 or done == total):
                            progress(f"  Tier 1: {done}/{total}")

            await asyncio.gather(*(w1(c) for c in todo))
            if progress:
                s = store.summary()
                progress(f"  Tier 1 done -> found {s['found']}, "
                         f"needs_review {s['needs_review']}, not_found {s['not_found']}")

        # ---- Tier 2 -------------------------------------------------------- #
        if use_render or render_only:
            await _run_tier2(store, provider, client, blocklist, cfg, progress)

        # ---- CSE filings fallback (XCNQ companies, after first-party) ------- #
        if cfg.get("cse", {}).get("enabled", True):
            await _run_cse_pass(store, client, cfg, progress)

        # ---- SEC EDGAR flagging pass (after first-party attempts) ----------- #
        if cfg.get("sec", {}).get("enabled", True):
            await _run_sec_pass(store, client, cfg, progress)

    summary = store.summary()
    store.close()
    return summary


async def _run_cse_pass(store, client, cfg, progress):
    """Resolve still-unresolved CSE (XCNQ) companies from the exchange's own
    filings API. Labeled discovery_method='cse_filings' since these documents
    originate from SEDAR (CSE-mirrored); first-party PDFs are already preferred.
    Never touches sedarplus.ca."""
    pending = store.rows_for_cse_pass()
    if not pending:
        return
    ua = cfg["crawl"].get("browser_user_agent") or cfg["crawl"]["user_agent"]
    timeout = float(cfg["crawl"]["timeout_seconds"])
    if progress:
        progress(f"CSE pass: checking {len(pending)} unresolved CSE/XCNQ company(ies) "
                 "against thecse.com filings ...")
    sem = asyncio.Semaphore(int(cfg.get("cse", {}).get("concurrency", 5)))
    resolved = 0

    async def handle(row):
        nonlocal resolved
        async with sem:
            res = await cse_filings.fetch_annual_statement(client, row["ticker"], ua, timeout)
            if not res:
                return
            state, _reason = await validate_pdf(client, res["url"], ua, timeout, ua)
            if state == "fail":
                return
            method = "cse_filings" if state == "ok" else "cse_filings+unverified"
        await store.upsert(ticker=row["ticker"], company_name=row["company_name"],
                           exchange=row["exchange"], pdf_url=res["url"],
                           fiscal_year_guess=res["year"], discovery_method=method,
                           status="found", failure_reason=None if state == "ok" else "unverified_403")
        resolved += 1

    await asyncio.gather(*(handle(r) for r in pending))
    if progress:
        progress(f"  CSE pass: resolved {resolved} of {len(pending)} via CSE filings.")


async def _run_sec_pass(store, client, cfg, progress):
    """Flag still-unresolved companies that file with the SEC, pointing at their
    latest annual filing on EDGAR. First-party PDFs are already preferred (this
    runs only after Tier 1 + Tier 2 could not find one)."""
    pending = store.rows_for_sec_pass()
    if not pending:
        return
    ua = cfg["sec"].get("user_agent", "CanadianARFinder/1.0 (contact: you@example.com)")
    try:
        index = sec_edgar.SecFilerIndex.load(ua)
    except Exception as exc:  # noqa: BLE001 - never let EDGAR being down break the run
        if progress:
            progress(f"SEC pass skipped: could not load EDGAR ticker index ({type(exc).__name__})")
        return

    # Resolve which of the leftovers are actually SEC filers (offline lookup).
    filers = [(r, cik) for r in pending
              if (cik := index.lookup(r["ticker"], r["company_name"])) is not None]
    if not filers:
        if progress:
            progress("SEC pass: no unresolved companies are SEC filers.")
        return
    if progress:
        progress(f"SEC pass: flagging {len(filers)} SEC filer(s) among the unresolved ...")

    sem = asyncio.Semaphore(5)  # SEC fair-access: keep well under 10 req/s

    async def flag(row, cik):
        async with sem:
            filing = await sec_edgar.latest_annual_filing(client, cik, ua)
        url = filing["url"] if filing else sec_edgar.filings_browse_url(cik)
        year = int(filing["date"][:4]) if filing and filing.get("date") else None
        await store.upsert(ticker=row["ticker"], company_name=row["company_name"],
                           exchange=row["exchange"], status="not_found",
                           failure_reason="sec_filer", sec_filer=1, sec_filing_url=url,
                           discovery_method="sec_edgar", fiscal_year_guess=year)

    await asyncio.gather(*(flag(r, cik) for r, cik in filers))


async def _run_tier2(store, provider, client, blocklist, cfg, progress):
    from render import Renderer, RenderUnavailable, playwright_available

    pending = store.rows_needing_tier2()
    if not pending:
        return
    if not playwright_available():
        if progress:
            progress(f"Tier 2 skipped ({len(pending)} rows unresolved): Playwright not "
                     "installed. Enable with:  pip install playwright && playwright install chromium")
        return
    if progress:
        progress(f"Tier 2 (render {len(pending)} unresolved via headless Chromium) ...")
    try:
        async with Renderer(cfg) as renderer:
            sem = asyncio.Semaphore(int(cfg.get("render", {}).get("concurrency", 3)))
            done = 0
            total = len(pending)

            async def w2(row):
                nonlocal done
                async with sem:
                    try:
                        await _tier2(row, renderer=renderer, provider=provider,
                                     client=client, store=store, blocklist=blocklist, cfg=cfg)
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        done += 1
                        if progress and (done % 10 == 0 or done == total):
                            progress(f"  Tier 2: {done}/{total}")

            await asyncio.gather(*(w2(r) for r in pending))
    except RenderUnavailable as exc:
        if progress:
            progress(f"Tier 2 skipped: {exc}")
