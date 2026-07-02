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
import time
from datetime import date, datetime, timezone
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
from verify_pdf import looks_like_financial_statement

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# --------------------------------------------------------------------------- #
# Fiscal-year targeting
# --------------------------------------------------------------------------- #
def expected_annual_year(today: date | None = None) -> int:
    """The fiscal year whose annual report should be the most recently published
    one right now. Most issuers file within a few months of fiscal year-end, so
    for most of the calendar year the PRIOR year's report is the current one.
    Derived from the system clock so this advances every January rather than
    being hardcoded to any particular year."""
    today = today or date.today()
    return today.year - 1


def _is_stale_year(year: int | None) -> bool:
    """True when a resolved PDF's year is older than the expected annual year
    (i.e. not last year's report or newer). We don't reject stale documents —
    a stale first-party PDF is still better than nothing for a thin micro-cap —
    but we tag them so they're easy to spot and re-check."""
    return year is not None and year < expected_annual_year()


def _finalize(method: str, verified: bool, year: int | None) -> tuple[str, str | None]:
    """Build the final discovery_method tag and failure_reason, chaining
    +unverified / +stale suffixes so both conditions stay visible at a glance."""
    m = method
    reasons: list[str] = []
    if not verified:
        m += "+unverified"
        reasons.append("unverified_403")
    if _is_stale_year(year):
        m += "+stale"
        reasons.append("stale_annual_report")
    return m, ("; ".join(reasons) if reasons else None)


def _pct(n: int, d: int) -> float:
    return (n / d * 100.0) if d else 0.0


