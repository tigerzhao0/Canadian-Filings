"""Tests for the granular download reasons + single retry + per-host UA in
pdf_pipeline._fetch_pdf / _choose_ua.

Runs under pytest, or standalone:  python tests/test_pdf_pipeline_download.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
import pdf_pipeline as pp  # noqa: E402


class FakeResp:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class FakeAsyncClient:
    """Replays a scripted list of outcomes for .get(); each item is a FakeResp
    or an Exception instance to raise."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def get(self, url, headers=None, timeout=None):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _fetch(script, max_bytes=1_000_000):
    client = FakeAsyncClient(script)
    with mock.patch.object(pp.asyncio, "sleep", new=mock.AsyncMock(return_value=None)):
        out = asyncio.run(pp._fetch_pdf(client, "http://x/f.pdf", "UA", timeout=5.0,
                                        max_bytes=max_bytes))
    return out, client


def test_success_first_try():
    (data, reason), client = _fetch([FakeResp(200, b"%PDF-1.4 hello")])
    assert data == b"%PDF-1.4 hello" and reason == ""
    assert client.calls == 1


def test_large_file_success_no_false_timeout():
    # a big (but under max_bytes) PDF succeeds cleanly -- this is the RAY/RY/
    # BGI.UN regression: large real annual reports must not be misreported.
    big = b"%PDF-1.4 " + b"x" * 5_000_000
    (data, reason), _ = _fetch([FakeResp(200, big)], max_bytes=40_000_000)
    assert reason == "" and len(data) == len(big)


def test_timeout_retries_once_then_succeeds():
    (data, reason), client = _fetch(
        [httpx.TimeoutException("t1"), FakeResp(200, b"%PDF ok")])
    assert reason == "" and data == b"%PDF ok"
    assert client.calls == 2, "a transient timeout gets exactly one retry"


def test_timeout_twice_reports_timeout_not_generic():
    (data, reason), client = _fetch(
        [httpx.TimeoutException("t1"), httpx.TimeoutException("t2")])
    assert data == b"" and reason == "timeout"  # not the old opaque 'download_failed'
    assert client.calls == 2


def test_403_is_permanent_no_retry():
    # a real bot-block (e.g. capitalpower.com's WAF) must be reported distinctly
    # and NOT retried (retrying a 403 wastes time -- it won't change).
    (data, reason), client = _fetch([FakeResp(403)])
    assert data == b"" and reason == "http_403"
    assert client.calls == 1


def test_404_is_permanent():
    (data, reason), _ = _fetch([FakeResp(404)])
    assert reason == "http_404"


def test_transient_503_then_success():
    (data, reason), client = _fetch([FakeResp(503), FakeResp(200, b"%PDF ok")])
    assert reason == "" and data == b"%PDF ok"
    assert client.calls == 2


def test_oversized_reported_distinctly():
    (data, reason), _ = _fetch([FakeResp(200, b"x" * 2000)], max_bytes=1000)
    assert data == b"" and reason == "too_large"


def test_empty_body():
    (data, reason), _ = _fetch([FakeResp(200, b"")])
    assert data == b"" and reason == "empty_response"


def test_connection_error_is_download_failed():
    (data, reason), _ = _fetch([httpx.ConnectError("boom"), httpx.ConnectError("boom")])
    assert reason == "download_failed"


def test_choose_ua_sec_gov_gets_declared_bot_ua():
    # SEC actively 403s a "real browser" UA and accepts a declared bot UA
    # (confirmed live against a real sec.gov filing URL). Route sec.gov
    # requests to the SEC-compliant UA, everything else to the browser UA.
    sec_ua = "CanadianARFinder/1.0 (contact: me@example.com)"
    browser = "Mozilla/5.0 Chrome"
    assert pp._choose_ua("https://www.sec.gov/Archives/x.htm", browser, sec_ua) == sec_ua
    assert pp._choose_ua("https://www.rbc.com/x.pdf", browser, sec_ua) == browser
    assert pp._choose_ua("https://www.sec.gov/x.htm", browser, None) == browser


def test_sec_gov_rows_skipped_without_network_call():
    # End-to-end: run_pdf_processing must never call the network for a sec.gov
    # row (it's known to be HTML, not a PDF -- no PDF is needed for SEC/EDGAR
    # cross-listed companies), and must NOT count it as 'failed'.
    import sqlite3
    import tempfile

    db = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db)
    conn.executescript((ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO filing_pdfs (ticker, fiscal_year, pdf_url) VALUES "
        "('SEA', 2025, 'https://www.sec.gov/Archives/edgar/data/1/x.htm'), "
        "('AAA', 2025, 'https://example.com/aaa.pdf')")
    conn.commit()
    conn.close()

    calls = []

    class NoNetworkClient:
        async def get(self, url, headers=None, timeout=None):
            calls.append(url)  # only AAA's URL should ever land here
            return FakeResp(200, b"%PDF-1.4 fake pdf content " + b"x" * 200)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False

    cfg = {"storage": {"db_path": db}, "crawl": {"timeout_seconds": 5},
          "pdf_processing": {"concurrency": 2}}
    with mock.patch.object(pp.httpx, "AsyncClient",
                          lambda **kw: NoNetworkClient()):
        result = asyncio.run(pp.run_pdf_processing(cfg, progress=None))

    assert calls == ["https://example.com/aaa.pdf"], \
        "sec.gov row must never hit the network"
    assert result["sec_skipped"] == 1
    assert result["attempted"] == 1        # SEA excluded from the denominator
    # 'failed' reflects only AAA (its fake content isn't a parseable PDF --
    # unrelated to SEC-skip logic); SEA must NOT be counted in it at all.
    assert result["failed"] == 1
    assert result["extracted"] == 0

    conn = sqlite3.connect(db)
    sea = conn.execute(
        "SELECT extract_ok, reason FROM pdf_extractions WHERE ticker='SEA'").fetchone()
    aaa_reason = conn.execute(
        "SELECT reason FROM pdf_extractions WHERE ticker='AAA'").fetchone()[0]
    conn.close()
    assert sea == (0, "sec_crosslisted_no_pdf_needed")
    assert aaa_reason != "sec_crosslisted_no_pdf_needed"
    Path(db).unlink(missing_ok=True)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
