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
import re

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

# Phrases that mark a document as an interim/quarterly filing rather than an
# annual one. Only disqualifying when NONE of _ANNUAL_SIGNALS is also present,
# since an annual report's MD&A routinely quotes prior-quarter figures.
_INTERIM_SIGNALS = (
    "interim financial statement", "interim consolidated", "condensed interim",
    "condensed consolidated interim", "unaudited condensed", "unaudited interim",
    "three months ended", "six months ended", "nine months ended",
    "first quarter", "second quarter", "third quarter", "quarterly report",
    "quarterly financial statement",
)
_ANNUAL_SIGNALS = (
    "annual report", "annual information form", "twelve months ended",
    "fiscal year ended", "for the year ended", "year ended december",
    "audited annual", "audited consolidated financial statement",
)

_CORP_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "ltd", "limited", "llc",
    "lp", "plc", "co", "company", "group", "holdings", "holding", "the",
    "and", "class", "ordinary", "common", "shares",
}


def _name_tokens(company_name: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", company_name.lower())
    return [w for w in words if w not in _CORP_SUFFIXES and len(w) > 2]


def _looks_interim(text: str) -> bool:
    return (any(sig in text for sig in _INTERIM_SIGNALS)
            and not any(sig in text for sig in _ANNUAL_SIGNALS))


def _name_mismatch(text: str, company_name: str | None) -> bool:
    """True only when we have real name tokens to check AND none of them
    appear anywhere in the document — a strong signal it's the wrong company."""
    if not company_name:
        return False
    tokens = _name_tokens(company_name)
    if not tokens:
        return False
    return not any(t in text for t in tokens)


async def looks_like_financial_statement(client, url, user_agent, timeout,
                                         company_name: str | None = None,
                                         max_bytes: int = 40_000_000,
                                         max_pages: int = 25) -> tuple[bool, str]:
    """Return (accept, reason). accept is False ONLY when the PDF is positively
    NOT what we want: a non-statement, an interim/quarterly filing (when no
    annual language is also present), or a document that never mentions the
    target company at all (when company_name is given and has real tokens)."""
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
    stripped = text.strip()

    # STRUCTURAL document-type gate: an Annual Information Form or an MD&A dump
    # mentions statement names in body text but does NOT present the income
    # statement + balance sheet + cash-flow statement as page-top headers with an
    # auditor's report. classify_document (via pdf_extract's header detection)
    # reliably tells real primary statements from those look-alikes -- the old
    # single-substring test accepted AIFs/MD&A, the WAU root cause.
    try:
        from doc_classify import classify_document, PRIMARY, AIF, MDA, INTERIM
        klass = classify_document(data)
    except Exception:  # noqa: BLE001 - classifier unavailable -> fall back below
        klass = None

    if klass is not None:
        if klass.doc_type in (AIF, MDA):
            return False, klass.doc_type                    # look-alike -> reject
        if klass.doc_type == INTERIM:
            return False, "interim_or_quarterly_report"
        if klass.doc_type == PRIMARY:
            if _name_mismatch(stripped, company_name):
                return False, "company_name_mismatch"
            return True, "primary_statements"
        # else UNPARSEABLE / OTHER -> fall through to the conservative old logic
        # (never false-reject a scanned or genuinely-unknown doc).

    if not parsed or npages == 0:
        return True, "unparseable"
    # A near-empty extract means a scanned/image PDF (no text layer) — can't judge
    # its content, so don't reject it. A genuine notice/letter, by contrast, has
    # real sentences (dozens+ of chars) but none of the statement language above.
    if len(stripped) < 40:
        return True, "no_text_layer"

    has_fs_signal = any(sig in stripped for sig in _FS_SIGNALS)
    # Has real text, no statement language: a short doc is a cover letter /
    # notice / terms page; a long one might be a marketing-heavy report, so
    # give it the benefit of the doubt and let the checks below judge it.
    if not has_fs_signal and npages <= 6:
        return False, "not_financial_statement"

    if _looks_interim(stripped):
        return False, "interim_or_quarterly_report"
    if _name_mismatch(stripped, company_name):
        return False, "company_name_mismatch"
    return True, "" if has_fs_signal else "uncertain_long_doc"


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
