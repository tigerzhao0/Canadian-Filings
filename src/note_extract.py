#!/usr/bin/env python3
"""Extract financial values from the NOTES to the financial statements.

Why: the statement FACE carries only summary lines. GuruFocus/Morningstar source a
large class of values from the notes -- Gross PP&E and accumulated depreciation (the
PP&E note), the accounts-payable / accrued split (payables note), and the receivables
breakdown. Measured against the GuruFocus cross-check, ~6,700 of our zero-filled cells
are exactly these notes-only items. The whole document text is already cached in
`filings.db.pdf_raw_pages` (written pre-boundary-trim), so this needs NO re-download.

Precedent: `pdf_extract._find_finance_expense_note` already does this for one note and
documents the contract this module keeps -- notes are *optional enrichment*, never a
requirement, and never override a face value.

    python src/note_extract.py --tickers WPK,DSG --limit 5     # inspect
    python src/note_extract.py --all                            # populate raw_note_items

DESIGN NOTE -- why this emits CANONICAL KEYS directly, instead of raw labels for the
general matcher: note pages mix policy prose with several unrelated tables, so feeding
them to the statement alias index would invite confident-but-wrong mappings. Instead
each supported note type has an explicit, narrow row-pattern table, and only the
targeted keys are emitted. Precision over recall by design.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pdf_extract import (  # noqa: E402  reuse the proven boundary machinery
    _notes_boundary, _AUDITOR_RE, _collapse,
)
from rule_extract import _detect_columns, _split_line, _to_number, _detect_scale  # noqa: E402

FILINGS_DB = ROOT / "output" / "filings.db"

# A numbered-note header. The repo's existing _NUMBERED_NOTE_RE demands "N." with a
# period, but real filings print "22 ACCOUNTS PAYABLE AND ACCRUED" and "19 PROPERTY
# AND EQUIPMENT" with no punctuation at all (verified on WSP), so the period is
# optional here. Requires an uppercase start so "12 345" (a value row) can't match.
_NOTE_HEAD_RE = re.compile(r"^\s*(\d{1,2})\s*[\.\)]?\s+([A-Z][A-Za-z,&'’\- ]{3,70})\s*$")
# The notes TABLE OF CONTENTS lists the same headers with dot leaders / page refs
# ("1 BASIS OF PRESENTATION ......... F-14"). Those are not note starts.
_TOC_LINE_RE = re.compile(r"\.{3,}|\s(?:F-)?\d{1,3}\s*$")

# ---------------------------------------------------------------- note type registry
# (note_key, title regex, target statement). Titles are matched against the note's
# header text, lowercased.
NOTE_TYPES: list[tuple[str, re.Pattern, str]] = [
    ("ppe", re.compile(r"propert(y|ies).{0,30}(plant|equipment)|premises and equipment"
                       r"|property and equipment|immobilisations corporelles"), "balance_sheet"),
    ("payables", re.compile(r"(accounts?|trade).{0,20}payable|payables and accrued"
                            r"|creditors and accr"), "balance_sheet"),
    ("receivables", re.compile(r"(accounts?|trade).{0,20}receivable|receivables and other"
                               r"|debtors"), "balance_sheet"),
    ("intangibles", re.compile(r"intangible assets|goodwill and intangible"), "balance_sheet"),
]

# Row patterns per note type: (canonical_key, row-label regex, take_abs).
# Deliberately narrow -- a row must look unmistakably like the concept.
ROW_PATTERNS: dict[str, list[tuple[str, re.Pattern, bool]]] = {
    "ppe": [
        # rollforward matrices label the gross block "Cost" (sometimes "Gross carrying
        # amount"); accumulated depreciation is the contra line. GuruFocus stores both
        # as POSITIVE magnitudes, matching schema_map._ABS_VALUE_KEYS for AccumDep.
        # Year-column PP&E notes instead print the asset classes then a gross subtotal
        # and a "Less: accumulated depreciation" line (verified on TPX.B).
        ("GrossPPE", re.compile(
            r"^(cost|gross carrying amount|gross book value)$"
            r"|^(total )?(gross )?propert(y|ies),? plant and equipment,?( at)? (cost|gross)$"
            r"|^total (cost|gross)$"), True),
        ("AccumulatedDepreciation", re.compile(
            r"^(less:?\s*)?accumulated (depreciation|amortization|amortisation|depletion)"
            r"( and (impairment|depletion|amortization))?( losses)?$"), True),
    ],
    # take_abs=True on every balance-sheet magnitude below: filers differ on whether a
    # liability/contra is printed positive or parenthesised (WPK prints trade payables
    # as "(64,005)"), while the canonical schema and GuruFocus both store these as
    # POSITIVE magnitudes. abs() is a no-op for the common positive case.
    # Patterns widened from a corpus scan of the real row labels inside matched notes.
    # Where both a gross and a NET row exist ("Trade receivables" then "Net trade
    # receivables"), the net row appears later and overwrites -- which is what we want,
    # since the balance-sheet carrying amount (and GuruFocus) is net of allowance.
    "payables": [
        ("AccountsPayable", re.compile(
            r"^(trade|accounts?)( accounts?)? payables?$"
            r"|^trade and other payables?$"
            r"|^(trade )?accounts? payable( and accrued (liabilities|expenses))?$"), True),
        ("CurrentAccruedExpenses", re.compile(
            r"^accrued (liabilities|expenses|charges|payables?)"
            r"( and other( payables?| liabilities)?)?$"
            r"|^other current liabilities and accrued expenses$"), True),
    ],
    "receivables": [
        ("AccountsReceivable", re.compile(
            r"^(trade|accounts?)( accounts?)? receivables?$"
            r"|^(net )?trade receivables?(,? net( of .{0,30})?)?$"
            r"|^(net )?accounts? receivable(,? net( of .{0,30})?)?$"
            r"|^trade accounts receivable(,? net of provision)?$"), True),
        ("OtherReceivables", re.compile(r"^other receivables?$"
                                        r"|^other trade receivables?$"), True),
    ],
    "intangibles": [
        ("Goodwill", re.compile(r"^goodwill$"), True),
    ],
}

# PP&E rollforward year anchors. Two flavours, and the distinction is load-bearing:
#   "At January 1, 2024"      -> the OPENING balance, i.e. FY2023's closing figure
#   "At December 29, 2024"    -> the CLOSING balance, i.e. FY2024
# Attributing an opening balance to its printed year would shift every value one year
# (verified on WPK: Cost 1,143,340 at Jan-1-2024 is the FY2023 number; FY2024 is
# 1,269,085 at Dec-29-2024).
# Real filings write the anchor every which way -- "Balance, December 31, 2024",
# "Balance at December 31, 2024", "At December 29, 2024", "December 31, 2024" -- so the
# optional prefix words are all comma/space tolerant and the MONTH must be a real month
# name (a bare "31, 2024" or a stray number line must never anchor a block).
_AT_DATE_RE = re.compile(
    r"^(?:net book value|carrying amount|balance)?[\s,]*(?:as\s+)?(?:at|of)?[\s,]*"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*"
    r"(\d{1,2})?[\s,]*(\d{4})\s*$", re.I)
_ACTIVITY_YEAR_RE = re.compile(r"^(\d{4})\s+activity\s*$", re.I)


def _decode_pages(blob: bytes) -> list[str]:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def iter_notes(pages: list[str]) -> list[tuple[str, str]]:
    """Slice the notes region into individual notes -> [(header_text, note_text)].

    Per-NOTE slicing (not per-page) matters twice over: two notes commonly share one
    page, and schema_map._looks_like_prose would zero out a whole page that mixes
    policy prose with a table."""
    npages = len(pages)
    collapsed = [_collapse(p) for p in pages]
    # start after the auditor's report, exactly as extract_statements does
    lo = 0
    for i in range(npages):
        if _AUDITOR_RE.search(collapsed[i]):
            lo = i + 1
            break
    nb = _notes_boundary(pages, collapsed, lo, npages)
    if nb is None:
        # FALLBACK (30% of cached documents hit this): many filers never print the
        # "Notes to the consolidated financial statements" banner -- the notes simply
        # begin at "1. NATURE OF OPERATIONS". Anchor on the first NUMBERED NOTE header
        # found after the auditor's report instead. Safe because a block is only read
        # when its TITLE matches a target note type, and statement faces don't carry
        # numbered section headers.
        for i in range(lo, npages):
            if any(_NOTE_HEAD_RE.match(ln.strip())
                   for ln in pages[i].splitlines()[:40]
                   if ln.strip() and not _TOC_LINE_RE.search(ln.strip())):
                nb = i
                break
    if nb is None:
        return []

    # flatten the notes region into (line, page_idx) and cut at each note header
    lines: list[str] = []
    for i in range(nb, npages):
        lines.extend(pages[i].splitlines())

    heads: list[tuple[int, str]] = []   # (line index, title)
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if not s or _TOC_LINE_RE.search(s):
            continue                      # table-of-contents entry, not a note start
        m = _NOTE_HEAD_RE.match(s)
        if m:
            title = m.group(2).strip()
            # note titles wrap onto the next line ("22 ACCOUNTS PAYABLE AND ACCRUED" /
            # "LIABILITIES") -- absorb an immediately-following all-caps continuation.
            nxt = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if nxt and nxt.isupper() and len(nxt) < 45 and not _NOTE_HEAD_RE.match(nxt):
                title = f"{title} {nxt}"
            heads.append((idx, title))
    out: list[tuple[str, str]] = []
    for n, (start, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        if end - start < 2:
            continue
        out.append((title, "\n".join(lines[start:end])))
    return out


_NULLISH = {"-", "–", "—", "n/a", "na", "nil"}


def _last_number(line: str) -> float | None:
    """Right-anchored TOTAL column of a matrix row. A PP&E rollforward's columns are
    asset classes with Total last, so the final numeric token is the figure we want.
    Bare dashes (empty cells) are skipped rather than treated as the end of the run --
    a matrix row routinely ends '... (13,642) - (599,953)'."""
    for t in reversed(line.split()):
        if t == "$" or t.endswith("%") or t.lower() in _NULLISH:
            continue
        v = _to_number(t)
        if v is not None:
            return v
        break
    return None


def _anchor_year(line: str) -> int | None:
    """Year a rollforward block belongs to, honouring opening-vs-closing semantics
    (see _AT_DATE_RE). Returns None when the line isn't a year anchor."""
    m = _ACTIVITY_YEAR_RE.match(line)
    if m:
        return int(m.group(1))
    m = _AT_DATE_RE.match(line)
    if not m:
        return None
    month, day, year = m.group(1).lower(), m.group(2), int(m.group(3))
    if not (1990 <= year <= 2100):
        return None
    # an opening balance dated in the first days of January is the PRIOR year's close
    if month.startswith("jan") and (day is None or int(day) <= 7):
        return year - 1
    return year


