"""Stage 2: pick the company's own IR homepage from search results.

Given a list of SearchResult for a company, filter out non-corporate hosts
(blocklist) and score the remainder by how well the domain matches tokens from
the company's legal name. Also flag known third-party IR hosting platforms
(Q4 Inc. first) which have predictable page structures for later stages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ingest import name_tokens
from search_provider import SearchResult

# Known third-party IR hosting platforms -> platform id. Domains here are NOT
# blocked; they ARE the IR site, just hosted by a vendor with a predictable
# structure. Extend as new platforms turn up during the pilot.
IR_PLATFORMS: dict[str, str] = {
    "q4inc.com": "q4",
    "q4web.com": "q4",
    "q4cdn.com": "q4",
}


@dataclass(frozen=True)
class IRCandidate:
    url: str
    domain: str
    score: float
    platform: str | None  # e.g. "q4" when hosted on a known IR platform
    sure: bool = False     # domain genuinely matches the company (not rank-only)


def load_blocklist(path: str) -> set[str]:
    blocked: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    blocked.add(line.lower())
    except FileNotFoundError:
        pass
    return blocked


def _registrable_domain(host: str) -> str:
    """Cheap eTLD+1 approximation good enough for scoring/blocklist matching.

    Handles common multi-part public suffixes for Canadian/UK/AU sites.
    """
    host = host.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    multi = {"co", "com", "org", "net", "gov", "ac", "edu"}
    if parts[-2] in multi and len(parts[-1]) == 2:  # e.g. example.co.uk
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_blocked(host: str, blocklist: set[str]) -> bool:
    host = host.lower()
    for b in blocklist:
        if host == b or host.endswith("." + b):
            return True
    return False


def _detect_platform(host: str) -> str | None:
    host = host.lower()
    for domain, platform in IR_PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return None


def choose_ir_homepage(
    results: list[SearchResult],
    legal_name: str,
    blocklist: set[str],
) -> IRCandidate | None:
    """Return the best IR-homepage candidate, or None if nothing plausible."""
    ordered = name_tokens(legal_name)
    tokens = set(ordered)
    # Acronym in NAME order, e.g. Royal Bank of Canada -> "rbc", Toronto-Dominion
    # Bank -> "tdb"/"td". Kept ordered so it actually matches acronym domains.
    acronym = "".join(t[0] for t in ordered)
    best: IRCandidate | None = None
    seen_domains: set[str] = set()

    for rank, res in enumerate(results):
        host = urlparse(res.url).netloc.lower()
        if not host:
            continue
        platform = _detect_platform(host)
        if _is_blocked(host, blocklist) and not platform:
            continue
        reg = _registrable_domain(host)
        if reg in seen_domains:
            continue
        seen_domains.add(reg)

        score, sure = _score_domain(reg, tokens, acronym, rank, platform)
        cand = IRCandidate(url=_homepage_of(res.url, platform), domain=reg,
                           score=score, platform=platform, sure=sure)
        if best is None or cand.score > best.score:
            best = cand

    # Require a minimum plausibility so we don't latch onto an unrelated domain.
    if best is not None and best.score >= 1.0:
        return best
    return None


def _homepage_of(url: str, platform: str | None) -> str:
    """For a normal corporate site, reduce to scheme://host so crawling starts
    at the homepage. For hosted IR platforms keep the full URL (the vendor path
    IS the IR landing page)."""
    parsed = urlparse(url)
    if platform:
        return url
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def _score_domain(reg_domain: str, tokens: set[str], acronym: str, rank: int,
                  platform: str | None) -> tuple[float, bool]:
    """Return (score, sure). Higher score is better; `sure` is True when the
    domain genuinely matches the company (a name token, the acronym, an exact
    label match, or a known IR platform) rather than winning on search rank
    alone. Callers use `sure` to accept a first-party site only for confident
    matches, deferring weak ones to the exchange-filings fallbacks."""
    label = reg_domain.split(".")[0]
    label_compact = re.sub(r"[^a-z0-9]", "", label)

    score = 0.0
    matched = 0
    for tok in tokens:
        if tok in label_compact:
            matched += 1
            # Longer token matches are stronger signals.
            score += 1.0 + min(len(tok), 8) / 8.0
    # A label that EXACTLY equals a name token (e.g. "shopify") or the acronym
    # (e.g. "rbc") is almost certainly the primary corporate domain — reward it
    # so it beats longer concatenations like "rbcroyalbank" (RBC's retail site).
    exact = label_compact in tokens or label_compact == acronym
    if exact:
        score += 4.5
    # Acronym match (name order), e.g. "rbc" == label for Royal Bank of Canada.
    # Also allow a short domain label to be a prefix of the acronym so
    # "td" matches Toronto-Dominion-Bank ("tdb").
    acr_match = False
    if len(acronym) >= 2 and (
        label_compact == acronym
        or label_compact.startswith(acronym)
        or (2 <= len(label_compact) <= 4 and acronym.startswith(label_compact))
    ):
        score += 2.0
        acr_match = True

    # Rank bonus: earlier results are more likely correct.
    score += max(0.0, 1.0 - rank * 0.15)

    if platform:
        score += 0.75  # a known IR host is a positive signal even w/o token match

    # A domain matching no name tokens, no acronym, and not a platform is weak.
    if matched == 0 and not acr_match and not platform:
        score *= 0.5
    # "Sure" = a real company signal, not a rank-only guess.
    sure = bool(matched) or acr_match or exact or bool(platform)
    return score, sure
