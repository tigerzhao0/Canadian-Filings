"""PDF -> per-statement text blocks. Pure and offline (no network, no DB, no
LLM), so it's unit-testable against the sample PDFs.

CSE annual-financial-statement PDFs are text-based (extractable), and their
income statement / balance sheet / cash-flow statement are laid out as
whitespace-aligned text, NOT ruled tables -- so pdfplumber's `extract_text()`
gives clean, LLM-ready lines while `extract_table()` returns nothing useful.
See docs/PLAN_cse_financials_extraction.md for the full rationale and the three
real gotchas this handles: section keywords appear on many pages (TOC, auditor
report, the statement itself, the notes); word-spacing extracts inconsistently
(so header matching is whitespace-insensitive); and some issuers file scanned
image PDFs with no text layer (detected, flagged, OCR left as a future branch).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ExtractResult:
    ok: bool                                    # False only when nothing usable came out
    scanned: bool = False                       # True if no text layer (OCR would be needed)
    npages: int = 0
    sections: dict[str, str] = field(default_factory=dict)  # income_statement / balance_sheet / cash_flow (+ oci)
    primary_block: str = ""                     # the whole primary-statements span (fallback for the LLM)
    reason: str | None = None                   # why ok=False / a note
    unit_scale_hint: float | None = None        # units note in the PRIMARY span (1000 / 1e6)


# Header regexes, matched against COLLAPSED-whitespace lowercased text so
# inconsistent word-spacing in the PDF text layer doesn't defeat them. Both
# English and FRENCH (Quebec / bilingual issuers file in French); accented
# classes like [ée] also tolerate accent-stripped extraction.
_AUDITOR_RE = re.compile(
    r"independent auditor|auditor'?s report|report of independent"
    r"|rapport de l['’]auditeur ind[ée]pendant|rapport des auditeurs ind[ée]pendants"
    r"|rapport de l['’]auditeur")
_NOTES_RE = re.compile(
    r"notes to (the )?(consolidated )?(interim )?financial statements"
    r"|notes (aff[ée]rentes|annexes|compl[ée]mentaires) aux [ée]tats financiers"
    r"|notes aux [ée]tats financiers")
# Big issuers' note pages never repeat the "notes to the financial statements"
# banner -- each page just opens "Note 1 General information", "Note 2 Summary
# of material accounting policies..." -- so the span-end must also recognise a
# numbered-note PAGE TOP, or the primary span drags a dozen note pages in
# (and a notes page mentioning "statement of income" re-anchors the section).
_NOTES_PAGE_TOP_RE = re.compile(r"^notes? \d{1,3}\b")


def _notes_boundary(pages: list[str], collapsed: list[str], lo: int, npages: int) -> int | None:
    """First page index >= lo that starts the notes: either the classic banner
    anywhere on the page, or a numbered-note page top."""
    for i in range(lo, npages):
        if _NOTES_RE.search(collapsed[i]):
            return i
        if _NOTES_PAGE_TOP_RE.match(_top3_collapsed(pages[i])):
            return i
    return None
_STATEMENT_RES = {
    "balance_sheet": re.compile(
        r"statements? of financial position|balance sheets?"
        r"|[ée]tats? de la situation financi[eè]re|bilans?"),
    "income_statement": re.compile(
        r"statements? of (loss|income|operations|comprehensive (income|loss)|profit)"
        r"|[ée]tats? (du|des) r[ée]sultats?( global| net)?"
        r"|[ée]tats? du r[ée]sultat global"),
    "cash_flow": re.compile(
        r"statements? of cash ?flows?"
        r"|(tableaux?|[ée]tats?) des flux de tr[ée]sorerie"),
}
# A STANDALONE comprehensive-income statement (big issuers print it as its own
# page AFTER the income statement; small issuers combine both into one header).
# Standalone = mentions comprehensive but NOT income/loss/operations by itself.
_OCI_ONLY_RE = re.compile(r"statements? of comprehensive (income|loss)")
_INCOME_PROPER_RE = re.compile(
    r"statements? of (loss|income|operations|profit)(?! and comprehensive)"
    r"|statements? of (loss|income) and comprehensive")
# Changes-in-equity header: ends whatever statement was being absorbed (it sits
# between the income/BS pages and is not one of the three statements we keep).
_EQUITY_STMT_RE = re.compile(
    r"statements? of changes in (shareholders'?|stockholders'?)? ?equity"
    r"|statement of equity|[ée]tats? des variations des capitaux propres")

# Units note, English + French.
# \s* between words: glued text layers ("(MillionsofCanadiandollars)" in
# RBC's pre-2019 reports) must still match.
_SCALE_HINT_RES = [
    (re.compile(r"in\s*thousands|thousands\s*of|\(000s?\)|\$000s?|en\s*milliers", re.I), 1000.0),
    (re.compile(r"in\s*millions|millions\s*of|en\s*millions", re.I), 1_000_000.0),
]


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _detect_unit_scale_hint(pages: list[str], primary_pages: list[int]) -> float | None:
    """Units note from the PRIMARY-STATEMENTS span only, most-frequent match wins.
    (Scanning the whole doc was wrong: RY's statements say '(Millions of Canadian
    dollars)' on every statement page, but a distant page mentioning 'thousands'
    made the old first-match logic store 1000 -- a 1000x hazard for any consumer
    trusting the hint.)"""
    counts = {1000.0: 0, 1_000_000.0: 0}
    for i in primary_pages:
        low = pages[i].lower()
        for rx, mult in _SCALE_HINT_RES:
            counts[mult] += len(rx.findall(low))
    if not any(counts.values()):
        return None
    return max(counts, key=lambda k: counts[k])


# Page furniture: running headers/footers repeated across many pages ("Royal
# Bank of Canada: Annual Report 2025", bare page numbers). They interleave with
# statement rows after text extraction and parse into junk line items.
_PAGE_NO_RE = re.compile(r"^[\s|]*\d{1,4}[\s|]*$")
# Stereotyped boilerplate that can appear on too few pages for the frequency
# threshold (e.g. only the ~6 statement pages of a 245-page report): the
# statement-page footer with page number, and the "accompanying notes" banner.
# No genuine statement DATA line contains these phrases.
_BOILERPLATE_RE = re.compile(
    r"accompanying notes (are|form) an integral part"
    r"|annual report (19|20)\d\d"
    r"|rapport annuel (19|20)\d\d", re.I)


def _furniture_key(line: str) -> str:
    """Digit-normalized collapsed form: running headers embed the PAGE NUMBER in
    the same line ('146 Royal Bank of Canada Annual Report 2025 ...'), so exact
    repeats never happen -- normalize digit runs to '#' before counting."""
    return re.sub(r"\d+", "#", _collapse(line))


def _furniture_lines(pages: list[str]) -> set[str]:
    """Digit-normalized lines that repeat on >30% of pages (min 3) -- running
    headers/footers, not data. Data lines never repeat verbatim across pages."""
    from collections import Counter
    npages = len(pages)
    if npages < 4:
        return set()
    counts: Counter[str] = Counter()
    for p in pages:
        seen_on_page = {_furniture_key(ln) for ln in p.splitlines() if ln.strip()}
        counts.update(seen_on_page)
    threshold = max(3, int(npages * 0.30))
    return {ln for ln, n in counts.items() if n >= threshold}


def _strip_furniture(page_text: str, furniture: set[str]) -> str:
    """Remove running-header/footer lines and bare page numbers from a page."""
    kept = []
    for ln in page_text.splitlines():
        if not ln.strip():
            continue
        if _PAGE_NO_RE.match(ln):
            continue
        if _BOILERPLATE_RE.search(ln):
            continue
        if _furniture_key(ln) in furniture:
            continue
        kept.append(ln)
    return "\n".join(kept)


def _top3_collapsed(page_text: str) -> str:
    return _collapse("\n".join(page_text.splitlines()[:3]))


def _statement_header_pages(pages: list[str]) -> dict[str, list[int]]:
    """Page indices whose TOP 3 lines match each statement's header regex --
    i.e. pages that actually START a primary statement (not a mid-notes mention)."""
    top3 = [_top3_collapsed(p) for p in pages]
    return {name: [i for i, t in enumerate(top3) if rx.search(t)]
            for name, rx in _STATEMENT_RES.items()}


def _smallest_span_covering_all_types(hits: dict[str, list[int]]) -> tuple[int, int] | None:
    """Smallest [lo, hi] page span that contains at least one header page of
    EACH statement type -- reliably the real primary-statements block even in a
    document with header-like matches scattered elsewhere (MD&A summary tables,
    per-segment disclosures). None if any statement type has zero header pages.
    Classic 'smallest range covering k lists' sliding window."""
    if not all(hits.values()):
        return None
    merged = sorted((page, kind) for kind, ps in hits.items() for page in ps)
    counts: dict[str, int] = {}
    left = 0
    best: tuple[int, int] | None = None
    for page, _kind in merged:
        counts[_kind] = counts.get(_kind, 0) + 1
        while len(counts) == len(hits):
            lo_page, lo_kind = merged[left]
            if best is None or (page - lo_page) < (best[1] - best[0]):
                best = (lo_page, page)
            counts[lo_kind] -= 1
            if counts[lo_kind] == 0:
                del counts[lo_kind]
            left += 1
    return best


def _page_texts(pdf_bytes: bytes) -> list[str] | None:
    """Per-page text via pdfplumber; None if pdfplumber can't open it at all."""
    try:
        import pdfplumber
    except Exception:  # noqa: BLE001 - dependency missing
        return None
    import io
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages]
            # Glued-text fallback: some text layers (e.g. RBC's pre-2021
            # annual reports) extract with NO inter-word spaces at the default
            # x_tolerance=3 ("oftherisksandrewards..."), defeating every
            # downstream keyword/header match. Normal prose has a space ratio
            # ~0.15; glued docs sit near 0. A tighter tolerance restores the
            # word breaks on those PDFs.
            total = sum(len(t) for t in pages)
            if total > 1000 and sum(t.count(" ") for t in pages) / total < 0.05:
                pages = [(p.extract_text(x_tolerance=1.5) or "") for p in pdf.pages]
            return pages
    except Exception:  # noqa: BLE001 - encrypted / corrupt / not a PDF
        return None