def parse_matrix_note(text: str, note_key: str = "ppe") -> dict[int, dict[str, float]]:
    """PP&E-style rollforward: rows are Cost / Accumulated depreciation, columns are
    asset classes (Total last), and successive YEAR BLOCKS are anchored by dated
    'At <date>' lines. Returns {year: {canonical_key: value}}.

    Only BALANCE blocks are read (a 'Cost' row directly under a dated anchor); the
    activity rows in between (Additions/Disposals/Depreciation) are movement, not a
    balance, and are deliberately ignored."""
    out: dict[int, dict[str, float]] = {}
    cur_year: int | None = None
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        y = _anchor_year(s)
        if y is not None:
            cur_year = y
            continue
        if cur_year is None:
            continue
        lab_toks = []
        for t in s.split():
            # stop at the first VALUE cell -- including an empty '-' cell, which is a
            # column placeholder, not part of the label ("Accumulated depreciation -
            # (103,232) ..." must reduce to "accumulated depreciation")
            if _to_number(t) is not None or t == "$" or t.lower() in _NULLISH:
                break
            lab_toks.append(t)
        label = " ".join(lab_toks).strip().lower().rstrip(":")
        # patterns must follow the NOTE TYPE: an intangibles rollforward also has a
        # "Cost" row, and mapping that to GrossPPE would be flatly wrong.
        for key, rx, take_abs in ROW_PATTERNS.get(note_key, []):
            if rx.match(label):
                v = _last_number(s)
                if v is not None:
                    # a later block for the same year (the closing restated block)
                    # legitimately overwrites an earlier one -- same figure either way
                    out.setdefault(cur_year, {})[key] = abs(v) if take_abs else v
    return out


