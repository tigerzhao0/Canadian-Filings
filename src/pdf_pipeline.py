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
import json
import multiprocessing as mp
import os
import sqlite3
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import httpx

from pdf_extract import extract_statements

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


def _isolated_worker(fn, args: tuple, result_queue) -> None:
    """Runs INSIDE a freshly-spawned, single-use process (see _run_isolated)."""
    try:
        result_queue.put(("ok", fn(*args)))
    except Exception as e:  # noqa: BLE001 -- report, don't crash the child silently
        result_queue.put(("error", repr(e)))


async def _run_isolated(fn, args: tuple, timeout: float):
    """Run fn(*args) in a DEDICATED, single-use process with a genuine
    kill-on-timeout -- NOT a shared ProcessPoolExecutor.

    A shared pool can't do this safely: concurrent.futures.ProcessPoolExecutor
    has no supported way to kill ONE misbehaving worker without poisoning the
    whole pool (any unexpected worker death is treated as fatal to every
    other pending/future task on it) -- confirmed the hard way. A prior
    version tried working around this by recreating the whole pool on a
    timeout (cancelling everything else queued on it), which cascaded into
    49% of a real ~23,454-PDF run coming back as false parse_timeouts
    (innocent PDFs orphaned by the recycle, not actually slow). Removing the
    recycle entirely was tried next and was WORSE: without any way to
    reclaim a stuck worker's slot, multiple genuinely pathological PDFs (not
    just the rare one) permanently consumed workers over the course of a
    long run, shrinking the pool's real capacity until everything still
    queued behind it also blew past the timeout waiting for a free slot.

    A dedicated process per call sidesteps both failure modes: killing it
    can never affect any other in-flight PDF, because there is no other work
    sharing it. The cost is a process spawn per call (~50-150ms measured on
    Windows) -- worth it against _page_texts() (~1-2s typical, benchmarked at
    ~95% of a PDF's CPU cost), and it's the only way to actually reclaim a
    slot from a truly stuck PDF instead of losing it forever.

    CRITICAL ordering: the result MUST be read from the queue before (or
    concurrently with) proc.join(), never strictly after. A result larger
    than the OS pipe buffer (~64KB on Windows -- a real annual report's
    extracted text/sections easily exceeds this) makes the child block
    inside queue.put() until the parent reads it; joining first waits for
    the child to exit, which it can't do while blocked on put() -- a
    textbook multiprocessing deadlock (Python's own docs warn about this
    exact ordering). Confirmed the hard way: a real run showed multiple
    worker processes' CPU time completely FLAT (zero growth) for 19+
    minutes straight -- not slow computation (which would show CPU still
    climbing), but two sides each waiting on the other forever. Fixed by
    polling the queue (via a short-timeout blocking get in a thread) in the
    SAME loop that watches the deadline, so a result is drained the moment
    it arrives, well before any join is attempted."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_isolated_worker, args=(fn, args, q))
    proc.start()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    result = None
    while loop.time() < deadline:
        try:
            result = await loop.run_in_executor(None, q.get, True, 0.1)
            break
        except Exception:  # noqa: BLE001 -- queue.Empty (or similar): keep polling
            if not proc.is_alive():
                try:
                    result = q.get_nowait()
                except Exception:  # noqa: BLE001
                    result = None
                break

    if result is None:
        proc.terminate()
        await loop.run_in_executor(None, proc.join, 5)
        if proc.is_alive():
            proc.kill()
        await loop.run_in_executor(None, proc.join, 5)
        q.close()
        return None

    await loop.run_in_executor(None, proc.join, 5)
    if proc.is_alive():
        proc.terminate()
        await loop.run_in_executor(None, proc.join, 5)
    q.close()
    status, payload = result
    return payload if status == "ok" else None


def _compress_pages(pages: list[str] | None) -> bytes | None:
    if not pages:
        return None
    return zlib.compress(json.dumps(pages).encode("utf-8"), 9)


def _decompress_pages(blob: bytes | None) -> list[str] | None:
    if not blob:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def _parse_pdf(pdf_bytes: bytes) -> dict:
    """All CPU-bound PDF parsing for one document, done in ONE pass and
    intended to run in a dedicated worker PROCESS (see _run_isolated, used by
    run_pdf_processing) rather than inline on the asyncio event loop.

    Two independent wins over the old inline approach:
      1. pdfplumber's per-page text extraction (_page_texts) is computed ONCE
         here and shared across extract_statements / classify_document /
         content_fiscal_years, instead of each of those three independently
         re-parsing the same PDF from scratch (~3-4x redundant pdfplumber
         work per PDF before this change, regardless of parallelism).
      2. Running in a separate OS process (not just a thread) gets real
         multi-core parallelism for this pure-Python, CPU-heavy parsing --
         threads would still serialize on the GIL. Called via
         _run_isolated(_parse_pdf, (pdf_bytes,), timeout), which also keeps
         the event loop free so OTHER companies' downloads keep
         proceeding concurrently while this one parses.

    Returns a plain (picklable) dict -- deliberately not the ExtractResult/
    DocClass dataclasses, to keep the cross-process payload simple.

    Also returns `pages_compressed`: the raw per-page text (pre-boundary-trim,
    pre-furniture-strip), zlib-compressed, so run_pdf_processing can cache it
    in pdf_raw_pages -- letting a later tweak to pdf_extract.py's OWN logic
    (furniture-stripping, notes-boundary, statement-slicing -- ~5% of a PDF's
    CPU cost) be replayed via _reparse_from_cache without re-downloading or
    re-running _page_texts() (~95% of the cost, benchmarked). Compressed HERE
    (in the worker) so only the smaller blob crosses process-pool IPC."""
    from pdf_extract import _page_texts, content_fiscal_years
    from doc_classify import classify_document

    pages = _page_texts(pdf_bytes)
    res = extract_statements(pdf_bytes, pages=pages)
    try:
        doc_type = classify_document(pdf_bytes, pages=pages).doc_type
    except Exception:  # noqa: BLE001
        doc_type = None
    try:
        content_years, true_fy, is_interim = content_fiscal_years(
            pdf_bytes, pages=pages, res=res)
    except Exception:  # noqa: BLE001
        content_years, true_fy, is_interim = [], None, False
    return {
        "ok": res.ok, "reason": res.reason, "scanned": res.scanned,
        "sections": dict(res.sections), "primary_block": res.primary_block,
        "unit_scale_hint": res.unit_scale_hint,
        "doc_type": doc_type, "content_years": content_years,
        "true_fy": true_fy, "is_interim": is_interim,
        "pages_compressed": _compress_pages(pages),
    }


def _reparse_from_cache(pages: list[str]) -> dict:
    """Same result shape as _parse_pdf, but replays pdf_extract.py's logic
    against ALREADY-EXTRACTED page text instead of raw PDF bytes -- skips
    _page_texts() entirely (no PDF library work, no download). Used by
    run_pdf_processing_from_cache for fast re-scans after a tweak to
    boundary-detection/furniture-stripping/slicing."""
    from pdf_extract import content_fiscal_years
    from doc_classify import classify_document

    res = extract_statements(b"%PDF-cached", pages=pages)
    try:
        doc_type = classify_document(b"%PDF-cached", pages=pages).doc_type
    except Exception:  # noqa: BLE001
        doc_type = None
    try:
        content_years, true_fy, is_interim = content_fiscal_years(
            b"%PDF-cached", pages=pages, res=res)
    except Exception:  # noqa: BLE001
        content_years, true_fy, is_interim = [], None, False
    return {
        "ok": res.ok, "reason": res.reason, "scanned": res.scanned,
        "sections": dict(res.sections), "primary_block": res.primary_block,
        "unit_scale_hint": res.unit_scale_hint,
        "doc_type": doc_type, "content_years": content_years,
        "true_fy": true_fy, "is_interim": is_interim,
    }


def _parse_pdf_path(path: str) -> dict:
    """Thin wrapper: reads the PDF from a file PATH inside the worker process
    and hands off to _parse_pdf. Used instead of passing raw bytes through
    the process pool's IPC -- see the call site in run_pdf_processing for why
    (large multi-MB payloads through Windows named pipes under concurrency
    exhausts OS pipe resources; a path is a few bytes)."""
    try:
        pdf_bytes = Path(path).read_bytes()
    except OSError:
        pdf_bytes = b""
    return _parse_pdf(pdf_bytes)


def _ensure_columns(conn) -> None:
    """Additive migration: add columns to an already-existing pdf_extractions
    table (CREATE TABLE IF NOT EXISTS won't alter an existing one)."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(pdf_extractions)")}
    for col, decl in (("doc_type", "TEXT"), ("unit_scale_hint", "REAL"),
                      ("content_years", "TEXT"), ("true_fiscal_year", "INTEGER")):
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


def _rows_to_process(db_path: str, tickers: set[str] | None = None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))  # ensure tables exist
        _ensure_columns(conn)
        conn.commit()
        sql = ("SELECT ticker, fiscal_year, pdf_url FROM filing_pdfs "
               "WHERE pdf_url IS NOT NULL")
        params: tuple = ()
        if tickers:
            placeholders = ",".join("?" * len(tickers))
            sql += f" AND ticker IN ({placeholders})"
            params = tuple(tickers)
        sql += " ORDER BY ticker, fiscal_year DESC"
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


