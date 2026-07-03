"""Stage 1: pluggable web-search backends.

Two free backends ship here:

* DuckDuckGoProvider (default) -- keyless, no signup, no daily cap, so it can
  finish the whole ~2,564-company list in one session. DuckDuckGo throttles
  under heavy use, so search LAUNCHES are paced at least min_delay_seconds
  apart with exponential backoff on throttling; up to `concurrency` searches
  can be in flight at once so pacing isn't also accidentally serialised on
  each request's round-trip time. A company that still can't be searched
  raises SearchRateLimited and is routed to needs_review by the pipeline.

* GoogleCSEProvider (optional) -- official Google Custom Search JSON API.
  Reliable but its free tier caps at 100 queries/day, so it can't finish in a
  single run; kept for anyone who wants it via config.

Both return a list[SearchResult]. The interface is intentionally tiny so more
backends (Brave, SerpAPI, ...) can be dropped in.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchError(Exception):
    """Non-retryable search failure (e.g. bad credentials, no results)."""


class SearchRateLimited(SearchError):
    """Search was throttled and exhausted retries -> route to needs_review."""


class SearchProvider(ABC):
    """Abstract search backend. Implementations must be safe to call
    concurrently, but may internally serialise (DuckDuckGo does)."""

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        ...


class _AsyncRateLimiter:
    """Paces call LAUNCHES at least `min_delay` apart (politeness towards the
    backend) while allowing up to `concurrency` calls to be in flight at once,
    so throughput isn't also accidentally capped by how long each request
    takes to complete. With concurrency=1 this behaves exactly like a full
    serialising lock (the original, most conservative behavior)."""

    def __init__(self, min_delay: float, concurrency: int = 1):
        self._min_delay = min_delay
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._pace_lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self):
        await self._sem.acquire()
        async with self._pace_lock:
            wait = self._min_delay - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo backend via the `ddgs` library (runs in a thread pool since
    the library is synchronous). Serialised + backed off to avoid IP blocks."""

    def __init__(
        self,
        min_delay: float = 2.5,
        max_retries: int = 4,
        backoff_base: float = 5.0,
        backoff_max: float = 120.0,
        concurrency: int = 1,
    ):
        self._limiter = _AsyncRateLimiter(min_delay, concurrency)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            async with self._limiter:
                try:
                    res = await asyncio.to_thread(self._search_sync, query, max_results)
                    if res:
                        return res
                    # DDG frequently returns an EMPTY list (rather than an error)
                    # when it soft-throttles. Treat empty as retryable for the
                    # first couple of attempts before accepting "no results".
                    if attempt >= 2:
                        return res
                    last_err = _DDGRateLimit("empty result set (soft throttle?)")
                except _DDGRateLimit as exc:
                    last_err = exc
                except Exception as exc:  # noqa: BLE001 - normalise library errors
                    msg = str(exc).lower()
                    if any(k in msg for k in ("rate", "ratelimit", "202", "429", "timeout")):
                        last_err = exc
                    else:
                        raise SearchError(f"DuckDuckGo search failed: {exc}") from exc
            if attempt < self._max_retries:
                delay = min(self._backoff_base * (2 ** attempt), self._backoff_max)
                delay *= 0.5 + random.random()  # jitter 0.5x-1.5x
                await asyncio.sleep(delay)
        raise SearchRateLimited(
            f"DuckDuckGo throttled after {self._max_retries + 1} attempts: {last_err}"
        )

    @staticmethod
    def _search_sync(query: str, max_results: int) -> list[SearchResult]:
        from ddgs import DDGS  # lazy import

        try:
            with DDGS() as ddgs:
                hits = ddgs.text(query, max_results=max_results)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if any(k in msg for k in ("rate", "ratelimit", "202", "429")):
                raise _DDGRateLimit(str(exc)) from exc
            raise
        results: list[SearchResult] = []
        for h in hits or []:
            url = h.get("href") or h.get("url") or ""
            if not url:
                continue
            results.append(
                SearchResult(
                    title=h.get("title", ""),
                    url=url,
                    snippet=h.get("body", ""),
                )
            )
        return results


class _DDGRateLimit(Exception):
    pass


class GoogleCSEProvider(SearchProvider):
    """Google Custom Search JSON API backend (optional)."""

    ENDPOINT = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, cx: str, min_delay: float = 0.5):
        if not api_key or not cx:
            raise SearchError(
                "Google CSE selected but api_key/cx are missing. Set them in "
                "config.yaml (search.google_cse) or via GOOGLE_CSE_API_KEY / "
                "GOOGLE_CSE_CX environment variables."
            )
        self._api_key = api_key
        self._cx = cx
        self._limiter = _AsyncRateLimiter(min_delay)

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        import httpx  # lazy import

        params = {
            "key": self._api_key,
            "cx": self._cx,
            "q": query,
            "num": min(max_results, 10),  # CSE max is 10 per request
        }
        async with self._limiter:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(self.ENDPOINT, params=params)
        if resp.status_code == 429:
            raise SearchRateLimited("Google CSE daily/rate quota exhausted (HTTP 429)")
        if resp.status_code != 200:
            raise SearchError(f"Google CSE HTTP {resp.status_code}: {resp.text[:200]}")
        items = resp.json().get("items", [])
        return [
            SearchResult(
                title=i.get("title", ""),
                url=i.get("link", ""),
                snippet=i.get("snippet", ""),
            )
            for i in items
            if i.get("link")
        ]


def build_provider(config: dict) -> SearchProvider:
    """Factory: construct the SearchProvider named in config['search']."""
    search_cfg = config.get("search", {})
    provider = (search_cfg.get("provider") or "duckduckgo").lower()

    if provider == "duckduckgo":
        return DuckDuckGoProvider(
            min_delay=float(search_cfg.get("min_delay_seconds", 2.5)),
            max_retries=int(search_cfg.get("max_retries", 4)),
            backoff_base=float(search_cfg.get("backoff_base_seconds", 5.0)),
            backoff_max=float(search_cfg.get("backoff_max_seconds", 120.0)),
            concurrency=int(search_cfg.get("concurrency", 4)),
        )
    if provider in ("google_cse", "google"):
        gcfg = search_cfg.get("google_cse", {}) or {}
        api_key = gcfg.get("api_key") or os.environ.get("GOOGLE_CSE_API_KEY", "")
        cx = gcfg.get("cx") or os.environ.get("GOOGLE_CSE_CX", "")
        return GoogleCSEProvider(api_key=api_key, cx=cx)

    raise SearchError(f"Unknown search provider '{provider}' (use duckduckgo or google_cse)")