_YEAR_ONLY_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _detect_note_columns(text: str) -> list[int]:
    """Column years for a NOTE table.

    rule_extract._detect_columns takes the header-region line with the most distinct
    years, which misfires inside notes: explanatory PROSE routinely names more years
    than the header does ("Depreciation expense was $505.2 million ... for the years
    ended December 31, 2025, December 31, 2024 and December 31, 2023" -> 3 years,
    beating the real 2-year header and breaking every row split, verified on TPX.B).

    So require the candidate line to LOOK like a header: year tokens must make up a
    large share of it, with little other text. Falls back to the statement detector
    when nothing qualifies."""
    best: list[int] = []
    for ln in text.splitlines()[:30]:
        s = ln.strip()
        if not s:
            continue
        yrs = [int(y) for y in _YEAR_ONLY_RE.findall(s)]
        yrs = [y for y in yrs if 1990 <= y <= 2100]
        if not yrs:
            continue
        seen: list[int] = []
        for y in yrs:
            if y not in seen:
                seen.append(y)
        # header-ness: drop year tokens, month names and separators; whatever remains
        # is "other" text. A real header leaves almost nothing behind.
        rest = _YEAR_ONLY_RE.sub(" ", s)
        rest = re.sub(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
                      " ", rest)
        rest = re.sub(r"[\d\s,\.\$\|\-–—:/()]+", " ", rest).strip()
        if len(rest) > 24:           # a sentence, not a column header
            continue
        if len(seen) > len(best):
            best = seen
    return best or _detect_columns(text)


