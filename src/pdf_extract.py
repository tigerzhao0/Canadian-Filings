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
    sections: dict[str, str] = field(default_factory=dict)  # income_statement / balance_sheet / cash_flow
    primary_block: str = ""                     # the whole primary-statements span (fallback for the LLM)
    reason: str | None = None                   # why ok=False / a note


# Header regexes, matched against COLLAPSED-whitespace lowercased text so
# inconsistent word-spacing in the PDF text layer doesn't defeat them.
_AUDITOR_RE = re.compile(r"independent auditor|auditor'?s report|report of independent")
_NOTES_RE = re.compile(r"notes to (the )?(consolidated )?(interim )?financial statements")
_STATEMENT_RES = {
    "balance_sheet": re.compile(r"statements? of financial position|balance sheets?"),
    "income_statement": re.compile(
        r"statements? of (loss|income|operations|comprehensive (income|loss)|profit)"),
    "cash_flow": re.compile(r"statements? of cash ?flows?"),
}


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _page_texts(pdf_bytes: bytes) -> list[str] | None:
    """Per-page text via pdfplumber; None if pdfplumber can't open it at all."""
    try:
        import pdfplumber
    except Exception:  # noqa: BLE001 - dependency missing
        return None
    import io
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return [(p.extract_text() or "") for p in pdf.pages]
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
    notes_page = next((i for i in range(start, npages) if _NOTES_RE.search(collapsed[i])), None)
    end = notes_page if notes_page is not None else npages
    if end <= start:  # markers crossed/missing -> fall back to the whole doc
        start, end = 0, npages

    primary_pages = list(range(start, end))
    primary_block = "\n".join(pages[i] for i in primary_pages).strip()
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
        matched = next((name for name, rx in _STATEMENT_RES.items() if rx.search(head)), None)
        if matched:
            current, cont = matched, 0
            sections[current] = (sections.get(current, "") + "\n" + pages[i]).strip()
        elif current and cont < MAX_CONT:
            # No new statement header at the top -> a continuation page of the
            # statement we're currently in (statements often spill over one page).
            cont += 1
            sections[current] = (sections[current] + "\n" + pages[i]).strip()
        else:
            current = None  # cap reached / nothing active -> stop absorbing

    return ExtractResult(ok=True, npages=npages, sections=sections,
                        primary_block=primary_block)


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
