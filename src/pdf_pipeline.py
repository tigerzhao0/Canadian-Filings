"""run.py --step 2: process the annual-report PDFs the finder located.

For every row in filings.db `filing_pdfs`, this DOWNLOADS the PDF to a temp
file, EXTRACTS its income/balance/cash-flow statement text (pdf_extract.py),
DELETES the file immediately (we never hoard PDFs on disk), and stores the
extracted text in `pdf_extractions`. Text extraction only -- the LLM step that
maps that text into structured financials is a later phase.

Kept separate from the step-1 pipeline because it only concerns the small tail
of companies QuoteMedia doesn't cover (their PDF URLs already sit in
filing_pdfs), and it's slower per company (a real download + parse each).
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from pdf_extract import extract_statements

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


def _ensure_columns(conn) -> None:
    """Additive migration: add columns to an already-existing pdf_extractions
    table (CREATE TABLE IF NOT EXISTS won't alter an existing one)."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(pdf_extractions)")}
    for col, decl in (("doc_type", "TEXT"), ("unit_scale_hint", "REAL")):
        if col not in existing:
            conn.execute(f"ALTER TABLE pdf_extractions ADD COLUMN {col} {decl}")


# Statuses worth one retry (transient) vs permanent (403/404 -> give up now).
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _choose_ua(url: str, browser_ua: str, sec_ua: str | None) -> str:
    """SEC EDGAR (sec.gov) actively 403s a "real browser" User-Agent -- its
    fair-access system wants an honestly-declared bot UA with contact info
    instead (confirmed: browser UA -> 403, declared UA -> 200 on the same URL).
    Everywhere else, use the normal browser UA (some corporate IR CDNs 403 a
    bare/undeclared UA)."""
    if sec_ua and "sec.gov" in (url or "").lower():
        return sec_ua
    return browser_ua


async def _fetch_pdf(client, url: str, ua: str, timeout: float,
                     max_bytes: int) -> tuple[bytes, str]:
    """Download a PDF, returning (bytes, reason). bytes is non-empty only on
    success (reason=""). On failure `reason` is GRANULAR -- http_403/http_404/
    http_<code>/timeout/too_large/download_failed -- instead of one opaque
    bucket, so real bot-blocks are distinguishable from transient network
    blips. Retries ONCE on a transient failure (timeout/429/5xx); a big PDF
    (multi-MB) competing with 5 other concurrent downloads can genuinely time
    out once and succeed on retry. Never raises -- a dead link must not kill
    the batch."""
    headers = {"User-Agent": ua, "Accept": "application/pdf,*/*"}
    last_reason = "download_failed"
    for attempt in range(2):
        try:
            resp = await client.get(url, headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            last_reason = "timeout"
        except Exception:  # noqa: BLE001 - DNS / connection / malformed URL
            last_reason = "download_failed"
        else:
            if resp.status_code == 200:
                if len(resp.content) > max_bytes:
                    return b"", "too_large"
                if not resp.content:
                    return b"", "empty_response"
                return resp.content, ""
            if resp.status_code in _TRANSIENT_STATUS:
                last_reason = f"http_{resp.status_code}"
            else:
                return b"", f"http_{resp.status_code}"  # permanent -> don't retry
        if attempt == 0:
            await asyncio.sleep(1.5)  # brief backoff before the single retry
    return b"", last_reason


def _rows_to_process(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))  # ensure tables exist
        _ensure_columns(conn)
        conn.commit()
        cur = conn.execute(
            "SELECT ticker, fiscal_year, pdf_url FROM filing_pdfs "
            "WHERE pdf_url IS NOT NULL ORDER BY ticker, fiscal_year DESC")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


async def run_pdf_processing(cfg, *, progress=None) -> dict:
    db_path = cfg.get("storage", {}).get("db_path", "output/filings.db")
    crawl = cfg.get("crawl", {})
    ua = crawl.get("browser_user_agent") or crawl.get("user_agent", "Mozilla/5.0")
    sec_ua = (cfg.get("sec", {}) or {}).get("user_agent")  # SEC-compliant UA for sec.gov
    # *3 (not *2): some real annual-report PDFs are 20-30MB, and this competes
    # with `concurrency` other downloads for bandwidth -- give large files room
    # to finish before calling it a timeout.
    timeout = float(crawl.get("timeout_seconds", 20)) * 3
    max_bytes = int(cfg.get("verify", {}).get("max_pdf_mb", 40)) * 1_000_000
    concurrency = int(cfg.get("pdf_processing", {}).get("concurrency", 6))

    rows = _rows_to_process(db_path)
    if progress:
        progress(f"PDF processing: {len(rows)} found PDF(s) in {db_path} "
                 "(download -> extract -> delete) ...")
    if not rows:
        if progress:
            progress("  nothing to process -- filing_pdfs is empty. Run step 1 first "
                     "so the finder populates it.")
        return {"attempted": 0, "extracted": 0, "scanned": 0, "failed": 0,
                "sec_skipped": 0, "elapsed": 0.0, "db_path": db_path}

    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    extracted = scanned = failed = sec_skipped = done = 0
    t0 = time.monotonic()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        async def handle(row):
            nonlocal extracted, scanned, failed, sec_skipped, done
            async with sem:
                url = row["pdf_url"] or ""
                if "sec.gov" in url.lower():
                    # SEC/EDGAR cross-listed companies already have their annual
                    # filing on EDGAR -- this link is an HTML filing document
                    # (10-K/40-F/20-F), never a PDF (confirmed: downloads fine,
                    # content-type text/html). We don't need a PDF for these
                    # companies at all, so don't waste a download attempting one,
                    # and don't count it as a failure -- it was never meant to
                    # extract as a PDF.
                    results.append(dict(
                        ticker=row["ticker"], fiscal_year=row["fiscal_year"],
                        pdf_url=row["pdf_url"], scanned=0,
                        income_statement=None, balance_sheet=None, cash_flow=None,
                        primary_block=None, extract_ok=0,
                        reason="sec_crosslisted_no_pdf_needed", doc_type=None,
                        unit_scale_hint=None))
                    sec_skipped += 1
                    done += 1
                    if progress and (done % 20 == 0 or done == len(rows)):
                        progress(f"  processed {done}/{len(rows)} (ok {extracted}, "
                                 f"scanned {scanned}, failed {failed}, "
                                 f"sec-skipped {sec_skipped})")
                    return
                row_ua = _choose_ua(row["pdf_url"], ua, sec_ua)
                pdf_bytes, dl_reason = await _fetch_pdf(
                    client, row["pdf_url"], row_ua, timeout, max_bytes)
                # DOWNLOAD to a temp file, read it, then DELETE -- never keep PDFs.
                tmp = None
                if pdf_bytes:
                    try:
                        with tempfile.NamedTemporaryFile(
                                suffix=".pdf", delete=False) as fh:
                            fh.write(pdf_bytes)
                            tmp = fh.name
                        pdf_bytes = Path(tmp).read_bytes()
                    except OSError:
                        pass  # keep the in-memory bytes if the temp round-trip fails
                    finally:
                        if tmp:
                            try:
                                Path(tmp).unlink()  # DELETE the downloaded file
                            except OSError:
                                pass

                doc_type = None
                if not pdf_bytes:
                    res_ok, res = False, None
                    reason = dl_reason or "download_failed"
                    sc = 0
                else:
                    res = extract_statements(pdf_bytes)
                    res_ok, reason, sc = res.ok, res.reason, (1 if res.scanned else 0)
                    # Structural document-type check (AIF / MD&A / interim look-alikes
                    # that aren't primary statements) so step 3 can skip/flag them.
                    try:
                        from doc_classify import classify_document
                        doc_type = classify_document(pdf_bytes).doc_type
                    except Exception:  # noqa: BLE001
                        doc_type = None

                sec = (res.sections if res else {}) or {}
                results.append(dict(
                    ticker=row["ticker"], fiscal_year=row["fiscal_year"],
                    pdf_url=row["pdf_url"], scanned=sc,
                    income_statement=sec.get("income_statement"),
                    balance_sheet=sec.get("balance_sheet"),
                    cash_flow=sec.get("cash_flow"),
                    primary_block=(res.primary_block if res else None),
                    extract_ok=1 if res_ok else 0, reason=reason,
                    doc_type=doc_type,
                    unit_scale_hint=(res.unit_scale_hint if res else None)))
                if res_ok:
                    extracted += 1
                elif sc:
                    scanned += 1
                else:
                    failed += 1
                done += 1
                if progress and (done % 20 == 0 or done == len(rows)):
                    progress(f"  processed {done}/{len(rows)} (ok {extracted}, "
                             f"scanned {scanned}, failed {failed}, "
                             f"sec-skipped {sec_skipped})")

        await asyncio.gather(*(handle(r) for r in rows))

    _bulk_write(db_path, results)
    elapsed = time.monotonic() - t0
    # "attempted" = rows genuinely tried as PDF downloads (excludes SEC-skipped,
    # which were never meant to be PDFs -- see the note above). Success rate
    # should be computed against THIS denominator, not len(rows).
    attempted = len(rows) - sec_skipped
    return {"attempted": attempted, "extracted": extracted, "scanned": scanned,
            "failed": failed, "sec_skipped": sec_skipped,
            "elapsed": elapsed, "db_path": db_path}


def _bulk_write(db_path: str, rows: list[dict]) -> None:
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        cols = list(rows[0].keys()) + ["extracted_at"]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols
                            if c not in ("ticker", "fiscal_year"))
        sql = (f"INSERT INTO pdf_extractions ({', '.join(cols)}) VALUES ({placeholders}) "
               f"ON CONFLICT(ticker, fiscal_year) DO UPDATE SET {updates}")
        conn.executemany(sql, [tuple(r[c] for c in cols[:-1]) + (now,) for r in rows])
        conn.commit()
    finally:
        conn.close()