def parse_column_note(text: str, note_key: str) -> dict[int, dict[str, float]]:
    """Year-column note (payables/receivables/intangibles): a real header row of years
    plus label+value rows. Reuses the statement parser's right-anchored splitting, so
    it inherits its hard-won handling of '$' markers, parenthesised negatives and dash
    cells -- but uses a note-aware header detector (see _detect_note_columns)."""
    years = _detect_note_columns(text)
    if not years:
        return {}
    n = len(years)
    out: dict[int, dict[str, float]] = {}
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        split = _split_line(s, n)
        if not split:
            continue
        label, raw_vals = split
        lab = label.strip().lower().rstrip(":").rstrip(".")
        lab = re.sub(r"\s*\(note[^)]*\)", "", lab).strip()
        for key, rx, take_abs in ROW_PATTERNS.get(note_key, []):
            if not rx.match(lab):
                continue
            for yi, rv in enumerate(raw_vals):
                if rv is None or yi >= len(years):
                    continue
                v = _to_number(rv)
                if v is None:
                    continue
                out.setdefault(years[yi], {})[key] = abs(v) if take_abs else v
    return out


def extract_for_document(pages: list[str], doc_scale_hint: float | None = None
                         ) -> tuple[dict[int, dict[str, float]], float, list[str]]:
    """Whole-document notes pass -> ({year: {canonical_key: printed_value}}, scale, notes_seen)."""
    found: dict[int, dict[str, float]] = {}
    seen: list[str] = []
    notes = iter_notes(pages)
    for title, body in notes:
        low = title.lower()
        for note_key, rx, _stmt in NOTE_TYPES:
            if not rx.search(low):
                continue
            seen.append(f"{note_key}:{title[:40]}")
            # Note LAYOUT does not follow note TYPE: PP&E is sometimes a year-column
            # table (asset classes + "Less: accumulated depreciation") and intangibles
            # /goodwill is often a Cost/AccumAmort rollforward matrix. Run both readers
            # and merge -- the column reader is more explicit, so it wins ties.
            vals: dict[int, dict[str, float]] = {}
            for y, kv in parse_matrix_note(body, note_key).items():
                vals.setdefault(y, {}).update(kv)
            for y, kv in parse_column_note(body, note_key).items():
                vals.setdefault(y, {}).update(kv)
            for y, kv in vals.items():
                found.setdefault(y, {}).update(kv)
            break
    # Scale: the notes' own units banner first ("Tabular figures in millions..."), else
    # the DOCUMENT-level hint step 2 already computed (pdf_extractions.unit_scale_hint).
    # Never guess from magnitude -- the pipeline's standing rule.
    scale = 1.0
    body_all = "\n".join(b for _t, b in notes[:12])
    s, ok = _detect_scale(body_all)
    if ok:
        scale = s
    elif doc_scale_hint:
        scale = doc_scale_hint
    return found, scale, seen


