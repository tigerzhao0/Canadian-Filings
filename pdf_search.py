"""Tier-1 fallback: find the annual-report PDF by searching for it directly.

When homepage discovery + crawl fails to surface a PDF (common on JS-heavy IR
sites), we run ONE extra search of the form:

    "<legal name>" annual report filetype:pdf

Search engines have already indexed most IR annual-report PDFs, so the top hits
are frequently the exact file on the company's own domain. We filter out
blocklisted / aggregator hosts, prefer the company's own domain, score by the
same annual-report heuristics as the crawler, and hand the best candidates back
to the pipeline for HEAD validation.
"""
from __future__ import annotations

from urllib.parse import urlparse

from crawl_pdf import PDFCandidate, score_pdf
from discover_ir import _is_blocked, _registrable_domain
from ingest import name_tokens
from search_provider import SearchError, SearchRateLimited


async def direct_pdf_search(
    provider,
    company,
    blocklist: set[str],
    preferred_reg_domain: str | None,
    cfg: dict,
) -> list[PDFCandidate]:
    """Return confident annual-report PDF candidates, best first (may be empty)."""
    name = company.legal_name
    query = f'"{name}" annual report filetype:pdf'
    max_results = int(cfg["search"]["max_results"])
    try:
        results = await provider.search(query, max_results)
    except (SearchError, SearchRateLimited):
        return []

    tokens = set(name_tokens(name))
    acronym = "".join(t[0] for t in name_tokens(name))

    scored: list[tuple[float, PDFCandidate]] = []
    seen: set[str] = set()
    for res in results:
        url = res.url
        low = url.lower()
        if ".pdf" not in low:
            continue
        host = urlparse(url).netloc.lower()
        if not host or _is_blocked(host, blocklist):
            continue
        if url in seen:
            continue
        seen.add(url)

        cand = score_pdf(url, res.title)
        if not cand.has_report_hint:
            continue

        # Domain trust: a filetype:pdf search readily returns the report hosted
        # on third-party sites (blogs, news-attachment CDNs, aggregators). Only
        # accept a PDF whose domain actually belongs to the company — matches a
        # name token, the name acronym, or the IR homepage we already found.
        reg = _registrable_domain(host)
        label = reg.split(".")[0]
        boost = 0.0
        if preferred_reg_domain and reg == preferred_reg_domain:
            boost += 4.0
        if any(tok in label for tok in tokens):
            boost += 2.0
        if len(acronym) >= 3 and (label == acronym or label.startswith(acronym)):
            boost += 2.0
        if boost == 0.0:
            continue  # untrusted third-party domain -> skip
        scored.append((cand.score + boost, cand))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored]
