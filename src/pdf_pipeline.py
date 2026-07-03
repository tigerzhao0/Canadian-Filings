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


def _rows_to_process(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))  # ensure tables exist
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
    timeout = float(crawl.get("timeout_seconds", 20))
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
                "elapsed": 0.0, "db_path": db_path}

    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    extracted = scanned = failed = done = 0
    t0 = time.monotonic()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        async def handle(row):
            nonlocal extracted, scanned, failed, done
            async with sem:
                pdf_bytes = b""
                # DOWNLOAD to a temp file, read it, then DELETE -- never keep PDFs.
                tmp = None
                try:
                    resp = await client.get(row["pdf_url"], headers={"User-Agent": ua},
                                            timeout=timeout * 2)
                    if resp.status_code == 200 and len(resp.content) <= max_bytes:
                        data = resp.content
                        with tempfile.NamedTemporaryFile(
                                suffix=".pdf", delete=False) as fh:
                            fh.write(data)
                            tmp = fh.name
                        pdf_bytes = Path(tmp).read_bytes()
                except Exception:  # noqa: BLE001 - a dead link shouldn't kill the batch
                    pdf_bytes = b""
                finally:
                    if tmp:
                        try:
                            Path(tmp).unlink()  # DELETE the downloaded file
                        except OSError:
                            pass

                if not pdf_bytes:
                    res_ok, res = False, None
                    reason = "download_failed"
                    sc = 0
                else:
                    res = extract_statements(pdf_bytes)
                    res_ok, reason, sc = res.ok, res.reason, (1 if res.scanned else 0)

                sec = (res.sections if res else {}) or {}
                results.append(dict(
                    ticker=row["ticker"], fiscal_year=row["fiscal_year"],
                    pdf_url=row["pdf_url"], scanned=sc,
                    income_statement=sec.get("income_statement"),
                    balance_sheet=sec.get("balance_sheet"),
                    cash_flow=sec.get("cash_flow"),
                    primary_block=(res.primary_block if res else None),
                    extract_ok=1 if res_ok else 0, reason=reason))
                if res_ok:
                    extracted += 1
                elif sc:
                    scanned += 1
                else:
                    failed += 1
                done += 1
                if progress and (done % 20 == 0 or done == len(rows)):
                    progress(f"  processed {done}/{len(rows)} "
                             f"(ok {extracted}, scanned {scanned}, failed {failed})")

        await asyncio.gather(*(handle(r) for r in rows))

    _bulk_write(db_path, results)
    elapsed = time.monotonic() - t0
    return {"attempted": len(rows), "extracted": extracted, "scanned": scanned,
            "failed": failed, "elapsed": elapsed, "db_path": db_path}


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
