"""Document-TYPE classification for candidate PDFs.

The finder/verifier historically accepted anything containing a single
financial-statement keyword, so Annual Information Forms (AIF) and MD&A
supplementary-table dumps -- which mention statement names but are NOT primary
financial statements -- slipped through and poisoned extraction (e.g. WAU got an
AIF for the wrong company).

The reliable discriminator already lives in pdf_extract: a real primary-statements
document has the income statement, balance sheet, AND cash-flow statement each
appearing as a PAGE-TOP header (not a mid-notes / TOC / MD&A-table mention),
clustered together after an auditor's report. An AIF has none of those headers; an
MD&A table dump mentions the names in body text but not as page tops. This module
turns that into a single `classify_document()` gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pdf_extract import (
    _AUDITOR_RE, _collapse, _page_texts, _smallest_span_covering_all_types,
    _statement_header_pages, _top3_collapsed,
)

# Primary types the finder cares about.
PRIMARY = "primary_statements"       # usable: the three statements are really here
AIF = "aif"                           # annual information form -- business description, no statements
MDA = "mda"                           # management's discussion & analysis -- prose + summary tables
INTERIM = "interim"                   # quarterly / interim report
OTHER = "other"                       # anything else (cover letter, prospectus, unknown)
UNPARSEABLE = "unparseable"           # no text layer / not a PDF -- can't judge (caller decides)

_AIF_RE = re.compile(r"annual information form|notice of annual|information circular")
_MDA_RE = re.compile(r"management'?s discussion|management discussion (and|&) analysis|md&a|mdna|"
                     r"management report of fund performance")  # MRFP: fund MD&A-equivalent
_INTERIM_RE = re.compile(r"\binterim\b|\bunaudited\b|first quarter|second quarter|third quarter|"
                         r"\bq[1-3]\b|three months ended|six months ended|nine months ended")
_ANNUAL_RE = re.compile(r"\bannual\b|year ended|years ended|fiscal year|for the year")


@dataclass
class DocClass:
    doc_type: str
    n_statement_types: int      # how many of income/balance/cashflow appear as page-top headers
    has_auditor: bool
    has_statement_span: bool    # all-three cluster found (via smallest-span)
    npages: int

    @property
    def is_primary(self) -> bool:
        return self.doc_type == PRIMARY


def classify_from_pages(pages: list[str]) -> DocClass:
    """Classify already-extracted per-page text. Pure/offline."""
    npages = len(pages)
    if npages == 0:
        return DocClass(UNPARSEABLE, 0, False, False, 0)

    header_pages = _statement_header_pages(pages)
    n_types = sum(1 for ps in header_pages.values() if ps)
    span = _smallest_span_covering_all_types(header_pages)
    collapsed = [_collapse(p) for p in pages]
    has_auditor = any(_AUDITOR_RE.search(c) for c in collapsed)

    # PRIMARY: all three statements as page-top headers (a real statement block),
    # OR two of three plus an auditor's report (some issuers' 3rd header evades the
    # top-3-lines match, but the auditor anchor makes it safe).
    if n_types == 3 or (n_types >= 2 and has_auditor):
        return DocClass(PRIMARY, n_types, has_auditor, span is not None, npages)

    # Not a statements block -> what kind of look-alike is it? Judge from the
    # first few pages' tops (titles), where AIF/MD&A/interim announce themselves.
    head = " ".join(_top3_collapsed(p) for p in pages[:6])
    body = " ".join(collapsed[:8])
    if _AIF_RE.search(head) or _AIF_RE.search(body):
        return DocClass(AIF, n_types, has_auditor, False, npages)
    if _MDA_RE.search(head) or _MDA_RE.search(body):
        return DocClass(MDA, n_types, has_auditor, False, npages)
    if _INTERIM_RE.search(body) and not _ANNUAL_RE.search(body):
        return DocClass(INTERIM, n_types, has_auditor, False, npages)
    return DocClass(OTHER, n_types, has_auditor, False, npages)


def classify_document(pdf_bytes: bytes) -> DocClass:
    """Classify a PDF's bytes. Returns UNPARSEABLE when there's no text layer
    (scanned) or it isn't a parseable PDF -- the caller decides how conservative
    to be there (we never want a false 'not a statement' on an unreadable file)."""
    if not pdf_bytes or not pdf_bytes[:5].startswith(b"%PDF"):
        return DocClass(UNPARSEABLE, 0, False, False, 0)
    pages = _page_texts(pdf_bytes)
    if pages is None:
        return DocClass(UNPARSEABLE, 0, False, False, 0)
    total_chars = sum(len(p) for p in pages)
    if len(pages) == 0 or total_chars < len(pages) * 100:
        return DocClass(UNPARSEABLE, 0, False, False, len(pages))  # scanned/no text layer
    return classify_from_pages(pages)
