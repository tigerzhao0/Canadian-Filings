"""Content check: download a candidate PDF and confirm it actually reads like a
financial statement / annual report — not a cover letter, a "our annual filing
will be available soon" notice, or a terms/disclaimer page that merely mentions
one.

Deliberately conservative: it only REJECTS a PDF when it can positively tell the
document is NOT a financial statement (short doc, real extractable text, zero
statement language). Anything it can't judge — a scanned/image PDF with no text
layer, an unparseable file, a download failure, or a long marketing-heavy annual
report — is ACCEPTED, so this never introduces new false negatives.
"""
from __future__ import annotations

import io

# Phrases that appear in genuine financial statements / annual reports.
_FS_SIGNALS = (
    "financial statement", "statement of financial position", "balance sheet",
    "statement of operations", "statement of loss", "statement of income",
    "statement of comprehensive", "statement of cash flow", "statements of cash flow",
    "statement of changes in equity", "shareholders' equity", "shareholders’ equity",
    "stockholders' equity", "notes to the", "consolidated statement",
    "independent auditor", "report of independent", "management's discussion",
    "management’s discussion", "total assets", "total liabilities",
)


async def looks_like_financial_statement(client, url, user_agent, timeout,
                                         max_bytes: int = 40_000_000,
                                         max_pages: int = 25) -> tuple[bool, str]:
    """Return (accept, reason). accept is False ONLY when the PDF is positively a
    non-statement (short, has real text, no statement language)."""
    try:
        resp = await client.get(url, headers={"User-Agent": user_agent},
                                timeout=timeout * 2, follow_redirects=True)
    except Exception:  # noqa: BLE001
        return True, "download_failed"
    if resp.status_code != 200:
        return True, "unverifiable_status"
    data = resp.content
    if not data or not data[:5].startswith(b"%PDF") or len(data) > max_bytes:
        return True, "unverifiable_pdf"

    text, npages, parsed = _extract_text(data, max_pages)
    if not parsed or npages == 0:
        return True, "unparseable"
    stripped = text.strip()
    # Real financial statements always contain statement language, at any length.
    if any(sig in stripped for sig in _FS_SIGNALS):
        return True, ""
    # A near-empty extract means a scanned/image PDF (no text layer) — can't judge
    # its content, so don't reject it. A genuine notice/letter, by contrast, has
    # real sentences (dozens+ of chars) but none of the statement language above.
    if len(stripped) < 40:
        return True, "no_text_layer"
    # Has real text, no statement language: a short doc is a cover letter / notice
    # / terms page; a long one might be a marketing-heavy report, so give benefit.
    if npages <= 6:
        return False, "not_financial_statement"
    return True, "uncertain_long_doc"


def _extract_text(data: bytes, max_pages: int) -> tuple[str, int, bool]:
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001 - pypdf not installed -> skip verification
        return "", 0, False
    try:
        reader = PdfReader(io.BytesIO(data))
        npages = len(reader.pages)
        parts = []
        for page in reader.pages[:max_pages]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page shouldn't abort
                pass
        return " ".join(parts).lower(), npages, True
    except Exception:  # noqa: BLE001 - encrypted/corrupt -> unverifiable
        return "", 0, False