async def run_pdf_processing(cfg, *, tickers: set[str] | None = None, progress=None) -> dict:
    db_path = cfg.get("storage", {}).get("db_path", "output/filings.db")
    crawl = cfg.get("crawl", {})
    ua = crawl.get("browser_user_agent") or crawl.get("user_agent", "Mozilla/5.0")
    sec_ua = (cfg.get("sec", {}) or {}).get("user_agent")  # SEC-compliant UA for sec.gov
    # *3 (not *2): some real annual-report PDFs are 20-30MB, and this competes
    # with `concurrency` other downloads for bandwidth -- give large files room
    # to finish before calling it a timeout.
    timeout = float(crawl.get("timeout_seconds", 20)) * 3
    max_bytes = int(cfg.get("verify", {}).get("max_pdf_mb", 40)) * 1_000_000
    # Download concurrency: plain I/O waits, so this can be higher than the
    # parse concurrency below -- but each in-flight download still holds a
    # full PDF (sometimes 20-30MB) in memory, so it's not free to raise
    # arbitrarily either.
    concurrency = int(cfg.get("pdf_processing", {}).get("concurrency", 8))
    # Parse concurrency: DELIBERATELY capped low (not os.cpu_count()) -- this
    # is memory-bound, not just CPU-bound. Each worker process holds
    # pdfplumber's full parsed-page representation of one PDF at a time,
    # which for a large multi-hundred-page annual report can run into the
    # hundreds of MB to over a GB; running one process per logical core
    # (e.g. 20 on a 10-core/20-thread machine) can push total memory well
    # past what a typical 16-32GB desktop has, especially alongside the
    # download concurrency above holding its own PDFs in flight. Raise this
    # only if you've confirmed you have the RAM headroom for
    # concurrency-many downloads + process_workers-many in-progress parses
    # simultaneously.
    process_workers = int(cfg.get("pdf_processing", {}).get(
        "process_workers", min(4, os.cpu_count() or 4)))
    # Wall-clock cap on the CPU-bound parse step, separate from the download
    # timeout above. Observed on a full-backlog run (~23,454 PDFs): one PDF
    # (a large multinational's dense annual report) sent a worker process
    # into a 2.9+ hour spin (vs ~45min for sibling workers doing similar
    # volume) with no exception ever raised -- run_in_executor has no timeout
    # of its own, so the coroutine just never completed. With 99.95% of rows
    # already done, this single stuck row blocked
    # process_pool.shutdown(wait=True) from ever returning, stalling
    # completion of the entire run. 900s (15min) comfortably covers even very
    # long real annual reports (observed worst-case sibling: ~45min for a
    # whole WORKER's batch, not one PDF).
    parse_timeout = float(cfg.get("pdf_processing", {}).get(
        "parse_timeout_seconds", 900))

    rows = _rows_to_process(db_path, tickers)
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
    raw_page_rows: list[dict] = []
    extracted = scanned = failed = sec_skipped = done = 0
    t0 = time.monotonic()
    loop = asyncio.get_running_loop()
    flush_lock = asyncio.Lock()
    # Bounds how many _run_isolated (one-process-per-PDF) calls run at once --
    # see _run_isolated's docstring for why this replaced a shared
    # ProcessPoolExecutor (killing one stuck worker in a shared pool poisons
    # the whole pool; recycling the whole pool on a timeout cascaded into 49%
    # false failures on a real run; not recycling at all let multiple
    # genuinely-stuck PDFs permanently eat workers until the pool starved).
    parse_sem = asyncio.Semaphore(process_workers)

    async def _maybe_flush():
        # Write completed rows to the DB as we go (same cadence as the
        # progress print below) instead of only once at the very end --
        # otherwise stopping a long run mid-way loses ALL completed work,
        # not just what's left to do, and there's no way to see real
        # progress in the DB while it's running.
        if done % 20 == 0 or done == len(rows):
            async with flush_lock:
                if results:
                    _bulk_write(db_path, results)
                    results.clear()
                if raw_page_rows:
                    _bulk_write_raw_pages(db_path, raw_page_rows)
                    raw_page_rows.clear()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        async def handle(row):
            nonlocal extracted, scanned, failed, sec_skipped, done
            url = row["pdf_url"] or ""
            if "sec.gov" in url.lower():
                # SEC/EDGAR cross-listed companies already have their annual
                # filing on EDGAR -- this link is an HTML filing document
                # (10-K/40-F/20-F), never a PDF (confirmed: downloads fine,
                # content-type text/html). We don't need a PDF for these
                # companies at all, so don't waste a download attempting one,
                # and don't count it as a failure -- it was never meant to
                # extract as a PDF. No network/CPU work at all, so no need
                # for the download semaphore below.
                results.append(dict(
                    ticker=row["ticker"], fiscal_year=row["fiscal_year"],
                    pdf_url=row["pdf_url"], scanned=0,
                    income_statement=None, balance_sheet=None, cash_flow=None,
                    primary_block=None, extract_ok=0,
                    reason="sec_crosslisted_no_pdf_needed", doc_type=None,
                    unit_scale_hint=None, content_years=None,
                    true_fiscal_year=None))
                sec_skipped += 1
                done += 1
                if progress and (done % 20 == 0 or done == len(rows)):
                    progress(f"  processed {done}/{len(rows)} (ok {extracted}, "
                             f"scanned {scanned}, failed {failed}, "
                             f"sec-skipped {sec_skipped})")
                await _maybe_flush()
                return
            row_ua = _choose_ua(row["pdf_url"], ua, sec_ua)
            # Download concurrency is gated by `sem` ONLY for the download
            # itself -- released as soon as the PDF is on disk, BEFORE
            # submitting to the process pool. Previously `sem` wrapped the
            # download AND the parse-and-wait, so a row mid-parse kept
            # occupying a download slot the whole time; with 20 parse
            # workers and a similar-sized download semaphore, most slots
            # ended up held by in-progress parses instead of active
            # downloads, throttling the download pipeline even though the
            # network had spare capacity. Splitting them lets downloads
            # keep streaming in continuously while parses queue
            # independently on the process pool's own (unbounded, cheap --
            # a queued item is just a path string) internal queue.
            async with sem:
                pdf_bytes, dl_reason = await _fetch_pdf(
                    client, row["pdf_url"], row_ua, timeout, max_bytes)
                # DOWNLOAD to a temp file. Kept alive until the worker process
                # has read it (below) -- never held longer than that, and
                # always DELETED before this row finishes either way.
                tmp = None
                if pdf_bytes:
                    try:
                        with tempfile.NamedTemporaryFile(
                                suffix=".pdf", delete=False) as fh:
                            fh.write(pdf_bytes)
                            tmp = fh.name
                    except OSError:
                        tmp = None  # fall back to the in-memory bytes below

            doc_type = None
            content_years: list[int] = []
            true_fy = None
            sec: dict = {}
            primary_block = None
            unit_scale_hint = None
            if not pdf_bytes:
                res_ok = False
                reason = dl_reason or "download_failed"
                sc = 0
            else:
                # Offload to a DEDICATED, single-use process (see
                # _run_isolated) -- real multi-core parallelism (a thread
                # would still serialize on the GIL) plus a genuine
                # kill-on-timeout no shared pool can safely offer. Pass the
                # temp file PATH, not the raw bytes, when available --
                # marshaling multi-MB PDF payloads through process IPC
                # (Windows named pipes) for every PDF exhausts OS pipe
                # resources once ~15-20 large PDFs are in flight at once
                # (observed: WinError 1450). A path is a few bytes; the
                # worker reads the file itself instead.
                async with parse_sem:
                    try:
                        if tmp:
                            parsed = await _run_isolated(
                                _parse_pdf_path, (tmp,), parse_timeout)
                        else:
                            parsed = await _run_isolated(
                                _parse_pdf, (pdf_bytes,), parse_timeout)
                    finally:
                        if tmp:
                            try:
                                Path(tmp).unlink()  # DELETE the downloaded file
                            except OSError:
                                pass
                timed_out = parsed is None
                if timed_out:
                    res_ok, reason, sc = False, "parse_timeout", 0
                    doc_type = None
                    content_years, true_fy = [], None
                else:
                    res_ok, reason, sc = (parsed["ok"], parsed["reason"],
                                         1 if parsed["scanned"] else 0)
                    doc_type = parsed["doc_type"]
                    content_years = parsed["content_years"]
                    true_fy = parsed["true_fy"]
                    is_interim = parsed["is_interim"]
                    sec = parsed["sections"] or {}
                    primary_block = parsed["primary_block"]
                    unit_scale_hint = parsed["unit_scale_hint"]
                    if parsed.get("pages_compressed"):
                        raw_page_rows.append(dict(
                            ticker=row["ticker"], fiscal_year=row["fiscal_year"],
                            pdf_url=row["pdf_url"],
                            pages_compressed=parsed["pages_compressed"]))
                    labeled = row["fiscal_year"]
                    # Reject a quarterly/interim doc, or one whose content
                    # year is WILDLY off its label (>1yr) -- a wrong document
                    # for this row.
                    if res_ok and is_interim:
                        res_ok, reason = False, "interim_or_quarterly_report"
                    elif (res_ok and content_years and labeled is not None
                          and not any(abs(y - labeled) <= 1 for y in content_years)):
                        res_ok, reason = False, f"wrong_year_content_{content_years[0]}"
            # The STORAGE fiscal_year (== raw_line_items.pdf_year) is left as
            # the original filing-year label ON PURPOSE: schema_map's merge
            # processes newest-pdf_year-first so the newest FILING wins each
            # col_year slot (restated figures), and the filing-year label is
            # the better recency proxy -- relabeling to the content year
            # would let two distinct reports collide on one pdf_year and
            # break that tie-break. The actual data is col_year-keyed and
            # content-derived regardless. content_years/true_fiscal_year are
            # stored as informational: coverage (P4) reads them, and the
            # reject logic above uses them to drop wrong-year/interim docs.
            results.append(dict(
                ticker=row["ticker"], fiscal_year=row["fiscal_year"],
                pdf_url=row["pdf_url"], scanned=sc,
                income_statement=sec.get("income_statement"),
                balance_sheet=sec.get("balance_sheet"),
                cash_flow=sec.get("cash_flow"),
                primary_block=primary_block,
                extract_ok=1 if res_ok else 0, reason=reason,
                doc_type=doc_type,
                unit_scale_hint=unit_scale_hint,
                content_years=(",".join(str(y) for y in content_years)
                               if content_years else None),
                true_fiscal_year=true_fy))
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
            await _maybe_flush()

        await asyncio.gather(*(handle(r) for r in rows))

    _bulk_write(db_path, results)
    _bulk_write_raw_pages(db_path, raw_page_rows)
    elapsed = time.monotonic() - t0
    # "attempted" = rows genuinely tried as PDF downloads (excludes SEC-skipped,
    # which were never meant to be PDFs -- see the note above). Success rate
    # should be computed against THIS denominator, not len(rows).
    attempted = len(rows) - sec_skipped
    return {"attempted": attempted, "extracted": extracted, "scanned": scanned,
            "failed": failed, "sec_skipped": sec_skipped,
            "elapsed": elapsed, "db_path": db_path}


