"""Stage 4: confirm a candidate URL really is a reachable PDF.

Tries HEAD first; some servers don't support HEAD or omit content-type on it,
so we fall back to a tiny ranged GET, then to sniffing the %PDF magic bytes.
Many IR-site CDNs block non-browser User-Agents, so on failure we retry with a
realistic browser UA.

Returns a (state, detail) tuple where state is one of:
    "ok"      -> confirmed reachable PDF
    "blocked" -> exists but the CDN forbids bots (403/401/429) even with a
                 browser UA (e.g. Akamai/Cloudflare JS challenge). The URL is
                 almost certainly the real file; caller may accept it as an
                 UNVERIFIED find for trusted own-domain candidates.
    "fail"    -> genuinely not a usable PDF (404, wrong content-type, error)
"""
from __future__ import annotations

_BLOCK_CODES = ("403", "401", "429")


async def validate_pdf(client, url, user_agent, timeout, browser_user_agent=None):
    state, detail = await _attempt(client, url, user_agent, timeout)
    if state == "ok":
        return "ok", ""
    # Retry with a browser UA when the first attempt looks like bot-blocking.
    if browser_user_agent and browser_user_agent != user_agent and (
        state == "blocked" or detail.startswith("error:")
    ):
        state2, detail2 = await _attempt(client, url, browser_user_agent, timeout)
        if state2 == "ok":
            return "ok", ""
        if state2 == "blocked" or state == "blocked":
            return "blocked", detail2 or detail
        return "fail", detail2
    if state == "blocked":
        return "blocked", detail
    return "fail", detail


async def _attempt(client, url, ua, timeout) -> tuple[str, str]:
    headers = {"User-Agent": ua, "Accept": "*/*"}

    # 1) HEAD
    try:
        resp = await client.head(url, headers=headers, timeout=timeout,
                                 follow_redirects=True)
        if _is_pdf_response(resp):
            return "ok", ""
        head_status = str(resp.status_code)
    except Exception as exc:  # noqa: BLE001
        head_status = f"error:{type(exc).__name__}"

    # 2) Ranged GET fallback (first 1 KB).
    try:
        resp = await client.get(
            url,
            headers={**headers, "Range": "bytes=0-1023"},
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        if any(code in head_status for code in _BLOCK_CODES):
            return "blocked", f"blocked (head={head_status})"
        return "fail", f"error:{type(exc).__name__}"

    if _is_pdf_response(resp) or resp.content[:5].startswith(b"%PDF"):
        return "ok", ""
    get_status = str(resp.status_code)
    if any(code in head_status or code in get_status for code in _BLOCK_CODES):
        return "blocked", f"blocked (head={head_status}, get={get_status})"
    return "fail", (f"pdf_head_failed (head={head_status}, get={get_status}, "
                    f"ctype={resp.headers.get('content-type','?')})")


def _is_pdf_response(resp) -> bool:
    if resp.status_code not in (200, 206):
        return False
    return "application/pdf" in resp.headers.get("content-type", "").lower()