# ------------------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_note_items (
    ticker          TEXT NOT NULL,
    pdf_year        INTEGER NOT NULL,   -- fiscal-year label of the SOURCE PDF
    note_key        TEXT NOT NULL,      -- ppe / payables / receivables / intangibles
    target_statement TEXT NOT NULL,
    canonical_key   TEXT NOT NULL,
    col_year        INTEGER NOT NULL,   -- comparative-period year the value belongs to
    value_printed   REAL,               -- as printed, pre-scale
    unit_scale      REAL,
    parsed_at       TIMESTAMP,
    PRIMARY KEY (ticker, pdf_year, canonical_key, col_year)
);
CREATE INDEX IF NOT EXISTS idx_raw_note_items_ticker ON raw_note_items(ticker, col_year);
"""
_STMT_OF_KEY = {"GrossPPE": "balance_sheet", "AccumulatedDepreciation": "balance_sheet",
                "AccountsPayable": "balance_sheet", "CurrentAccruedExpenses": "balance_sheet",
                "AccountsReceivable": "balance_sheet", "OtherReceivables": "balance_sheet",
                "Goodwill": "balance_sheet"}


def run(tickers: set[str] | None = None, limit: int | None = None,
        db: Path = FILINGS_DB, progress=print) -> dict:
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    hints = {(t, y): h for t, y, h in conn.execute(
        "SELECT ticker, fiscal_year, unit_scale_hint FROM pdf_extractions")}
    q = "SELECT ticker, fiscal_year, pages_compressed FROM pdf_raw_pages"
    args: list = []
    if tickers:
        q += f" WHERE ticker IN ({','.join('?' * len(tickers))})"
        args = list(tickers)
    q += " ORDER BY ticker, fiscal_year DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q, args).fetchall()
    t0 = time.time()
    docs = vals = 0
    notes_hit = 0
    out: list[tuple] = []
    for i, (tk, fy, blob) in enumerate(rows, 1):
        try:
            pages = _decode_pages(blob)
        except Exception:
            continue
        try:
            found, scale, seen = extract_for_document(pages, hints.get((tk, fy)))
        except Exception as exc:            # never let one bad doc stop the pass
            progress(f"  ! {tk} {fy}: {exc}")
            continue
        docs += 1
        if seen:
            notes_hit += 1
        for y, kv in found.items():
            for key, v in kv.items():
                out.append((tk, fy, _note_key_of(key), _STMT_OF_KEY.get(key, "balance_sheet"),
                            key, y, v, scale, time.strftime("%Y-%m-%d %H:%M:%S")))
                vals += 1
        if i % 200 == 0:
            progress(f"  ... {i}/{len(rows)} docs, {vals} note values")
    conn.executemany(
        "INSERT OR REPLACE INTO raw_note_items (ticker,pdf_year,note_key,target_statement,"
        "canonical_key,col_year,value_printed,unit_scale,parsed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        out)
    conn.commit()
    conn.close()
    return {"docs": docs, "with_notes": notes_hit, "values": vals,
            "elapsed": time.time() - t0}


def _note_key_of(canonical_key: str) -> str:
    if canonical_key in ("GrossPPE", "AccumulatedDepreciation"):
        return "ppe"
    if canonical_key in ("AccountsPayable", "CurrentAccruedExpenses"):
        return "payables"
    if canonical_key in ("AccountsReceivable", "OtherReceivables"):
        return "receivables"
    return "intangibles"


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract values from financial-statement NOTES.")
    ap.add_argument("--tickers", help="Comma-separated allow-list.")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="Process every cached document.")
    ap.add_argument("--inspect", action="store_true",
                    help="Print the notes found per document instead of storing.")
    args = ap.parse_args()
    tks = {t.strip() for t in args.tickers.split(",")} if args.tickers else None

    if args.inspect:
        conn = sqlite3.connect(FILINGS_DB)
        q = "SELECT ticker, fiscal_year, pages_compressed FROM pdf_raw_pages"
        a: list = []
        if tks:
            q += f" WHERE ticker IN ({','.join('?' * len(tks))})"; a = list(tks)
        q += " ORDER BY fiscal_year DESC"
        if args.limit:
            q += f" LIMIT {int(args.limit)}"
        for tk, fy, blob in conn.execute(q, a):
            pages = _decode_pages(blob)
            hint = conn.execute("SELECT unit_scale_hint FROM pdf_extractions "
                                "WHERE ticker=? AND fiscal_year=?", (tk, fy)).fetchone()
            found, scale, seen = extract_for_document(pages, hint[0] if hint else None)
            print(f"\n=== {tk} {fy}  (pages={len(pages)}, scale={scale:,.0f}) ===")
            print(f"  notes matched: {seen}")
            for y in sorted(found, reverse=True)[:4]:
                print(f"   {y}: " + ", ".join(f"{k}={v:,.1f}" for k, v in found[y].items()))
        conn.close()
        return

    if not (args.all or tks):
        print("Pass --all or --tickers.", file=sys.stderr); sys.exit(1)
    r = run(tickers=tks, limit=args.limit)
    print("\n" + "=" * 46)
    print("  Notes extraction complete")
    print("=" * 46)
    print(f"  documents scanned      : {r['docs']}")
    print(f"  with a target note     : {r['with_notes']}")
    print(f"  note values stored     : {r['values']}  (table 'raw_note_items')")
    print(f"  elapsed                : {r['elapsed']:.1f}s")


if __name__ == "__main__":
    main()