def _rows_from_cache(db_path: str, tickers: set[str] | None = None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        sql = "SELECT ticker, fiscal_year, pdf_url, pages_compressed FROM pdf_raw_pages"
        params: tuple = ()
        if tickers:
            placeholders = ",".join("?" * len(tickers))
            sql += f" WHERE ticker IN ({placeholders})"
            params = tuple(tickers)
        sql += " ORDER BY ticker, fiscal_year DESC"
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


async def run_pdf_processing_from_cache(cfg, *, tickers: set[str] | None = None,
                                        progress=None) -> dict:
    """Replay pdf_extract.py's boundary-detection/furniture-stripping/
    statement-slicing logic against the pdf_raw_pages cache -- no download, no
    PDF-library re-parse (_page_texts() is ~95% of a PDF's CPU cost,
    benchmarked; this skips it entirely). For iterating on a tweak to that
    downstream logic once run_pdf_processing has already populated the cache.
    Rows with nothing cached yet (never processed since the cache was added)
    are silently skipped -- run the normal run_pdf_processing first."""
    db_path = cfg.get("storage", {}).get("db_path", "output/filings.db")
    process_workers = int(cfg.get("pdf_processing", {}).get(
        "process_workers", min(20, os.cpu_count() or 4)))
    parse_timeout = float(cfg.get("pdf_processing", {}).get(
        "parse_timeout_seconds", 900))

    rows = _rows_from_cache(db_path, tickers)
    if progress:
        progress(f"PDF re-parse (from cache): {len(rows)} cached page-set(s) "
                 f"in {db_path} ...")
    if not rows:
        if progress:
            progress("  nothing cached -- run run_pdf_processing (--step 2) first "
                     "so it populates pdf_raw_pages.")
        return {"attempted": 0, "extracted": 0, "scanned": 0, "failed": 0,
                "elapsed": 0.0, "db_path": db_path}

    results: list[dict] = []
    extracted = scanned = failed = done = 0
    t0 = time.monotonic()
    flush_lock = asyncio.Lock()
    # See _run_isolated's docstring: one dedicated process per call, not a
    # shared pool -- a shared pool has no safe way to kill just one stuck
    # worker (recycling the whole pool cascaded into 49% false failures on a
    # real run; not recycling let multiple stuck PDFs permanently starve it).
    parse_sem = asyncio.Semaphore(process_workers)

    async def _maybe_flush():
        if done % 200 == 0 or done == len(rows):
            async with flush_lock:
                if results:
                    _bulk_write(db_path, results)
                    results.clear()

    async def handle(row):
        nonlocal extracted, scanned, failed, done
        pages = _decompress_pages(row["pages_compressed"])
        if not pages:
            done += 1
            await _maybe_flush()
            return
        async with parse_sem:
            parsed = await _run_isolated(_reparse_from_cache, (pages,), parse_timeout)
        timed_out = parsed is None

        if timed_out:
            res_ok, reason, sc = False, "parse_timeout", 0
            doc_type, content_years, true_fy = None, [], None
            sec, primary_block, unit_scale_hint = {}, None, None
        else:
            res_ok, reason, sc = (parsed["ok"], parsed["reason"],
                                 1 if parsed["scanned"] else 0)
            doc_type = parsed["doc_type"]
            content_years = parsed["content_years"]
            true_fy = parsed["true_fy"]
            sec = parsed["sections"] or {}
            primary_block = parsed["primary_block"]
            unit_scale_hint = parsed["unit_scale_hint"]
            labeled = row["fiscal_year"]
            if res_ok and parsed["is_interim"]:
                res_ok, reason = False, "interim_or_quarterly_report"
            elif (res_ok and content_years and labeled is not None
                  and not any(abs(y - labeled) <= 1 for y in content_years)):
                res_ok, reason = False, f"wrong_year_content_{content_years[0]}"

        results.append(dict(
            ticker=row["ticker"], fiscal_year=row["fiscal_year"],
            pdf_url=row["pdf_url"], scanned=sc,
            income_statement=sec.get("income_statement"),
            balance_sheet=sec.get("balance_sheet"),
            cash_flow=sec.get("cash_flow"),
            primary_block=primary_block,
            extract_ok=1 if res_ok else 0, reason=reason,
            doc_type=doc_type,
            unit_scale_hint=unit_scale_hint,
            content_years=(",".join(str(y) for y in content_years)
                           if content_years else None),
            true_fiscal_year=true_fy))
        if res_ok:
            extracted += 1
        elif sc:
            scanned += 1
        else:
            failed += 1
        done += 1
        if progress and (done % 500 == 0 or done == len(rows)):
            progress(f"  reparsed {done}/{len(rows)} (ok {extracted}, "
                     f"scanned {scanned}, failed {failed})")
        await _maybe_flush()

    await asyncio.gather(*(handle(r) for r in rows))

    _bulk_write(db_path, results)
    elapsed = time.monotonic() - t0
    return {"attempted": len(rows), "extracted": extracted, "scanned": scanned,
            "failed": failed, "elapsed": elapsed, "db_path": db_path}


def _bulk_write_raw_pages(db_path: str, rows: list[dict]) -> None:
    """Cache each PDF's raw per-page text (see pdf_raw_pages in schema.sql)."""
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executemany(
            "INSERT INTO pdf_raw_pages (ticker, fiscal_year, pdf_url, "
            "pages_compressed, cached_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(ticker, fiscal_year) DO UPDATE SET "
            "pdf_url=excluded.pdf_url, pages_compressed=excluded.pages_compressed, "
            "cached_at=excluded.cached_at",
            [(r["ticker"], r["fiscal_year"], r["pdf_url"], r["pages_compressed"], now)
             for r in rows])
        conn.commit()
    finally:
        conn.close()


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
