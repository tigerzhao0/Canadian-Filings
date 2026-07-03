"""Orchestrates structured financials collection via the QuoteMedia API for
TSX/TSXV, CSE/XCNQ, AND NEOE companies.

Pulls 5-year Income Statement / Balance Sheet / Cash Flow data (see
tmx_financials.py) and stores it in its OWN SQLite database (separate from
filings.db, the PDF-finder's DB) since the schemas don't overlap.

CSE/XCNQ coverage: QuoteMedia also indexes CSE-listed companies, but their
plain ticker collides with other exchanges' listings in QuoteMedia's cross-
market database (e.g. bare "QQ" resolved to a stale/wrong exchange tag) --
appending ":CNX" (the exchange's former name, CNSX) disambiguates it.
Empirically validated: a random 100-company sample matched 97/100 correctly
(3 genuine misses; the other "mismatches" were false alarms from a crude
name-matching heuristic -- e.g. "The Yumy Candy Co" vs "Yumy Candy Co", just
the article). Confirmed again on the full run: 550/555 (99.1%).

NEOE coverage: NEO-listed securities trade across several alternative trading
systems (ATSs) -- money.tmx.com itself shows the same company under multiple
symbols (BTQ:OMG, BTQ:TCM, BTQ:LYX, BTQ:CHI, ...), and QuoteMedia returns
identical Company data for all of them. :OMG (Omega ATS) is tried first
(empirically 19/19 real NEOE tickers matched on the first try), with the
others as fallback. See _quotemedia_symbols().

Writes the NORMALIZED 3-level model (see schema_financials.sql):
  - companies:       identity, one row per ticker (name, exchange, currency,
                     primary_source).
  - company_years:   one row per (ticker, fiscal_year) -- the per-year header
                     carrying period_end, currency, and PROVENANCE. `source`
                     is 'tmx_quotemedia' / 'cse_quotemedia' / 'neo_quotemedia'
                     here; a future LLM-PDF or SEC-XBRL year for the same
                     company slots in with its own provenance, no collision.
                     This is the grain a source hands over a whole year at.
  - statement_lines: the numbers, one row per (ticker, year, statement_type,
                     line_item) -- one authoritative value per cell.
  - v_financials:    a view re-flattening all three into the old wide row
                     shape for exports / ad-hoc queries.
Plus tmx_financials_raw (verbatim QuoteMedia JSON per ticker-year, audit
only) and tmx_financials_status (per-company outcome).

Deliberately excluded from the default run.py pipeline (see run.py) since
this structured-data path makes the PDF-based fallbacks (TMX's browser-
driven Filings widget, and CSE's own filings API) unnecessary for the large
majority of these exchanges' companies.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from tmx_financials import TokenExpired, fetch_annual_financials, mint_token

SCHEMA_PATH = Path(__file__).with_name("schema_financials.sql")

TMX_EXCHANGES = {"TSX", "TSXV", "XTSE", "XTSX"}
CSE_EXCHANGES = {"CSE", "XCNQ"}
NEO_EXCHANGES = {"NEOE", "NEO"}
SUPPORTED_EXCHANGES = TMX_EXCHANGES | CSE_EXCHANGES | NEO_EXCHANGES

# NEO-listed securities trade across several alternative trading systems
# (ATSs), and QuoteMedia keys each one as its own symbol suffix -- unlike CSE
# there's no single disambiguator, but the underlying company/financials are
# identical regardless of which venue you pick (confirmed: BTQ:OMG, :TCM,
# :LYX, :CHI all returned byte-identical Company data). :OMG (Omega ATS) was
# empirically the most complete -- 19/19 real NEOE tickers matched on the
# first try -- so it's tried first, with the others as fallback in case some
# ticker isn't quoted there.
NEO_SUFFIXES = ("OMG", "TCM", "LYX", "CHI")


def is_tmx_exchange(exchange: str | None) -> bool:
    """Despite the name (kept for run.py's existing import), this covers
    every exchange run_tmx_financials can fetch structured data for --
    TSX/TSXV directly, CSE/XCNQ via the :CNX suffix, NEOE via an ATS suffix
    (see _quotemedia_symbols). run.py uses this same function both to select
    --financials targets AND to exclude them from the default PDF-finder run."""
    return (exchange or "").strip().upper() in SUPPORTED_EXCHANGES


def _quotemedia_symbols(ticker: str, exchange: str | None) -> list[str]:
    """Candidate QuoteMedia symbols to try, in priority order (stop at the
    first that returns data). CSE/XCNQ tickers need a :CNX suffix to resolve
    to the right company in QuoteMedia's cross-market database -- bare
    symbols collide with other exchanges' listings (confirmed: only matched
    correctly 1/12 without it, and even that one hit was tagged with the
    wrong exchange). NEOE tickers need one of several ATS suffixes (see
    NEO_SUFFIXES). TSX/TSXV tickers are used as-is (that's what QuoteMedia
    expects for them)."""
    ticker = (ticker or "").strip().upper()
    exch = (exchange or "").strip().upper()
    if exch in CSE_EXCHANGES:
        return [f"{ticker}:CNX"]
    if exch in NEO_EXCHANGES:
        return [f"{ticker}:{suf}" for suf in NEO_SUFFIXES]
    return [ticker]


def _source_for_exchange(exchange: str | None) -> str:
    exch = (exchange or "").strip().upper()
    if exch in CSE_EXCHANGES:
        return "cse_quotemedia"
    if exch in NEO_EXCHANGES:
        return "neo_quotemedia"
    return "tmx_quotemedia"


def is_capital_pool_company(ticker: str | None) -> bool:
    """TSXV Capital Pool Companies (CPCs) trade under a .P suffix -- they're
    shell companies raised to later reverse-take-over a private business, so
    they usually have no financial statements to fetch. Still API-called like
    everything else (some do have data); only excluded from the success-rate
    denominator if the call comes back empty."""
    return (ticker or "").strip().upper().endswith(".P")


def is_trust_or_fund(ticker: str | None) -> bool:
    """.UN-suffixed tickers are income trust / fund UNITS, not corporate
    shares (e.g. closed-end funds, infrastructure trusts) -- they don't all
    file the same standard corporate financial statements. Same treatment as
    CPCs: still called, only excluded from the denominator if empty."""
    return (ticker or "").strip().upper().endswith(".UN")


def _non_reporting_category(ticker: str | None) -> str | None:
    """Returns the exclusion category for a ticker that came back with no
    data, or None if it should count as a genuine failure."""
    if is_capital_pool_company(ticker):
        return "capital_pool_company"
    if is_trust_or_fund(ticker):
        return "trust_or_fund"
    return None


def _source_ref(symbol: str) -> str:
    """Provenance string for company_years.source_ref -- identifies exactly
    which call produced a year's data (the actual symbol sent, :CNX/:OMG
    suffix and all, so a CSE/NEO year's provenance is unambiguous), without
    embedding the (secret) token."""
    return f"https://app.quotemedia.com/datatool/getFinancialsEnhancedBySymbol.json?symbol={symbol}"


def _coerce_numeric(value) -> float | None:
    """statement_lines.value is REAL -- skip (store NULL for) anything that
    isn't actually numeric rather than guess at a conversion."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class FinancialsStore:
    """Deliberately has NO per-row async write methods. sqlite3 calls are
    blocking, and calling them one row at a time from inside `async def`
    methods -- even lock-guarded -- halts the ENTIRE event loop for each
    commit, which serializes all the concurrent HTTP fetches behind ~2000
    individual commits. Confirmed empirically: elapsed time was flat
    regardless of concurrency (60 through 300) until writes were batched.
    Callers accumulate plain dicts during the fetch loop and hand them to
    `bulk_upsert_*` ONCE after `asyncio.gather()` completes."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._conn.commit()

    def _bulk_upsert(self, table: str, rows: list[dict], key: tuple[str, ...],
                     stamp_col: str | None = None) -> None:
        """Generic batched upsert helper. `key` is the conflict target; every
        non-key column is updated on conflict. If `stamp_col` is given it's
        appended with the current UTC timestamp."""
        if not rows:
            return
        cols = list(rows[0].keys())
        vals_from = list(cols)
        if stamp_col:
            cols = cols + [stamp_col]
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in key)
        conflict = ", ".join(key)
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
               f"ON CONFLICT({conflict}) DO UPDATE SET {updates}")
        if stamp_col:
            data = [tuple(r[c] for c in vals_from) + (now,) for r in rows]
        else:
            data = [tuple(r[c] for c in vals_from) for r in rows]
        self._conn.executemany(sql, data)
        self._conn.commit()

    def bulk_upsert_companies(self, rows: list[dict]) -> None:
        """Level 1: identity, one row per ticker."""
        self._bulk_upsert("companies", rows, ("ticker",), stamp_col="updated_at")

    def bulk_upsert_company_years(self, rows: list[dict]) -> None:
        """Level 2: per-(ticker, fiscal_year) header + provenance."""
        self._bulk_upsert("company_years", rows, ("ticker", "fiscal_year"),
                          stamp_col="fetched_at")

    def bulk_upsert_statement_lines(self, rows: list[dict]) -> None:
        """Level 3: the numbers, one per (ticker, year, statement, line_item)."""
        self._bulk_upsert("statement_lines", rows,
                          ("ticker", "fiscal_year", "statement_type", "line_item"))

    def bulk_upsert_raw(self, rows: list[dict]) -> None:
        """Secondary/audit write path -- the verbatim QuoteMedia response per
        (ticker, year), for re-derivation/spot-checking. Not canonical; query
        the companies/company_years/statement_lines tables (or v_financials)."""
        if not rows:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cols = list(rows[0].keys()) + ["fetched_at"]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("ticker", "year"))
        sql = (f"INSERT INTO tmx_financials_raw ({', '.join(cols)}) VALUES ({placeholders}) "
               f"ON CONFLICT(ticker, year) DO UPDATE SET {updates}")
        self._conn.executemany(sql, [tuple(r[c] for c in cols[:-1]) + (now,) for r in rows])
        self._conn.commit()

    def bulk_upsert_status(self, rows: list[dict]) -> None:
        if not rows:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cols = list(rows[0].keys()) + ["checked_at"]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "ticker")
        sql = (f"INSERT INTO tmx_financials_status ({', '.join(cols)}) VALUES ({placeholders}) "
               f"ON CONFLICT(ticker) DO UPDATE SET {updates}")
        self._conn.executemany(sql, [tuple(r[c] for c in cols[:-1]) + (now,) for r in rows])
        self._conn.commit()

    def close(self):
        self._conn.close()


class _TokenHolder:
    """Shared, lock-guarded datatool-token so concurrent workers that all hit
    a 403 around the same time trigger exactly one re-mint, not N of them.

    Capped at `max_remints`: a genuinely-invalid ticker (not a token problem
    at all, just a bad/delisted symbol) also 403s, and with no cap each one
    would trigger its own ~5-6s Playwright re-mint -- serialized behind this
    same lock, that turns into a chain that dwarfs the actual fetch time and
    makes raising request concurrency pointless (confirmed: elapsed time was
    flat ~30s from concurrency 60 through 300 until this cap was added). Once
    the budget is used up, further 403s are just recorded as failures/
    exclusions without paying for another mint.
    """

    def __init__(self, token: str, max_remints: int = 2):
        self.token = token
        self._lock = asyncio.Lock()
        self._remints_left = max_remints

    async def refresh(self, stale_token: str) -> str | None:
        async with self._lock:
            if self.token != stale_token:
                return self.token  # someone else already refreshed past this
            if self._remints_left <= 0:
                return None
            new_token = await mint_token()
            self._remints_left -= 1
            if new_token:
                self.token = new_token
                return self.token
            return None


async def run_tmx_financials(companies, cfg, *, progress=None) -> dict:
    """companies: list[Company] already filtered/sampled by the caller (run.py) —
    this function fetches financials for whichever of them are TSX/TSXV/CSE/
    XCNQ and silently skips the rest (defense in depth; run.py already filters).

    EVERY target company gets API-called, including .P (Capital Pool Cos)
    and .UN (trust/fund units) tickers -- some of those do have data. A
    ticker only gets excluded from the success-rate math if it's .P/.UN AND
    comes back with no data (see _non_reporting_category); a non-.P/.UN
    ticker with no data is a genuine failure and counts against the rate.
    """
    fcfg = cfg.get("tmx_financials", {}) or {}
    db_path = fcfg.get("db_path", "financials.db")
    concurrency = int(fcfg.get("concurrency", 10))
    timeout = float(fcfg.get("timeout_seconds", 20))

    targets = [c for c in companies if is_tmx_exchange(c.exchange)]
    n_tmx = sum(1 for c in targets if (c.exchange or "").strip().upper() in TMX_EXCHANGES)
    n_cse = sum(1 for c in targets if (c.exchange or "").strip().upper() in CSE_EXCHANGES)
    n_neo = len(targets) - n_tmx - n_cse
    if progress:
        progress(f"{len(targets)} compan(ies) to fetch financials for "
                 f"({n_tmx} TSX/TSXV, {n_cse} CSE/XCNQ via :CNX, {n_neo} NEOE via "
                 f":OMG/:TCM/:LYX/:CHI; all of them, including .P/.UN) -> {db_path}")
    if not targets:
        return {"total": 0, "resolved": 0, "failed": 0, "excluded_non_reporting": 0,
                "success_rate_pct": 0.0, "elapsed": 0.0, "db_path": db_path,
                "resolved_tickers": set()}

    # The datatool-token is short-lived (confirmed: the same token that worked
    # earlier later 403'd), so mint a fresh one via a real page load before
    # starting, rather than trusting a hardcoded value. See tmx_financials.py.
    if progress:
        progress("Minting a datatool-token (one headless page load)...")
    token = await mint_token()
    if not token:
        if progress:
            progress("Could not mint a datatool-token -- is Playwright installed? "
                     "(pip install playwright && playwright install chromium). "
                     "Aborting financials fetch.")
        return {"total": len(targets), "resolved": 0, "failed": len(targets),
                "excluded_non_reporting": 0, "success_rate_pct": 0.0,
                "elapsed": 0.0, "db_path": db_path, "error": "token_mint_failed",
                "resolved_tickers": set()}

    holder = _TokenHolder(token)
    sem = asyncio.Semaphore(concurrency)
    # httpx defaults to a 100-connection pool regardless of our semaphore size
    # -- raise it to match concurrency or we'd silently cap throughput at 100.
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    resolved = 0
    failed = 0
    excluded = 0
    done = 0
    resolved_tickers: set[str] = set()
    company_rows: list[dict] = []       # level 1: identity
    year_rows: list[dict] = []          # level 2: per-(ticker, year) header
    line_rows: list[dict] = []          # level 3: the numbers
    raw_rows: list[dict] = []
    status_rows: list[dict] = []
    t0 = time.monotonic()

    async with httpx.AsyncClient(limits=limits) as client:
        async def handle(company):
            nonlocal resolved, failed, excluded, done
            async with sem:
                candidates = _quotemedia_symbols(company.ticker, company.exchange)
                cur_token = holder.token
                reports: list[dict] = []
                symbol = candidates[0]
                for symbol in candidates:  # NEOE tries several ATS suffixes
                    for _attempt in range(2):  # 1 retry after a re-mint on 403
                        try:
                            reports = await fetch_annual_financials(
                                client, symbol, token=cur_token,
                                timeout=timeout, raise_on_expired=True)
                            break
                        except TokenExpired:
                            cur_token = await holder.refresh(cur_token)
                            if not cur_token:
                                break
                        except Exception:  # noqa: BLE001 - never let one company kill the batch
                            break
                    if reports:
                        break
                # Pure in-memory accumulation here -- no DB I/O in the hot
                # loop (see FinancialsStore docstring for why that matters).
                if not reports:
                    category = _non_reporting_category(company.ticker)
                    if category:
                        excluded += 1
                        status, reason = category, (
                            f"no financials returned from QuoteMedia -- excluded from "
                            f"success rate ({category.replace('_', ' ')}, not a failure)")
                    else:
                        failed += 1
                        status, reason = "no_data", (
                            "no financials returned from QuoteMedia "
                            "(unlisted symbol / no data on file / request error)")
                    status_rows.append(dict(
                        ticker=company.ticker, company_name=company.legal_name,
                        exchange=company.exchange, status=status, reason=reason))
                else:
                    ref = _source_ref(symbol)
                    source = _source_for_exchange(company.exchange)
                    latest_currency = None
                    for r in reports:
                        if r.get("year") is None:
                            continue
                        if latest_currency is None:  # reports are most-recent-first
                            latest_currency = r["currency"]
                        raw_rows.append(dict(
                            ticker=company.ticker,
                            company_name=company.legal_name,
                            exchange=company.exchange,
                            year=r["year"],
                            period_end=r["period_end"],
                            currency=r["currency"],
                            income_statement=json.dumps(r["income_statement"]),
                            balance_sheet=json.dumps(r["balance_sheet"]),
                            cash_flow=json.dumps(r["cash_flow"]),
                        ))
                        # Level 2: one per-year header row (provenance lives here).
                        year_rows.append(dict(
                            ticker=company.ticker, fiscal_year=r["year"],
                            period_end=r["period_end"], currency=r["currency"],
                            source=source, source_ref=ref))
                        # Level 3: one row per line item.
                        for statement_type in ("income_statement", "balance_sheet", "cash_flow"):
                            for line_item, raw_value in (r[statement_type] or {}).items():
                                line_rows.append(dict(
                                    ticker=company.ticker,
                                    fiscal_year=r["year"],
                                    statement_type=statement_type,
                                    line_item=line_item,
                                    value=_coerce_numeric(raw_value),
                                ))
                    # Level 1: one identity row for the company.
                    company_rows.append(dict(
                        ticker=company.ticker, company_name=company.legal_name,
                        exchange=company.exchange, currency=latest_currency,
                        primary_source=source))
                    resolved += 1
                    resolved_tickers.add(company.ticker)
                    status_rows.append(dict(
                        ticker=company.ticker, company_name=company.legal_name,
                        exchange=company.exchange, status="ok", reason=None))
                done += 1
                if progress and (done % 50 == 0 or done == len(targets)):
                    progress(f"  financials: {done}/{len(targets)} "
                             f"(ok {resolved}, failed {failed}, excluded {excluded})")

        await asyncio.gather(*(handle(c) for c in targets))

    fetch_elapsed = time.monotonic() - t0
    store = FinancialsStore(db_path)
    store.bulk_upsert_companies(company_rows)      # level 1
    store.bulk_upsert_company_years(year_rows)     # level 2
    store.bulk_upsert_statement_lines(line_rows)   # level 3
    store.bulk_upsert_raw(raw_rows)                # audit
    store.bulk_upsert_status(status_rows)          # outcome
    store.close()
    elapsed = time.monotonic() - t0
    if progress:
        progress(f"  fetch phase: {fetch_elapsed:.1f}s, DB write phase: "
                 f"{elapsed - fetch_elapsed:.1f}s")
    success_denominator = resolved + failed  # excludes non-reporting .P/.UN misses
    success_rate_pct = (resolved / success_denominator * 100.0) if success_denominator else 0.0
    return {
        "total": len(targets),
        "resolved": resolved,
        "failed": failed,
        "excluded_non_reporting": excluded,
        "success_rate_pct": success_rate_pct,
        "elapsed": elapsed,
        "db_path": db_path,
        "resolved_tickers": resolved_tickers,
    }