def extract_statements(pdf_bytes: bytes) -> ExtractResult:
    """Turn a CSE annual-statement PDF into per-statement text blocks. Never
    raises -- returns ok=False with a reason instead, so a batch caller can
    keep going."""
    if not pdf_bytes or not pdf_bytes[:5].startswith(b"%PDF"):
        return ExtractResult(ok=False, reason="not_a_pdf")

    pages = _page_texts(pdf_bytes)
    if pages is None:
        return ExtractResult(ok=False, reason="unparseable")

    npages = len(pages)
    total_chars = sum(len(p) for p in pages)
    # Scanned/image PDF: essentially no text layer. OCR is a future branch.
    if npages == 0 or total_chars < npages * 100:
        return ExtractResult(ok=False, scanned=True, npages=npages, reason="scanned_no_ocr")

    collapsed = [_collapse(p) for p in pages]

    # Bound the primary statements: they follow the auditor's report and end
    # where the notes begin. This avoids the TOC / notes mentions of the same
    # section names.
    auditor_page = next((i for i, c in enumerate(collapsed) if _AUDITOR_RE.search(c)), None)
    start = (auditor_page + 1) if auditor_page is not None else 0
    notes_page = _notes_boundary(pages, collapsed, start, npages)
    end = notes_page if notes_page is not None else npages
    if end <= start:  # markers crossed/missing -> fall back to the whole doc
        start, end = 0, npages

    # Safety re-anchor: a large or multi-report filing can have SEVERAL
    # "auditor's report" / "notes to..." mentions (a separate internal-controls
    # opinion, TOC, cross-references), so the naive first-match window above can
    # land on a span containing NO real statement pages at all. Detect that and
    # re-anchor to the tightest page cluster that starts a header of EACH
    # statement type -- reliable regardless of how far apart the text markers are.
    header_pages = _statement_header_pages(pages)
    window_has_header = any(start <= p < end
                            for ps in header_pages.values() for p in ps)
    if not window_has_header:
        span = _smallest_span_covering_all_types(header_pages)
        if span is not None:
            lo, hi = span
            preceding_aud = [i for i in range(0, lo + 1) if _AUDITOR_RE.search(collapsed[i])]
            start = (preceding_aud[-1] + 1) if preceding_aud else max(0, lo - 2)
            notes_after = _notes_boundary(pages, collapsed, hi, npages)
            # Cap the span when no trailing notes marker is found, so a huge
            # document doesn't drag in unrelated later header-like mentions.
            end = notes_after if notes_after is not None else min(npages, hi + 15)
            if end <= start:
                start, end = 0, npages

    primary_pages = list(range(start, end))
    # Strip running headers/footers + bare page numbers BEFORE storing text --
    # they interleave with statement rows and parse into junk line items
    # (e.g. "...Annual Report" + page number parsing as values [2025, 145]).
    furniture = _furniture_lines(pages)
    clean = {i: _strip_furniture(pages[i], furniture) for i in primary_pages}
    primary_block = "\n".join(clean[i] for i in primary_pages).strip()
    if not primary_block:
        return ExtractResult(ok=False, npages=npages, reason="no_primary_statements_found")

    # Best-effort isolate each statement: a page belongs to the statement whose
    # header appears near its TOP (first ~3 lines), which distinguishes the real
    # statement page from the many in-text mentions inside notes. A statement can
    # spill onto the next page (continuation with no new header of its own).
    sections: dict[str, str] = {}
    current: str | None = None
    cont = 0
    # A statement page + at most this many continuation pages. Caps runaway
    # absorption when the notes marker is missing (e.g. Mosaic) -- otherwise a
    # matched statement would eat every following page through end-of-document.
    # Real primary statements are 1-2 pages; the notes still live in primary_block.
    MAX_CONT = 2
    for i in primary_pages:
        head = _collapse("\n".join(pages[i].splitlines()[:3]))
        # A changes-in-equity page sits between the statements we keep -- it is
        # NOT a continuation of anything; stop absorbing so it can't bloat the
        # previous section.
        if _EQUITY_STMT_RE.search(head):
            current = None
            continue
        matched = next((name for name, rx in _STATEMENT_RES.items() if rx.search(head)), None)
        # A STANDALONE "Statements of Comprehensive Income" page (big issuers
        # print it separately, right after the income statement) is OCI, not the
        # income statement -- routing it into income_statement was bloating that
        # section 4x with OCI rows. Combined single-statement headers
        # ("Statements of Loss and Comprehensive Loss") stay income_statement.
        if (matched == "income_statement" and _OCI_ONLY_RE.search(head)
                and not _INCOME_PROPER_RE.search(head)):
            matched = "oci"
        if matched:
            current, cont = matched, 0
            sections[current] = (sections.get(current, "") + "\n" + clean[i]).strip()
        elif current and cont < MAX_CONT:
            # No new statement header at the top -> a continuation page of the
            # statement we're currently in (statements often spill over one page).
            cont += 1
            sections[current] = (sections[current] + "\n" + clean[i]).strip()
        else:
            current = None  # cap reached / nothing active -> stop absorbing

    return ExtractResult(ok=True, npages=npages, sections=sections,
                        primary_block=primary_block,
                        unit_scale_hint=_detect_unit_scale_hint(pages, primary_pages))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python pdf_extract.py <file.pdf>")
        raise SystemExit(2)
    with open(sys.argv[1], "rb") as fh:
        res = extract_statements(fh.read())
    print(f"ok={res.ok} scanned={res.scanned} npages={res.npages} reason={res.reason}")
    for name in ("income_statement", "balance_sheet", "cash_flow"):
        txt = res.sections.get(name, "")
        print(f"\n=== {name} ({len(txt)} chars) ===")
        print(txt[:600])
    print(f"\n=== primary_block: {len(res.primary_block)} chars ===")
