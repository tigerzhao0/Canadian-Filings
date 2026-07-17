"""Orchestrates the resolution cascade with SQLite checkpointing.

Official exchange/regulator sources are tried first, before any scraping:
    CSE filings API (XCNQ) -> SEC EDGAR cross-listing check -> TMX filings
    (TSX/TSXV, via TMX's own GraphQL API -- no browser needed).

Scraping only runs on whatever's still unresolved after that:
    Tier 1 (fast, no browser): search -> pick IR homepage -> deep static
        crawl -> if that fails, a direct "<name> annual report filetype:pdf"
        search -> HEAD-validate -> store.
    Tier 2 (heavy, Playwright), only over rows Tier 1 left un-found:
        render the JS-heavy IR site and re-run the same PDF heuristics.

SQLite is the source of truth and every company is upserted the moment it is
resolved, so a crash mid-run loses nothing and re-runs skip 'found' rows.
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
import tmx_filings
from search_provider import SearchProvider, SearchRateLimited, SearchError
from validate import validate_pdf
from verify_pdf import looks_like_financial_statement

# src/ holds this module; the SQL schemas live in the sibling sql/ dir.
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


# --------------------------------------------------------------------------- #
# Fiscal-year targeting
# --------------------------------------------------------------------------- #
def expected_annual_year(today: date | None = None) -> int:
    """The fiscal year whose annual report we're targeting right now (e.g.
    2026 for the whole of calendar 2026). Derived from the system clock so
    this advances every January rather than being hardcoded to any particular
    year."""
    today = today or date.today()
    return today.year


# How many years of annual-report PDFs the PDF finder tries to collect per
# company. 10 = the target history depth: each annual-report PDF carries ~2
# comparative years, so 10 PDFs give up to 11 fiscal years. CSE's SEDAR mirror
# and year-pattern URL probing (below) reach that deep for many issuers; the
# --years CLI flag still overrides. INDEPENDENT of the structured-financials
# depth (tmx_financials.num_years, ~14 via QuoteMedia).
FILING_YEARS = 10

# "Go as far back as a PDF is findable per company." Cheap, lossless sources
# (the year-pattern URL probe, the CSE filings API, the TMX filings GraphQL
# API, SEC EDGAR, IR-crawl dedup) reach this deep instead of stopping at
# FILING_YEARS -- a year with no findable PDF simply yields no hit, so this is
# a SAFETY FLOOR, not a fixed target. Only the request-heavy annualreports.com
# sweep stays at FILING_YEARS. Every deep candidate is content-verified before
# storage (verify_pdf.expected_year), so reaching deep never fabricates a year.
MAX_HISTORY_YEARS = 25

# TMX's GraphQL filings API (tmx_filings.py) is cheap plain HTTP -- a year with
# no filings just returns empty in well under a second -- so its pass searches
# much deeper than MAX_HISTORY_YEARS: as far back as the exchange's electronic
# filing history plausibly goes for any TSX/TSXV issuer, not a cost-driven cap.
TMX_HISTORY_YEARS = 40


def _dedupe_recent_years(cands: list[dict], limit: int = FILING_YEARS) -> list[dict]:
    """From a source's annual candidates (each a dict with an int|None 'year',
    given most-recent-first), keep the first (most-recent) candidate per
    DISTINCT fiscal year, for the `limit` most-recent years. Year=None entries
    can't be placed in the per-year filing_pdfs table, so they're dropped here
    (they still feed the single-row `filings` primary pointer)."""
    by_year: dict[int, dict] = {}
    for c in cands:
        y = c.get("year")
        if y is None:
            continue
        by_year.setdefault(int(y), c)  # first-seen == most-recent for that year
    return [by_year[y] for y in sorted(by_year, reverse=True)[:limit]]


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


def _sec_note(cik: int, form: str | None, fdate: str | None) -> str:
    """A plain-text note (not a URL) for the pdf_url column of an SEC
    cross-listed company — the real link lives in sec_filing_url."""
    if form:
        return f"SEC cross-listed -- see EDGAR CIK {cik} (latest {form}, filed {fdate})"
    return f"SEC cross-listed -- see EDGAR CIK {cik} (no annual filing found via API)"


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
        # Default SQLite fsyncs on every commit(); with thousands of individual
        # per-company upserts (including the up-front seeding pass) that adds up
        # fast, more so since this DB file lives inside a OneDrive-synced folder.
        # WAL + NORMAL synchronous is still crash-safe for our purposes (a lost
        # last-write on an ungraceful kill just means that one row gets
        # re-processed on the next run) and is dramatically faster for this
        # write pattern.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
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

    async def reset_for_overwrite(self, tickers: list[str]) -> int:
        """Force-reprocess: set the given (currently 'found') companies back to
        'needs_review', clear their prior pdf_url, and drop their per-year
        filing_pdfs rows, so the finder re-runs and overwrites them. Returns how
        many 'found' rows were reset."""
        if not tickers:
            return 0
        qs = ",".join("?" for _ in tickers)
        async with self._lock:
            n = self._conn.execute(
                f"SELECT COUNT(*) FROM filings WHERE status='found' AND ticker IN ({qs})",
                tickers).fetchone()[0]
            self._conn.execute(
                f"UPDATE filings SET status='needs_review', pdf_url=NULL "
                f"WHERE ticker IN ({qs})", tickers)
            self._conn.execute(f"DELETE FROM filing_pdfs WHERE ticker IN ({qs})", tickers)
            self._conn.commit()
        return n

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

    async def upsert_filing_pdfs(self, rows: list[dict]) -> None:
        """Write up to ~5 per-year annual-filing rows for one company into the
        filing_pdfs detail table (one row per fiscal_year). Called by every
        Stage-2 source in addition to the per-company `filings` status upsert."""
        if not rows:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        async with self._lock:
            for r in rows:
                fields = {**r, "last_checked": now}
                cols = ", ".join(fields)
                placeholders = ", ".join("?" for _ in fields)
                updates = ", ".join(f"{c}=excluded.{c}" for c in fields
                                    if c not in ("ticker", "fiscal_year"))
                sql = (f"INSERT INTO filing_pdfs ({cols}) VALUES ({placeholders}) "
                       f"ON CONFLICT(ticker, fiscal_year) DO UPDATE SET {updates}")
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
        # Real downloadable PDF links only — excludes the SEC pass's plain-text
        # "see EDGAR" note (pdf_url is set but isn't an actual file link there).
        row = self._conn.execute(
            "SELECT COUNT(*) FROM filings WHERE pdf_url IS NOT NULL "
            "AND COALESCE(sec_filer, 0) = 0").fetchone()
        counts["with_pdf_url"] = row[0] if row else 0
        # Multi-year detail: total per-year annual filings collected, and how
        # many distinct companies have at least one.
        row = self._conn.execute("SELECT COUNT(*) FROM filing_pdfs").fetchone()
        counts["filing_pdf_years"] = row[0] if row else 0
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM filing_pdfs").fetchone()
        counts["filing_pdf_companies"] = row[0] if row else 0
        return counts

    def close(self):
        self._conn.close()


# --------------------------------------------------------------------------- #
# Shared validation
# --------------------------------------------------------------------------- #
async def _first_validating(cands: list[PDFCandidate], client, ua, timeout,
                            browser_ua=None, limit=5, verify_content=True,
                            company_name=None):
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
                    client, cand.url, browser_ua or ua, timeout, company_name=company_name)
                if not accept:
                    continue  # cover letter / notice / terms -> try next candidate
            return cand, True
        if state == "blocked" and blocked is None:
            blocked = cand
    if blocked is not None:
        return blocked, False
    return None, False


async def _validate_year_candidates(cands, client, ua, timeout, browser_ua, method,
                                    verify_content, base):
    """Validate a list of already-year-deduped exchange candidates (dicts with
    'url'/'year', most-recent first) for CSE/TMX. Returns (filing_pdf_rows,
    primary) where each row is a per-year filing_pdfs record and `primary` is
    the most-recent usable candidate (for the single `filings` pointer), or
    None if nothing validated."""
    rows: list[dict] = []
    primary: dict | None = None
    for c in cands:
        state, _r = await validate_pdf(client, c["url"], ua, timeout, browser_ua)
        if state == "fail":
            continue
        verified = state == "ok"
        if verify_content and verified:
            accept, _r2 = await looks_like_financial_statement(
                client, c["url"], browser_ua or ua, timeout,
                company_name=base.get("company_name"))
            if not accept:
                continue  # wrong company / interim / non-statement -> skip this year
        m, reason = _finalize(method, verified, c.get("year"))
        rows.append(dict(**base, fiscal_year=c["year"], pdf_url=c["url"],
                        discovery_method=m, verified=1 if verified else 0,
                        failure_reason=reason))
        if primary is None:
            primary = {"url": c["url"], "year": c["year"], "method": m, "reason": reason}
    return rows, primary


async def _collect_pdfcandidate_years(cands: list[PDFCandidate], client, ua, timeout,
                                      browser_ua, verify_content, method, base):
    """The Tier-1/Tier-2 analogue of _validate_year_candidates: from ranked
    PDFCandidates (IR-site crawl), take up to FILING_YEARS confident annual
    PDFs, one per distinct year, validated. Returns (filing_pdf_rows, primary).
    Coverage is best-effort -- IR sites often expose only the latest year or
    two, unlike the exchange/EDGAR feeds."""
    confident = [c for c in cands if is_confident(c)]
    picked = _dedupe_recent_years(
        [{"year": c.year, "cand": c} for c in confident], MAX_HISTORY_YEARS)
    rows: list[dict] = []
    primary: dict | None = None
    for entry in picked:
        cand = entry["cand"]
        state, _r = await validate_pdf(client, cand.url, ua, timeout, browser_ua)
        if state == "fail":
            continue
        verified = state == "ok"
        if verify_content and verified:
            accept, _r2 = await looks_like_financial_statement(
                client, cand.url, browser_ua or ua, timeout,
                company_name=base.get("company_name"))
            if not accept:
                continue
        m, reason = _finalize(method, verified, cand.year)
        rows.append(dict(**base, fiscal_year=cand.year, pdf_url=cand.url,
                        discovery_method=m, verified=1 if verified else 0,
                        failure_reason=reason))
        if primary is None:
            primary = {"url": cand.url, "year": cand.year, "method": m, "reason": reason}
    return rows, primary


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

    # Stage 3: deep static crawl of the homepage (sure matches only). Returns a
    # ranked list (most-recent-first among confident candidates) so a dead/
    # blocked top link doesn't sink an otherwise-successful crawl — we retry
    # down the list until one validates.
    crawl_pdf_cands: list[PDFCandidate] = []
    if sure:
        try:
            crawl_pdf_cands = await crawler.find_annual_report_pdf(candidate.url)
        except Exception:  # noqa: BLE001
            crawl_pdf_cands = []

    good, verified = await _first_validating(
        crawl_pdf_cands, client, ua, timeout, browser_ua, verify_content=verify_content,
        company_name=company.legal_name)
    if good:
        m, reason = _finalize(method, verified, good.year)
        # Also record up to FILING_YEARS of annual PDFs found on the IR site
        # (best-effort -- many IR sites only expose the latest year or two).
        rows, _p = await _collect_pdfcandidate_years(
            crawl_pdf_cands, client, ua, timeout, browser_ua, verify_content, method, base)
        if rows:
            await store.upsert_filing_pdfs(rows)
        await store.upsert(**base, ir_homepage_url=homepage_url, pdf_url=good.url,
                           fiscal_year_guess=good.year, discovery_method=m,
                           status="found", failure_reason=reason)
        return True

    # Stage 3b: direct PDF search fallback (one extra query).
    pdf_cands = await direct_pdf_search(provider, company, blocklist, reg, cfg)
    good, verified = await _first_validating(pdf_cands, client, ua, timeout, browser_ua,
                                             verify_content=verify_content,
                                             company_name=company.legal_name)
    if good:
        # If we never found a homepage, derive one from the PDF's own host.
        ir_url = homepage_url
        if not ir_url and "//" in good.url:
            ir_url = f"https://{_registrable_domain(good.url.split('/')[2])}"
        m, reason = _finalize("pdf_search", verified, good.year)
        rows, _p = await _collect_pdfcandidate_years(
            pdf_cands, client, ua, timeout, browser_ua, verify_content, "pdf_search", base)
        if rows:
            await store.upsert_filing_pdfs(rows)
        await store.upsert(**base, ir_homepage_url=ir_url, pdf_url=good.url,
                           fiscal_year_guess=good.year, discovery_method=m,
                           status="found", failure_reason=reason)
        return True

    # Not resolved in Tier 1 — record the most informative partial state.
    weak = (crawl_pdf_cands[0] if crawl_pdf_cands else None) or (pdf_cands[0] if pdf_cands else None)
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
    pdfs = await renderer.find_annual_report_pdf(homepage)
    good, verified = await _first_validating(pdfs, client, ua, timeout, browser_ua,
                                             verify_content=verify_content,
                                             company_name=row["company_name"])
    if good:
        m, reason = _finalize("render", verified, good.year)
        rows, _p = await _collect_pdfcandidate_years(
            pdfs, client, ua, timeout, browser_ua, verify_content, "render", base)
        if rows:
            await store.upsert_filing_pdfs(rows)
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
                       use_render=True, render_only=False, overwrite=False, progress=None):
    store = Store(cfg["storage"]["db_path"])
    blocklist = load_blocklist(cfg.get("blocklist_path", "data/blocklist.txt"))
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

        # ---- Overwrite: force re-processing of already-'found' companies by
        # resetting their status and clearing their old PDF links. Without this
        # the passes below skip anything status='found' (resume behaviour). ----
        if overwrite:
            n = await store.reset_for_overwrite([c.ticker for c in companies])
            if progress:
                progress(f"Overwrite: reset {n} already-found compan(ies) to re-process.")

        # ---- Seed DB rows so CSE/SEC/TMX (which now run before scraping) ---- #
        # have something to query; skip companies already resolved on a prior run.
        seeded = 0
        for c in companies:
            if not store.already_found(c.ticker):
                await store.upsert(ticker=c.ticker, company_name=c.legal_name,
                                   exchange=c.exchange, status="needs_review")
                seeded += 1
        if progress:
            progress(f"{len(companies)} companies loaded; {len(companies) - seeded} already "
                     f"found, {seeded} to process.")

        # ---- annualreports.com structured-URL pass (cheapest, runs FIRST) --- #
        # Deterministic {EXCH}_{TICKER}_{YEAR}.pdf pattern -- real annual
        # reports, no search/scrape. Also re-targets companies whose current
        # PDF the step-2 doc gate rejected (MRFP/MD&A picked by mistake).
        if cfg.get("annualreports", {}).get("enabled", True):
            t0 = time.monotonic()
            try:
                ar_added, ar_resolved, ar_checked = await _run_annualreports_pass(
                    store, client, cfg, progress, companies)
                elapsed = time.monotonic() - t0
                if ar_checked:
                    stage_stats.append(("annualreports.com pattern probe",
                                        ar_checked, ar_resolved, elapsed))
                if progress and ar_checked:
                    progress(f"  annualreports.com done: {ar_resolved} resolved, "
                             f"{ar_added} year-PDFs added in {_fmt_secs(elapsed)}")
            except Exception as exc:  # noqa: BLE001 - fail soft, source is optional
                if progress:
                    progress(f"  annualreports.com pass skipped ({type(exc).__name__}: {exc})")

        # ---- CSE filings fallback (XCNQ companies, tried first) ------------- #
        if cfg.get("cse", {}).get("enabled", True):
            pending = store.rows_for_cse_pass()
            if pending:
                t0 = time.monotonic()
                resolved = await _run_cse_pass(store, client, cfg, progress, pending)
                elapsed = time.monotonic() - t0
                stage_stats.append(("CSE filings fallback (XCNQ)", len(pending), resolved, elapsed))
                if progress:
                    progress(_stage_line("CSE pass done", resolved, len(pending), elapsed))

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

        # ---- Tier 1 (scrape: only for whatever CSE/SEC/TMX left unresolved) - #
        if not render_only:
            todo = [c for c in companies if not store.already_found(c.ticker)]
            if progress:
                progress(f"Tier 1 (fast: search + deep crawl + PDF search) on "
                         f"{len(todo)} still-unresolved companies ...")
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

        # ---- Tier 2 (scrape: render, last resort) --------------------------- #
        if use_render or render_only:
            pending = store.rows_needing_tier2()
            if pending:
                t0 = time.monotonic()
                resolved = await _run_tier2(store, provider, client, blocklist, cfg, progress, pending)
                elapsed = time.monotonic() - t0
                stage_stats.append(("Tier 2 (render)", len(pending), resolved, elapsed))
                if progress:
                    progress(_stage_line("Tier 2 done", resolved, len(pending), elapsed))

        # ---- SEC EDGAR flagging pass (LAST: only what nothing else found) --- #
        # Runs after CSE/TMX/Tier1/Tier2 so a company's OWN annual-report PDF
        # always wins over an SEC-cross-listing note (RY: rbc.com's ar_YYYY_e.pdf
        # series beats the 40-F HTML flag). Only still-unresolved rows get
        # flagged.
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

        # ---- Year-pattern URL extrapolation (fills the 10-year history) ----- #
        if cfg.get("probe_year_patterns", True):
            t0 = time.monotonic()
            probed = await _probe_year_patterns(store, client, cfg, progress)
            elapsed = time.monotonic() - t0
            if probed:
                stage_stats.append(("Year-pattern URL probe", probed, probed, elapsed))

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
    verify_content = bool(cfg.get("verify", {}).get("content_check", True))
    if progress:
        progress(f"CSE pass: checking {len(pending)} unresolved CSE/XCNQ company(ies) "
                 "against thecse.com filings ...")
    sem = asyncio.Semaphore(int(cfg.get("cse", {}).get("concurrency", 5)))
    resolved = 0

    async def handle(row):
        nonlocal resolved
        async with sem:
            base = dict(ticker=row["ticker"], company_name=row["company_name"],
                        exchange=row["exchange"])
            # CSE's SEDAR-mirror API is cheap and returns whatever history it
            # has -- reach as far back as it offers (content-verified downstream).
            candidates = await cse_filings.fetch_annual_statements(
                client, row["ticker"], ua, timeout, years=MAX_HISTORY_YEARS)
            cands = _dedupe_recent_years(candidates, MAX_HISTORY_YEARS)
            rows, primary = await _validate_year_candidates(
                cands, client, ua, timeout, ua, "cse_filings", verify_content, base)
            if rows:
                await store.upsert_filing_pdfs(rows)
            if primary:
                await store.upsert(**base, pdf_url=primary["url"],
                                   fiscal_year_guess=primary["year"],
                                   discovery_method=primary["method"],
                                   status="found", failure_reason=primary["reason"])
                resolved += 1

    await asyncio.gather(*(handle(r) for r in pending))
    return resolved


async def _run_tmx_pass(store, client, cfg, progress, pending: list[dict]) -> int:
    """Resolve unresolved TSX/TSXV companies via TMX Money's own GraphQL API
    (labeled 'tmx_filings_api') -- plain HTTP, no browser needed. SEC filers
    and CSE companies are already excluded. Never touches sedarplus.ca.
    Returns the number resolved."""
    ua = cfg["crawl"].get("browser_user_agent") or cfg["crawl"]["user_agent"]
    timeout = float(cfg["crawl"]["timeout_seconds"])
    # No content-check here: TMX's own API already labels each filing's TYPE
    # (pick_annuals/pick_secondary in tmx_filings.py already restrict to
    # "Annual report"/"Audited annual financial statements" before a URL ever
    # reaches this point), so downloading and pypdf-parsing every candidate to
    # re-confirm it is unnecessary extra cost for this source specifically --
    # unlike a scraped IR-site link, there's no risk of a mislabeled cover
    # letter here. validate_pdf's cheap HEAD/ranged-GET reachability check
    # still runs below.
    verify_content = False
    current_year = expected_annual_year()
    if progress:
        progress(f"TMX pass: querying TMX's filings API for {len(pending)} "
                 "TSX/TSXV company(ies) ...")
    sem = asyncio.Semaphore(int(cfg.get("tmx", {}).get("concurrency", 15)))
    resolved = 0
    done = 0

    async def handle(row):
        nonlocal resolved, done
        async with sem:
            base = dict(ticker=row["ticker"], company_name=row["company_name"],
                        exchange=row["exchange"])
            candidates = await tmx_filings.annual_statements_via_api(
                client, row["ticker"], ua, timeout, years=TMX_HISTORY_YEARS,
                current_year=current_year)
            cands = _dedupe_recent_years(candidates, TMX_HISTORY_YEARS)
            rows, primary = await _validate_year_candidates(
                cands, client, ua, timeout, ua, "tmx_filings_api", verify_content, base)
            if rows:
                await store.upsert_filing_pdfs(rows)
            if primary:
                await store.upsert(**base, pdf_url=primary["url"],
                                   fiscal_year_guess=primary["year"],
                                   discovery_method=primary["method"],
                                   status="found", failure_reason=primary["reason"])
                resolved += 1
        done += 1
        if progress and (done % 20 == 0 or done == len(pending)):
            progress(f"  TMX pass: {done}/{len(pending)} (resolved {resolved})")

    await asyncio.gather(*(handle(r) for r in pending))
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
            filings = await sec_edgar.recent_annual_filings(client, cik, ua, limit=MAX_HISTORY_YEARS)
        latest = filings[0] if filings else None
        url = latest["url"] if latest else sec_edgar.filings_browse_url(cik)
        form = latest.get("form") if latest else None
        fdate = latest.get("date") if latest else None
        year = int(fdate[:4]) if fdate else None
        method, _reason = _finalize("sec_edgar", True, year)
        # Record up to FILING_YEARS of real EDGAR annual filings (10-K/40-F/20-F,
        # never quarterly) in the per-year filing_pdfs table -- these ARE genuine
        # annual filing documents, so a real link per year is correct there.
        pdf_rows = [dict(ticker=row["ticker"], company_name=row["company_name"],
                        exchange=row["exchange"], fiscal_year=f["year"],
                        pdf_url=f["url"], discovery_method="sec_edgar",
                        verified=1, failure_reason=None)
                    for f in filings if f.get("year") is not None]
        if pdf_rows:
            await store.upsert_filing_pdfs(pdf_rows)
        # In the per-company `filings` row this company already has its financials
        # on EDGAR under a different regime — don't hand back a second "own" link
        # as if it were a first-party/exchange PDF. Write a plain-text NOTE in
        # pdf_url; the real latest URL lives in sec_filing_url.
        note = _sec_note(cik, form, fdate)
        await store.upsert(ticker=row["ticker"], company_name=row["company_name"],
                           exchange=row["exchange"], status="not_found",
                           failure_reason="sec_filer", sec_filer=1,
                           pdf_url=note, sec_filing_url=url,
                           sec_filing_form=form, sec_filing_date=fdate,
                           discovery_method=method, fiscal_year_guess=year)
        if progress and len(filers) <= 20:
            progress(f"    cross-listed: {row['ticker']} -> CIK {cik} "
                     f"({len(pdf_rows)} annual filing(s); latest {form or '?'}"
                     f"{', ' + fdate if fdate else ''})")

    await asyncio.gather(*(flag(r, cik) for r, cik in filers))
    return len(filers)


async def _run_annualreports_pass(store, client, cfg, progress, companies) -> tuple[int, int, int]:
    """Probe annualreports.com's deterministic {EXCH}_{TICKER}_{YEAR}.pdf URLs
    for every selected company. Targets: (a) unresolved companies (become
    'found'), (b) resolved companies missing years in the FILING_YEARS window
    (adds filing_pdfs rows), (c) companies whose extracted PDFs the doc gate
    ALL rejected (mda/aif/other -- the wrong-document case): a hit REPLACES
    their primary pdf_url. Fails soft: one reachability check up front, any
    connection trouble skips the pass. Returns (years_added, resolved, checked)."""
    import annualreports_source as ars

    ua = cfg["crawl"].get("browser_user_agent") or cfg["crawl"].get("user_agent", "Mozilla/5.0")
    timeout = min(float(cfg["crawl"].get("timeout_seconds", 20)), 15.0)

    # Reachability gate: the site refuses some networks; don't burn the run.
    try:
        await client.get("https://www.annualreports.com/", timeout=10.0,
                         headers={"User-Agent": ua})
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"site unreachable ({type(exc).__name__})") from exc

    current_year = expected_annual_year()
    targets = list(range(current_year, current_year - FILING_YEARS, -1))

    # Which tickers have ONLY doc-gate-rejected extractions? (wrong document)
    bad_doc: set[str] = set()
    try:
        cur = store._conn.execute(
            "SELECT ticker FROM pdf_extractions GROUP BY ticker HAVING "
            "SUM(CASE WHEN COALESCE(doc_type,'primary_statements') IN "
            "('primary_statements','annual_report_wrapper') THEN 1 ELSE 0 END) = 0")
        bad_doc = {r[0] for r in cur.fetchall()}
    except Exception:  # noqa: BLE001 - table may not exist yet
        pass

    have_years: dict[str, set[int]] = {}
    for tk, fy in store._conn.execute(
            "SELECT ticker, fiscal_year FROM filing_pdfs WHERE pdf_url IS NOT NULL"):
        have_years.setdefault(tk, set()).add(fy)

    ar_cfg = cfg.get("annualreports", {}) or {}
    sem = asyncio.Semaphore(int(ar_cfg.get("concurrency", 3)))
    added = resolved = checked = skipped_no_target = no_prefix = no_hits = 0
    total = len(companies)

    # Good-citizen politeness so we don't trip a rate-limiter (NOT bot-block
    # evasion): a global request PACER (min seconds between requests to the
    # host), a per-run URL cache so we never re-hit the same URL, and polite
    # exponential backoff-with-jitter on a 429/503 "blocked" response.
    import random
    import time as _time
    min_delay = float(ar_cfg.get("min_delay_seconds", 1.0))
    max_retries = int(ar_cfg.get("max_retries", 3))
    _pace_lock = asyncio.Lock()
    _next_allowed = [0.0]
    _probe_cache: dict[str, bool] = {}

    async def _pace() -> None:
        async with _pace_lock:
            now = _time.monotonic()
            wait = _next_allowed[0] - now
            if wait > 0:
                await asyncio.sleep(wait)
            _next_allowed[0] = _time.monotonic() + min_delay

    async def probe(url) -> bool:
        if url in _probe_cache:                      # never re-hit a URL this run
            return _probe_cache[url]
        result = False
        for attempt in range(max_retries):
            await _pace()
            state, _ = await validate_pdf(client, url, ua, timeout)
            if state == "ok":
                result = True
                break
            if state != "blocked":                   # genuine 404/miss -> stop
                break
            # 429/403 "blocked": back off politely (jittered) and retry a few
            # times; give up gracefully rather than hammering.
            await asyncio.sleep(min(30.0, (2 ** attempt) + random.uniform(0, 1)))
        _probe_cache[url] = result
        return result

    async def handle(c):
        nonlocal added, resolved, checked, skipped_no_target, no_prefix, no_hits
        async with sem:
            checked += 1
            if progress and (checked % 50 == 0 or checked == total):
                progress(f"  annualreports.com: checked {checked}/{total} "
                         f"(resolved {resolved}, {added} year-PDFs so far)")
            unresolved = not store.already_found(c.ticker)
            retarget = c.ticker in bad_doc
            have = have_years.get(c.ticker, set())
            missing = [y for y in targets if y not in have]
            if not (unresolved or retarget or missing):
                skipped_no_target += 1
                return
            # 1) find the working (exchange, ticker) prefix on recent years
            prefix = None
            hit0 = None
            for exch in ars.exchange_prefixes(c.exchange):
                for tick in ars.ticker_variants(c.ticker):
                    for year in targets[:2]:
                        if await probe(ars.current_url(exch, tick, year)):
                            prefix, hit0 = (exch, tick), year
                            break
                    if prefix:
                        break
                if prefix:
                    break
            if not prefix:
                no_prefix += 1
                return
            exch, tick = prefix
            # 2) sweep the window with that prefix (current path, then archive).
            #    A reachability hit (cheap ranged GET) is only a CANDIDATE --
            #    content-verify it (right doc, right year, not interim) before
            #    storing, instead of the old blind verified=1. A URL that
            #    serves the wrong year (e.g. the latest report under an old
            #    year's path) or a quarterly doc is dropped here.
            hits: list[tuple[int, str]] = []
            for year in targets:
                if year in have and not (retarget and year == hit0):
                    continue
                for url in ars.year_urls(exch, tick, year, c.legal_name):
                    if not await probe(url):
                        continue
                    ok_content, _reason = await looks_like_financial_statement(
                        client, url, ua, timeout, company_name=c.legal_name,
                        expected_year=year)
                    if ok_content:
                        hits.append((year, url))
                    break
            if not hits:
                no_hits += 1
                return
            rows = [dict(ticker=c.ticker, company_name=c.legal_name,
                         exchange=c.exchange, fiscal_year=y, pdf_url=u,
                         discovery_method="annualreports_com", verified=1)
                    for y, u in hits]
            await store.upsert_filing_pdfs(rows)
            added += len(rows)
            years_str = ",".join(str(y) for y, _ in sorted(hits, reverse=True))
            tag = "NEW" if unresolved else ("RETARGET" if retarget else "FILL")
            if progress:
                progress(f"  annualreports.com [{tag}] {c.ticker} ({exch}_{tick}): "
                         f"{len(hits)} year(s) -> {years_str}")
            if unresolved or retarget:
                y, u = max(hits)
                await store.upsert(ticker=c.ticker, company_name=c.legal_name,
                                   exchange=c.exchange, pdf_url=u,
                                   fiscal_year_guess=y,
                                   discovery_method="annualreports_com",
                                   status="found", failure_reason=None)
                resolved += 1

    if progress:
        progress(f"annualreports.com pass: probing {len(companies)} company(ies) "
                 f"({FILING_YEARS}-year window) ...")
    await asyncio.gather(*(handle(c) for c in companies))
    if progress:
        progress(f"  annualreports.com breakdown: {resolved} resolved, {added} year-PDF(s) "
                 f"added; no-target(already complete) {skipped_no_target}, "
                 f"no-matching-prefix {no_prefix}, prefix-found-but-no-hits {no_hits}")
    return added, resolved, checked


_YEAR_PROBE_OPAQUE_HOSTS = ("thecse.com", "quotemedia.com", "sec.gov")


def _year_probe_candidates(rows: list[tuple], targets: set[int]) -> list[tuple[int, str]]:
    """Pure logic (no I/O): given one ticker's (fiscal_year, url, name, exchange)
    rows, return [(year, candidate_url), ...] worth probing for missing years.
    Isolated from _probe_year_patterns so the URL-pattern bug class (query
    string / opaque-host false positives) is directly unit-testable."""
    from urllib.parse import urlsplit, urlunsplit

    patterned = []
    for fy, url, name, exch in rows:
        split = urlsplit(url)
        if any(h in split.netloc for h in _YEAR_PROBE_OPAQUE_HOSTS):
            continue
        fname = split.path.rsplit("/", 1)[-1]   # PATH only -- never the query string
        if str(fy) in fname:
            patterned.append((fy, url, name, exch))
    if not patterned:
        return []
    fy0, url0, _name, _exch = max(patterned)      # newest as the template
    have = {fy for fy, *_ in rows}
    split0 = urlsplit(url0)
    out = []
    for year in sorted(targets - have, reverse=True):
        new_path = split0.path.replace(str(fy0), str(year))
        if new_path == split0.path:
            continue
        out.append((year, urlunsplit(split0._replace(path=new_path))))
    return out


async def _probe_year_patterns(store, client, cfg, progress) -> int:
    """Fill the 10-year history via URL extrapolation: companies whose verified
    pdf_url embeds its fiscal year in the FILENAME ('ar_2025_e.pdf') almost
    always host earlier years at the same path ('ar_2016_e.pdf' -- verified live
    for rbc.com back to 2015). For each missing year in the FILING_YEARS window,
    substitute the year, validate with the usual ranged-GET + %PDF sniff, and
    record verified hits. Sources like CSE/TMX use opaque per-filing URLs (no
    year in the filename), so they are naturally skipped.

    BUG FIXED (confirmed corrupting 220 filing_pdfs rows / 32 tickers): the
    "patterned filename" check used to run against the URL's LAST PATH SEGMENT
    INCLUDING its query string. CSE/TMX filing URLs carry a
    '&dateFiled=YYYY-MM-DD' query param, so a URL like thecse.com's
    '.../06408296-...?formDescription=Annual+report&dateFiled=2026-07-02' was
    wrongly treated as filename-patterned (the docstring's own claim that these
    "are naturally skipped" was false). The subsequent whole-URL string
    .replace() then rewrote that harmless date param for each target year,
    producing 5-10 "different year" candidate URLs that all pointed at the
    SAME opaque document -- which validate_pdf correctly confirmed as a real
    PDF every time, so the SAME single filing got recorded as up to 10 distinct
    fabricated fiscal years. Fix: (1) operate on the URL PATH only, never the
    query string, so a coincidental year-shaped query param can't match or be
    rewritten; (2) explicitly exclude opaque per-filing-ID hosts (CSE, TMX/
    QuoteMedia, SEC) from year-probing entirely -- their filenames are hashes/
    serials with no real per-year addressing, so probing is fundamentally
    unsound there even after the query-string fix."""
    cur = store._conn.execute(
        "SELECT ticker, company_name, exchange, fiscal_year, pdf_url "
        "FROM filing_pdfs WHERE pdf_url IS NOT NULL AND pdf_url NOT LIKE '%sec.gov%'")
    by_ticker: dict[str, list[tuple]] = {}
    for tk, name, exch, fy, url in cur.fetchall():
        by_ticker.setdefault(tk, []).append((fy, url, name, exch))

    ua = cfg["crawl"].get("browser_user_agent") or cfg["crawl"].get("user_agent", "Mozilla/5.0")
    timeout = float(cfg["crawl"].get("timeout_seconds", 20))
    current_year = expected_annual_year()
    # DEEP window: probe as far back as the filename pattern yields a valid PDF
    # (content-verified in probe_one). A year with no PDF just produces no hit,
    # so this floor never fabricates -- it's "go as far back as findable".
    targets = set(range(current_year - MAX_HISTORY_YEARS + 1, current_year + 1))
    sem = asyncio.Semaphore(6)
    added = 0

    async def probe_one(tk, template_url, year, name, exch):
        nonlocal added
        async with sem:
            state, _ = await validate_pdf(client, template_url, ua, timeout)
            if state != "ok":
                return
            # A reachable %PDF at the substituted-year URL is only a CANDIDATE.
            # Content-verify (right doc, right year, not interim) before
            # storing, instead of the old blind verified=1 -- a year-pattern
            # URL can resolve to a stale/latest report under an old year's path.
            ok_content, _reason = await looks_like_financial_statement(
                client, template_url, ua, timeout, company_name=name,
                expected_year=year)
            if ok_content:
                await store.upsert_filing_pdfs([dict(
                    ticker=tk, company_name=name, exchange=exch,
                    fiscal_year=year, pdf_url=template_url,
                    discovery_method="year_probe", verified=1)])
                added += 1

    tasks = []
    for tk, rows in by_ticker.items():
        name, exch = rows[0][2], rows[0][3]
        for year, candidate in _year_probe_candidates(rows, targets):
            tasks.append(probe_one(tk, candidate, year, name, exch))
    if tasks:
        if progress:
            progress(f"Year-pattern probe: testing {len(tasks)} candidate URL(s) "
                     f"for missing years ...")
        await asyncio.gather(*tasks)
        if progress:
            progress(f"  Year-pattern probe done: {added} new year(s) verified.")
    return added


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
