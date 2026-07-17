"""Tests for the GraphQL-based TMX filings source (tmx_filings.py), which
replaced a Playwright DOM-scrape of money.tmx.com's Filings widget with a
direct call to TMX's own `getCompanyFilings` GraphQL API. Covers: normal
single-window responses feeding into pick_annuals/pick_secondary, the
truncation-detecting quarter-split fallback (observed live against RY, a
high-filing-volume issuer whose routine disclosures flooded a broad
multi-year query before reaching its annual statement), and fail-soft
behaviour on transient/permanent/malformed responses.

Runs under pytest, or standalone:  python tests/test_tmx_filings.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import tmx_filings as tf  # noqa: E402


def _filing(date: str, form_description: str) -> dict:
    url = (f"https://app.quotemedia.com/data/downloadFiling?webmasterId=101020"
           f"&type=PDFC&formDescription={form_description.replace(' ', '+')}"
           f"&dateFiled={date}")
    return {"filingDate": date, "description": "Continuous Disclosure",
            "name": form_description, "urlToPdf": url}


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeAsyncClient:
    """Replays a scripted list of outcomes for .post(); each item is a
    FakeResp or an Exception instance to raise. One entry consumed per call."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _run_async(coro):
    with mock.patch.object(tf.asyncio, "sleep", new=mock.AsyncMock(return_value=None)):
        return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# _graphql_request
# --------------------------------------------------------------------------- #
def test_graphql_request_success():
    payload = {"data": {"filings": [_filing("2026-03-05", "Audited annual financial statements")]}}
    client = FakeAsyncClient([FakeResp(200, payload)])
    urls = _run_async(tf._graphql_request(client, "IPO", "2026-01-01", "2026-12-31",
                                          250, "UA", 20.0))
    assert len(urls) == 1
    assert "downloadFiling" in urls[0]
    assert client.calls == 1


def test_graphql_request_transient_then_success():
    payload = {"data": {"filings": []}}
    client = FakeAsyncClient([FakeResp(503), FakeResp(200, payload)])
    urls = _run_async(tf._graphql_request(client, "IPO", "2026-01-01", "2026-12-31",
                                          250, "UA", 20.0))
    assert urls == []
    assert client.calls == 2, "a transient 503 gets exactly one retry"


def test_graphql_request_permanent_no_retry():
    client = FakeAsyncClient([FakeResp(403)])
    urls = _run_async(tf._graphql_request(client, "IPO", "2026-01-01", "2026-12-31",
                                          250, "UA", 20.0))
    assert urls == []
    assert client.calls == 1, "a permanent error must not be retried"


def test_graphql_request_malformed_json_is_fail_soft():
    client = FakeAsyncClient([FakeResp(200, payload=None)])  # .json() raises
    urls = _run_async(tf._graphql_request(client, "IPO", "2026-01-01", "2026-12-31",
                                          250, "UA", 20.0))
    assert urls == []


def test_graphql_request_network_error_is_fail_soft():
    client = FakeAsyncClient([ConnectionError("boom"), ConnectionError("boom")])
    urls = _run_async(tf._graphql_request(client, "IPO", "2026-01-01", "2026-12-31",
                                          250, "UA", 20.0))
    assert urls == []
    assert client.calls == 2


# --------------------------------------------------------------------------- #
# _year_window -- the RY-style truncation guard
# --------------------------------------------------------------------------- #
def test_year_window_normal_no_truncation():
    payload = {"data": {"filings": [_filing("2026-03-05", "Audited annual financial statements")]}}
    client = FakeAsyncClient([FakeResp(200, payload)])
    urls = _run_async(tf._year_window(client, "IPO", 2026, "UA", 20.0, limit=250))
    assert len(urls) == 1
    assert client.calls == 1, "a normal year needs only one request"


def test_year_window_truncation_triggers_quarter_split():
    # simulate RY: the broad Jan1-Dec31 query returns exactly `limit` rows
    # (signalling truncation), so the caller must re-query as 4 quarters.
    limit = 3
    flooded = {"data": {"filings": [_filing(f"2026-0{i}-01", "News release") for i in range(1, limit + 1)]}}
    q_payloads = [
        {"data": {"filings": []}},
        {"data": {"filings": [_filing("2026-03-05", "Audited annual financial statements")]}},
        {"data": {"filings": []}},
        {"data": {"filings": []}},
    ]
    client = FakeAsyncClient([FakeResp(200, flooded)] + [FakeResp(200, p) for p in q_payloads])
    urls = _run_async(tf._year_window(client, "RY", 2026, "UA", 20.0, limit=limit))
    assert client.calls == 5, "1 broad query + 4 quarterly re-queries"
    assert len(urls) == 1
    assert "Audited" in urls[0] or "audited" in urls[0].lower()


# --------------------------------------------------------------------------- #
# annual_statements_via_api -- end to end
# --------------------------------------------------------------------------- #
def test_annual_statements_via_api_finds_strict_annual_match():
    payload_2026 = {"data": {"filings": [_filing("2026-03-05", "Audited annual financial statements")]}}
    payload_2025 = {"data": {"filings": [_filing("2025-03-14", "Audited annual financial statements")]}}
    client = FakeAsyncClient([FakeResp(200, payload_2026), FakeResp(200, payload_2025)])
    out = _run_async(tf.annual_statements_via_api(
        client, "IPO", "UA", 20.0, years=2, current_year=2026))
    assert len(out) == 2
    assert {c["year"] for c in out} == {2025, 2026}


def test_annual_statements_via_api_falls_back_to_secondary():
    # no strict "annual + financial statement" match anywhere, only an
    # unlabelled "audited financial statements" -- pick_secondary should catch it.
    payload = {"data": {"filings": [_filing("2026-03-05", "Audited financial statements")]}}
    client = FakeAsyncClient([FakeResp(200, payload)])
    out = _run_async(tf.annual_statements_via_api(
        client, "XYZ", "UA", 20.0, years=1, current_year=2026))
    assert len(out) == 1
    assert out[0]["year"] == 2026


def test_annual_statements_via_api_empty_across_all_years():
    client = FakeAsyncClient([FakeResp(200, {"data": {"filings": []}}) for _ in range(3)])
    out = _run_async(tf.annual_statements_via_api(
        client, "NEWCO", "UA", 20.0, years=3, current_year=2026))
    assert out == []


# --------------------------------------------------------------------------- #
# reused classification helpers (unchanged from the old DOM-scraping path)
# --------------------------------------------------------------------------- #
def test_pick_annuals_excludes_interim():
    links = [
        _filing("2026-03-05", "Audited annual financial statements")["urlToPdf"],
        _filing("2026-08-01", "Interim financial statements")["urlToPdf"],
    ]
    out = tf.pick_annuals(links)
    assert len(out) == 1
    assert out[0]["year"] == 2026


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
