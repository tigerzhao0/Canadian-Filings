"""GuruFocus financials API client (the repo's first GuruFocus integration).

Thin, synchronous httpx client for the GuruFocus per-stock financials endpoint

    GET https://api.gurufocus.com/public/user/{api_token}/stock/{EXCH:SYM}/financials
        ?order=desc[&target_currency=CAD]

The token is a PATH segment (not an Authorization header) -- this is GuruFocus's
established API, whose response is the `financials.annuals` block (Fiscal Year[] +
income_statement/balance_sheet/cashflow_statement/per_share_data_array dicts) that
gurufocus_compare.parse_gf already understands.

Used by gurufocus_compare.py to fetch the "ground-truth" fundamentals we diff our
PDF-extracted numbers against. Deliberately small: no async fan-out (the compare
tool's default scope is a ~40-name sample, and each call is a *paid* request, so we
optimise for cache reuse over throughput).

Design points that mirror the rest of the repo:
- httpx, not requests (the user's snippet used requests, but httpx is the repo's HTTP
  lib -- keeping one dependency).
- Hand-rolled exponential backoff with jitter on transient errors (cf.
  search_provider.py / pipeline.py), NOT tenacity (declared-but-unused in the repo).
- A disk cache under output/gurufocus_cache/ so the many offline label-mapping
  refinement passes never re-spend API calls. A 404 / empty payload is cached as a
  "no coverage" marker (expected for most TSXV/CSE microcaps) so we don't re-request
  a name GuruFocus simply doesn't carry.

The response is returned verbatim (a dict) -- parsing/aligning into fiscal years is
gurufocus_compare.py's job.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import httpx

# pdf_financials.db exchange code -> the exchange prefix GuruFocus expects in
# its {exchange:symbol} path. TSX/TSXV pass through; the CSE is "XCNQ" in our
# GuruFocus xlsx input but "CSE" in GuruFocus's own symbology, and NEO Exchange
# is "NEOE" for us but "NEO" for them.
EXCHANGE_MAP = {
    "TSX": "TSX",
    "TSXV": "TSXV",
    "XCNQ": "CSE",
    "NEOE": "NEO",
}

# The token goes in the PATH ({api_token}), so BASE_URL is templated per request.
BASE_URL = "https://api.gurufocus.com/public/user"
# A polite, identifiable UA. Not an auth mechanism -- the token in the URL path is.
USER_AGENT = "Canadian-Filings-QA/1.0 (financial-data reconciliation)"

_NO_COVERAGE = "_gf_no_coverage"


class GuruFocusError(RuntimeError):
    """A non-recoverable API error (bad token, exhausted retries)."""


def gf_symbol(exchange: str, ticker: str) -> str:
    """Build the GuruFocus '{EXCHANGE}:{SYMBOL}' path segment for one company.

    Dot suffixes (class/unit shares like BBD.B, AD.UN) are preserved -- that's how
    GuruFocus keys them too. Unknown exchanges are passed through uppercased rather
    than dropped, so a stray code still produces a requestable symbol.
    """
    exch = EXCHANGE_MAP.get((exchange or "").upper(), (exchange or "").upper())
    return f"{exch}:{ticker.strip().upper()}"


def _cache_path(cache_dir: Path, exchange: str, ticker: str) -> Path:
    # Filesystem-safe: ':' and other separators can't appear in the raw pieces we
    # use here (exchange codes and tickers are [A-Z0-9.]), but normalise ' ' and
    # '/' defensively.
    safe = f"{(exchange or 'NA').upper()}_{ticker.strip().upper()}"
    safe = safe.replace("/", "-").replace(" ", "")
    return cache_dir / f"{safe}.json"


def load_cached(cache_dir: Path, exchange: str, ticker: str) -> Optional[dict]:
    """Return a previously cached response dict (or no-coverage marker), else None."""
    p = _cache_path(Path(cache_dir), exchange, ticker)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(cache_dir: Path, exchange: str, ticker: str, payload: dict) -> None:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache_path(cache_dir, exchange, ticker).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def is_no_coverage(payload: Optional[dict]) -> bool:
    """True if `payload` is our cached 'GuruFocus has no data for this name' marker."""
    return bool(payload) and payload.get(_NO_COVERAGE) is True


def _is_auth_error(status: int, err_text: str) -> bool:
    """Distinguish a bad/unconfirmed TOKEN (fatal for the whole run) from a symbol
    GuruFocus just doesn't carry. The old API returns the token problem as an
    {"error": "...Invalid API Token, please ... confirm it on API authentication
    page..."} body -- key phrases below -- whereas an unknown symbol yields an empty
    or non-token error body."""
    t = err_text.lower()
    if any(k in t for k in ("invalid api token", "api authentication",
                            "don't have authorization", "please login")):
        return True
    # a 401/403 with no parseable body at all is also treated as an auth failure
    return status in (401, 403) and not t


def fetch_fundamentals(
    exchange: str,
    ticker: str,
    token: str,
    *,
    cache_dir: str | Path = "output/gurufocus_cache",
    timeout: float = 20.0,
    from_cache: bool = False,
    max_retries: int = 4,
    backoff_base: float = 1.5,
    backoff_max: float = 20.0,
    client: Optional[httpx.Client] = None,
    progress=None,
) -> Optional[dict]:
    """Fetch one company's fundamentals, cache-first.

    Returns the raw GuruFocus response dict, a no-coverage marker dict (check with
    `is_no_coverage`), or None if `from_cache` and nothing is cached. Raises
    GuruFocusError on an unrecoverable API error (e.g. 401 bad token).
    """
    cache_dir = Path(cache_dir)
    cached = load_cached(cache_dir, exchange, ticker)
    if cached is not None:
        return cached
    if from_cache:
        return None  # offline mode: a miss is a miss, never hit the network

    sym = gf_symbol(exchange, ticker)
    # Old-API shape: token in the PATH, resource is 'financials', newest-year-first.
    url = f"{BASE_URL}/{token}/stock/{sym}/financials"
    params = {"order": "desc"}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                resp = client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:  # network/timeout -- retry
                last_exc = exc
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = GuruFocusError(f"transient {resp.status_code} for {sym}")
                else:
                    # The old API signals a bad token as an {"error": "...Invalid API
                    # Token..."} body (often with 401), and an unknown/uncovered
                    # symbol as an empty or error body too -- so parse the body and
                    # distinguish auth-failure (fatal) from no-coverage (per-symbol).
                    try:
                        data = resp.json()
                    except json.JSONDecodeError:
                        data = None
                    err = (data.get("error") if isinstance(data, dict) else None) or ""
                    if _is_auth_error(resp.status_code, err):
                        raise GuruFocusError(
                            f"GuruFocus token rejected for {sym} ({resp.status_code}): "
                            f"{err.strip() or 'invalid/unconfirmed token'}\n  Confirm the "
                            "token at https://www.gurufocus.com/api/authentication "
                            "(config gurufocus.api_token or GURUFOCUS_API_TOKEN).")
                    if resp.status_code in (400, 404) or not isinstance(data, dict) \
                            or not data or err or "financials" not in data:
                        marker = {_NO_COVERAGE: True, "_status": resp.status_code,
                                  "_symbol": sym,
                                  "_body": data if isinstance(data, dict) else None}
                        _write_cache(cache_dir, exchange, ticker, marker)
                        return marker
                    _write_cache(cache_dir, exchange, ticker, data)
                    return data

            # backoff before the next attempt (jitter derived from attempt, no RNG
            # so the tool stays deterministic)
            if attempt < max_retries - 1:
                delay = min(backoff_max, backoff_base ** (attempt + 1))
                delay += (attempt % 3) * 0.25  # light spread
                if progress:
                    progress(f"    {sym}: retry {attempt + 1}/{max_retries - 1} "
                             f"in {delay:.1f}s ({last_exc})")
                time.sleep(delay)

        raise GuruFocusError(
            f"GuruFocus fetch failed for {sym} after {max_retries} attempts: {last_exc}")
    finally:
        if owns_client:
            client.close()