def _fmt_secs(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    return f"{m}m{sec:02d}s"


def _stage_line(label: str, resolved: int, attempted: int, elapsed: float) -> str:
    return (f"  {label}: {resolved}/{attempted} found "
           f"({_pct(resolved, attempted):.1f}%) in {_fmt_secs(elapsed)}")


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
                         ("sec_filing_url", "sec_filing_url TEXT"),
                         ("sec_filing_form", "sec_filing_form TEXT"),
                         ("sec_filing_date", "sec_filing_date TEXT")):
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

    def rows_for_tmx_pass(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT ticker, company_name, exchange FROM filings "
            "WHERE status != 'found' AND COALESCE(sec_filer, 0) = 0 "
            "AND UPPER(exchange) IN ('TSX', 'TSXV', 'XTSE', 'XTSX')"
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
        row = self._conn.execute(
            "SELECT COUNT(*) FROM filings WHERE discovery_method LIKE 'tmx_filings%'").fetchone()
        counts["tmx_filings"] = row[0] if row else 0
        return counts

    def close(self):
        self._conn.close()


# --------------------------------------------------------------------------- #
# Shared validation
# --------------------------------------------------------------------------- #
async def _first_validating(cands: list[PDFCandidate], client, ua, timeout,
                            browser_ua=None, limit=3, verify_content=True):
    """Pick the best confident candidate.

    Returns (candidate, verified) where verified is True for a confirmed PDF and
    False for a 'blocked' one (exists but the CDN forbids bots — accepted as an
    unverified find since these are trusted own-domain annual-report filenames).
    Returns (None, False) if nothing usable.

    When verify_content is on, a candidate that validates as a reachable PDF is
    also downloaded and content-checked; if it reads like a cover letter/notice
    rather than a financial statement it is skipped and the next candidate tried.
    (Bot-blocked candidates can't be downloaded to inspect, so they're accepted
    unverified as before.)
    """
    blocked: PDFCandidate | None = None
    for cand in cands[:limit]:
        if not is_confident(cand):
            continue
        state, _reason = await validate_pdf(client, cand.url, ua, timeout, browser_ua)
        if state == "ok":
            if verify_content:
                accept, _r = await looks_like_financial_statement(
                    client, cand.url, browser_ua or ua, timeout)
                if not accept:
                    continue  # cover letter / notice / terms -> try next candidate
            return cand, True
        if state == "blocked" and blocked is None:
            blocked = cand
    if blocked is not None:
        return blocked, False
    return None, False


# --------------------------------------------------------------------------- #
# Tier 1 — fast path
# --------------------------------------------------------------------------- #
async def _tier1(company, *, provider, crawler, client, store, blocklist, cfg) -> bool:
    """Returns True iff this company was resolved to status='found'."""
    ticker = company.ticker
    ua = cfg["crawl"]["user_agent"]
    browser_ua = cfg["crawl"].get("browser_user_agent")
    timeout = float(cfg["crawl"]["timeout_seconds"])
    verify_content = bool(cfg.get("verify", {}).get("content_check", True))
    query = cfg["search"]["query_template"].format(name=company.legal_name)
    max_results = int(cfg["search"]["max_results"])
    base = dict(ticker=ticker, company_name=company.legal_name, exchange=company.exchange)

    # Stage 1: search for the IR homepage.
    try:
        results = await provider.search(query, max_results)
    except SearchRateLimited:
        await store.upsert(**base, status="needs_review", failure_reason="search_rate_limited")
        return False
    except SearchError as exc:
        await store.upsert(**base, status="needs_review",
                           failure_reason=f"search_error: {str(exc)[:120]}")
        return False

    if not results:
        await store.upsert(**base, status="not_found", failure_reason="no_search_results")
        return False

    # Stage 2: pick the IR homepage. Only accept a SURE company-domain match
    # (real name/acronym/exact/platform signal) as a first-party source; weak
    # rank-only guesses are dropped so the company defers to the exchange
    # fallbacks (CSE/TMX) instead of asserting a shaky first-party find.
    candidate = choose_ir_homepage(results, company.legal_name, blocklist)
    sure = bool(candidate and candidate.sure)
    homepage_url = candidate.url if sure else None
    reg = candidate.domain if sure else None
    method = ((f"crawl:{candidate.platform}" if candidate.platform else "crawl")
              if sure else None)

    # Stage 3: deep static crawl of the homepage (sure matches only).
    crawl_pdf_cand = None
    if sure:
        try:
            crawl_pdf_cand = await crawler.find_annual_report_pdf(candidate.url)
        except Exception:  # noqa: BLE001
            crawl_pdf_cand = None

    good, verified = await _first_validating(
        [crawl_pdf_cand] if crawl_pdf_cand else [], client, ua, timeout, browser_ua,
        verify_content=verify_content)
    if good:
        m, reason = _finalize(method, verified, good.year)
        await store.upsert(**base, ir_homepage_url=homepage_url, pdf_url=good.url,
                           fiscal_year_guess=good.year, discovery_method=m,
                           status="found", failure_reason=reason)
        return True

    # Stage 3b: direct PDF search fallback (one extra query).
    pdf_cands = await direct_pdf_search(provider, company, blocklist, reg, cfg)
    good, verified = await _first_validating(pdf_cands, client, ua, timeout, browser_ua,
                                             verify_content=verify_content)
    if good:
        # If we never found a homepage, derive one from the PDF's own host.
        ir_url = homepage_url
        if not ir_url and "//" in good.url:
            ir_url = f"https://{_registrable_domain(good.url.split('/')[2])}"
        m, reason = _finalize("pdf_search", verified, good.year)
        await store.upsert(**base, ir_homepage_url=ir_url, pdf_url=good.url,
                           fiscal_year_guess=good.year, discovery_method=m,
                           status="found", failure_reason=reason)
        return True

    # Not resolved in Tier 1 — record the most informative partial state.
    weak = crawl_pdf_cand or (pdf_cands[0] if pdf_cands else None)
    if not sure and not pdf_cands:
        await store.upsert(**base, status="needs_review", failure_reason="no_corporate_domain")
    elif weak is not None:
        await store.upsert(**base, ir_homepage_url=homepage_url, pdf_url=weak.url,
                           fiscal_year_guess=weak.year, discovery_method=method or "pdf_search",
                           status="needs_review", failure_reason="weak_pdf_match")
    else:
        await store.upsert(**base, ir_homepage_url=homepage_url, discovery_method=method,
                           status="needs_review", failure_reason="no_pdf_on_ir_site")
    return False


# --------------------------------------------------------------------------- #
# Tier 2 — Playwright render pass
# --------------------------------------------------------------------------- #
async def _tier2(row, *, renderer, provider, client, store, blocklist, cfg) -> bool:
    """Returns True iff this company was resolved to status='found'."""
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
            homepage = cand.url if (cand and cand.sure) else None
        except (SearchError, SearchRateLimited):
            homepage = None
    if not homepage:
        return False  # nothing renderable; leave Tier-1 status untouched

    verify_content = bool(cfg.get("verify", {}).get("content_check", True))
    pdf = await renderer.find_annual_report_pdf(homepage)
    good, verified = await _first_validating([pdf] if pdf else [], client, ua, timeout,
                                             browser_ua, verify_content=verify_content)
    if good:
        m, reason = _finalize("render", verified, good.year)
        await store.upsert(**base, ir_homepage_url=homepage, pdf_url=good.url,
                           fiscal_year_guess=good.year, discovery_method=m,
                           status="found", failure_reason=reason)
        return True
    # Rendered and still nothing — mark it so the review queue shows Tier-2 ran.
    await store.upsert(**base, ir_homepage_url=homepage, status="needs_review",
                       failure_reason="no_pdf_after_render")
    return False


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
    stage_stats: list[tuple[str, int, int, float]] = []  # (label, attempted, resolved, elapsed)
    run_start = time.monotonic()
    if progress:
        progress(f"Targeting annual reports for fiscal year {expected_annual_year()} or newer "
                 "(adjusts automatically each year).")

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
            resolved_t1 = 0
            total = len(todo)
            t0 = time.monotonic()

            async def w1(company: Company):
                nonlocal done, resolved_t1
                async with worker_sem:
                    try:
                        found = await _tier1(company, provider=provider, crawler=crawler,
                                             client=client, store=store, blocklist=blocklist, cfg=cfg)
                        if found:
                            resolved_t1 += 1
                    except Exception as exc:  # noqa: BLE001
                        await store.upsert(ticker=company.ticker, company_name=company.legal_name,
                                           exchange=company.exchange, status="needs_review",
                                           failure_reason=f"unexpected: {type(exc).__name__}")
                    finally:
                        done += 1
                        if progress and (done % 25 == 0 or done == total):
                            progress(f"  Tier 1: {done}/{total}")

            await asyncio.gather(*(w1(c) for c in todo))
            elapsed = time.monotonic() - t0
            if total:
                stage_stats.append(("Tier 1 (search+crawl+pdf_search)", total, resolved_t1, elapsed))
            if progress:
                s = store.summary()
                progress(_stage_line("Tier 1 done", resolved_t1, total, elapsed))
                progress(f"    still open -> needs_review {s['needs_review']}, not_found {s['not_found']}")

        # ---- Tier 2 -------------------------------------------------------- #
        if use_render or render_only:
            pending = store.rows_needing_tier2()
            if pending:
                t0 = time.monotonic()
                resolved = await _run_tier2(store, provider, client, blocklist, cfg, progress, pending)
                elapsed = time.monotonic() - t0
                stage_stats.append(("Tier 2 (render)", len(pending), resolved, elapsed))
                if progress:
                    progress(_stage_line("Tier 2 done", resolved, len(pending), elapsed))

        # ---- CSE filings fallback (XCNQ companies, after first-party) ------- #
        if cfg.get("cse", {}).get("enabled", True):
            pending = store.rows_for_cse_pass()
            if pending:
                t0 = time.monotonic()
                resolved = await _run_cse_pass(store, client, cfg, progress, pending)
                elapsed = time.monotonic() - t0
                stage_stats.append(("CSE filings fallback (XCNQ)", len(pending), resolved, elapsed))
                if progress:
                    progress(_stage_line("CSE pass done", resolved, len(pending), elapsed))

        # ---- SEC EDGAR flagging pass (after first-party attempts) ----------- #
        if cfg.get("sec", {}).get("enabled", True):
            pending = store.rows_for_sec_pass()
            if pending:
                t0 = time.monotonic()
                flagged = await _run_sec_pass(store, client, cfg, progress, pending)
                elapsed = time.monotonic() - t0
                stage_stats.append(("SEC cross-listing check", len(pending), flagged, elapsed))
                if progress:
                    progress(f"  SEC pass done: {flagged}/{len(pending)} cross-listed "
                             f"({_pct(flagged, len(pending)):.1f}%) in {_fmt_secs(elapsed)}")

        # ---- TMX filings fallback (TSX/TSXV tail, browser-driven) ----------- #
        if cfg.get("tmx", {}).get("enabled", True):
            pending = store.rows_for_tmx_pass()
            if pending:
                t0 = time.monotonic()
                resolved = await _run_tmx_pass(store, client, cfg, progress, pending)
                elapsed = time.monotonic() - t0
                stage_stats.append(("TMX filings fallback (TSX/TSXV)", len(pending), resolved, elapsed))
                if progress:
                    progress(_stage_line("TMX pass done", resolved, len(pending), elapsed))

    total_elapsed = time.monotonic() - run_start
    summary = store.summary()
    store.close()
    summary["_stage_stats"] = stage_stats
    summary["_elapsed_total"] = total_elapsed
    return summary


async def _run_cse_pass(store, client, cfg, progress, pending: list[dict]) -> int:
    """Resolve still-unresolved CSE (XCNQ) companies from the exchange's own
    filings API. Labeled discovery_method='cse_filings' since these documents
    originate from SEDAR (CSE-mirrored); first-party PDFs are already preferred.
    Never touches sedarplus.ca. Returns the number resolved."""
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
            method, reason = _finalize("cse_filings", state == "ok", res["year"])
        await store.upsert(ticker=row["ticker"], company_name=row["company_name"],
                           exchange=row["exchange"], pdf_url=res["url"],
                           fiscal_year_guess=res["year"], discovery_method=method,
                           status="found", failure_reason=reason)
        resolved += 1

    await asyncio.gather(*(handle(r) for r in pending))
    return resolved


async def _run_tmx_pass(store, client, cfg, progress, pending: list[dict]) -> int:
    """Resolve unresolved TSX/TSXV companies by driving the TMX Money Filings
    widget to their latest annual financial statement (labeled 'tmx_filings').
    Browser-driven and slow, so it runs only on the leftover tail. SEC filers
    and CSE companies are already excluded. Never touches sedarplus.ca.
    Returns the number resolved (0 if the pass could not run)."""
    from render import Renderer, RenderUnavailable, playwright_available
    if not playwright_available():
        if progress:
            progress(f"TMX pass skipped ({len(pending)} TSX/TSXV rows): Playwright not "
                     "installed. Enable with:  pip install playwright && playwright install chromium")
        return 0
    ua = cfg["crawl"].get("browser_user_agent") or cfg["crawl"]["user_agent"]
    timeout = float(cfg["crawl"]["timeout_seconds"])
    max_months = int(cfg.get("tmx", {}).get("max_months", 14))
    if progress:
        progress(f"TMX pass: navigating money.tmx.com filings for {len(pending)} "
                 "TSX/TSXV company(ies) [slow, browser-driven] ...")
    resolved = 0
    try:
        async with Renderer(cfg) as renderer:
            sem = asyncio.Semaphore(int(cfg.get("tmx", {}).get("concurrency", 2)))
            done = 0

            async def handle(row):
                nonlocal resolved, done
                async with sem:
                    res = await renderer.tmx_annual_statement(row["ticker"], max_months)
                    if res:
                        state, _r = await validate_pdf(client, res["url"], ua, timeout, ua)
                        if state != "fail":
                            method, reason = _finalize("tmx_filings", state == "ok", res.get("year"))
                            await store.upsert(
                                ticker=row["ticker"], company_name=row["company_name"],
                                exchange=row["exchange"], pdf_url=res["url"],
                                fiscal_year_guess=res.get("year"), discovery_method=method,
                                status="found", failure_reason=reason)
                            resolved += 1
                done += 1
                if progress and (done % 20 == 0 or done == len(pending)):
                    progress(f"  TMX pass: {done}/{len(pending)} (resolved {resolved})")

            await asyncio.gather(*(handle(r) for r in pending))
    except RenderUnavailable as exc:
        if progress:
            progress(f"TMX pass skipped: {exc}")
        return resolved
    return resolved


async def _run_sec_pass(store, client, cfg, progress, pending: list[dict]) -> int:
    """Flag still-unresolved companies that are cross-listed with the SEC,
    pointing at their latest annual filing on EDGAR. First-party PDFs are
    already preferred (this runs only after Tier 1 + Tier 2 could not find
    one). Returns the number identified as SEC cross-listed."""
    ua = cfg["sec"].get("user_agent", "CanadianARFinder/1.0 (contact: you@example.com)")
    try:
        index = sec_edgar.SecFilerIndex.load(ua)
    except Exception as exc:  # noqa: BLE001 - never let EDGAR being down break the run
        if progress:
            progress(f"SEC pass skipped: could not load EDGAR ticker index ({type(exc).__name__})")
        return 0

    # Resolve which of the leftovers are actually SEC cross-listed (offline lookup).
    filers = [(r, cik) for r in pending
              if (cik := index.lookup(r["ticker"], r["company_name"])) is not None]
    if not filers:
        if progress:
            progress("SEC pass: no unresolved companies are cross-listed with the SEC.")
        return 0
    if progress:
        progress(f"SEC pass: {len(filers)} unresolved company(ies) are SEC cross-listed "
                 "(financials available on EDGAR; excluded from needs_review) ...")

    sem = asyncio.Semaphore(5)  # SEC fair-access: keep well under 10 req/s

    async def flag(row, cik):
        async with sem:
            filing = await sec_edgar.latest_annual_filing(client, cik, ua)
        url = filing["url"] if filing else sec_edgar.filings_browse_url(cik)
        form = filing.get("form") if filing else None
        fdate = filing.get("date") if filing else None
        year = int(fdate[:4]) if fdate else None
        method, _reason = _finalize("sec_edgar", True, year)
        # pdf_url mirrors sec_filing_url so the SEC notice shows up in the same
        # column every other source uses, not just the SEC-specific fields.
        await store.upsert(ticker=row["ticker"], company_name=row["company_name"],
                           exchange=row["exchange"], status="not_found",
                           failure_reason="sec_filer", sec_filer=1,
                           pdf_url=url, sec_filing_url=url,
                           sec_filing_form=form, sec_filing_date=fdate,
                           discovery_method=method, fiscal_year_guess=year)
        if progress and len(filers) <= 20:
            progress(f"    cross-listed: {row['ticker']} -> CIK {cik} "
                     f"(latest {form or '?'}{', ' + fdate if fdate else ''})")

    await asyncio.gather(*(flag(r, cik) for r, cik in filers))
    return len(filers)


async def _run_tier2(store, provider, client, blocklist, cfg, progress, pending: list[dict]) -> int:
    """Returns the number resolved (0 if the pass could not run)."""
    from render import Renderer, RenderUnavailable, playwright_available

    if not playwright_available():
        if progress:
            progress(f"Tier 2 skipped ({len(pending)} rows unresolved): Playwright not "
                     "installed. Enable with:  pip install playwright && playwright install chromium")
        return 0
    if progress:
        progress(f"Tier 2 (render {len(pending)} unresolved via headless Chromium) ...")
    resolved = 0
    try:
        async with Renderer(cfg) as renderer:
            sem = asyncio.Semaphore(int(cfg.get("render", {}).get("concurrency", 3)))
            done = 0
            total = len(pending)

            async def w2(row):
                nonlocal done, resolved
                async with sem:
                    try:
                        found = await _tier2(row, renderer=renderer, provider=provider,
                                             client=client, store=store, blocklist=blocklist, cfg=cfg)
                        if found:
                            resolved += 1
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
        return resolved
    return resolved
