"""run.py --step 3: separate the raw statement TEXT (step 2) into individual
LINE ITEMS, stored one row per (label, year, value) in filings.db
`raw_line_items`. EVERY parsed line is stored -- mapped or not -- so nothing
from the PDF is lost; canonical mapping is step 4's job.

Parser upgrades over the old inline parse (measured on RY's real statements):
- SECTION TRACKING: a short no-value line ("Interest and dividend income
  (Note 3)", "Loans (Note 5)", "Deposits (Note 14)") sets the current section;
  every row records it. "Loans" under Interest income is a different concept
  than "Loans" under Assets -- the section is what disambiguates them.
- VALUE-ONLY SUBTOTALS: a line of bare numbers ("1,049,515 987,417") is the
  running total of the current section. It gets a synthetic label
  "{section} total" ("Loans total" -> GrossLoan) or "{section} net total"
  when it directly follows an allowance line ("Loans net total" -> NetLoan).
  These bare lines ARE the headline bank figures (GrossLoan, NetLoan,
  InterestIncome, InterestExpense, TotalDeposits) and were previously dropped.
- JUNK GUARD: rows whose values are just the column years / page numbers are
  rejected (page furniture is also stripped upstream in step 2).
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_extract import FILINGS_SCHEMA, STATEMENTS, _to_number
from rule_extract import (
    _detect_columns, _detect_currency, _detect_period_ends, _detect_scale,
    _split_line,
)

# Lines that must never become a section header (date headers, units notes,
# column headers, statement titles) -- they are layout, not accounting sections.
_NON_SECTION_RE = re.compile(
    r"year(s)? ended|as at|months? ended|in (thousands|millions)|"
    r"(thousands|millions) of|canadian dollars|u\.?s\.? dollars|"
    r"statements? of|consolidated|balance sheets?|^\$[\s$]*$|"
    r"^(october|november|december|january|february|march|april|may|june|july|"
    r"august|september)\b", re.I)
_NOTE_REF_RE = re.compile(r"\s*\((notes?|note)\b[^)]*\)", re.I)
_MAX_SECTION_WORDS = 8
# Top-level balance-sheet ZONES: disambiguate identical labels/sections that
# occur on both sides ("Derivatives" under assets vs under liabilities).
_ZONE_RE = re.compile(r"^(assets|liabilities( and equity)?|equity"
                      r"|liabilities and shareholders'? equity)\s*$", re.I)
# The subtotal after an allowance DEDUCTION line is the NET total ("Loans net
# total" -> NetLoan). Prefix-anchored: "Allowance for loan losses" qualifies,
# "Investment, net of applicable allowance" (allowance mentioned mid-label,
# not a deduction row) does not.
_ALLOWANCE_ROW_RE = re.compile(r"^\s*(less[:\s]+)?allowance", re.I)


def _clean_section(line: str) -> str:
    """Human-readable section name: drop note refs + trailing $/colon."""
    s = _NOTE_REF_RE.sub("", line).strip()
    s = re.sub(r"[\s:$]+$", "", s)
    return s


@dataclass
class ParsedLine:
    line_no: int
    section: str | None
    label: str
    values: list          # one float|None per column
    synthetic: bool = False   # label derived from section (value-only subtotal)
    zone: str | None = None   # assets / liabilities / equity (balance sheet)


@dataclass
class ParsedStatement:
    col_years: list[int] = field(default_factory=list)
    period_ends: dict = field(default_factory=dict)
    scale: float = 1.0
    scale_found: bool = False
    currency: str | None = None
    rows: list[ParsedLine] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def parse_lines(text: str, statement_type: str,
                col_years_hint: list[int] | None = None) -> ParsedStatement:
    """TEXT -> every individual line item, with section context. Values are AS
    PRINTED (pre-scale). Never raises.

    col_years_hint: fallback column years from a SIBLING statement in the same
    filing, used only when this statement's OWN text has no year header. Root
    cause (verified on TTZ and a 60-company-year sample: 55/60, 92%): a company
    prints the "As at <date>, <YYYY> and <YYYY>" header ABOVE the statement
    title rather than as a column row within it, so the captured section text
    starts straight into "ASSETS / Current Assets / Cash ... 879,328  842,584"
    with no year token anywhere -- _detect_columns correctly finds nothing. All
    three statements in one filing share the same fiscal-year columns, so a
    sibling that DID find a header (very often true -- e.g. this exact failure
    hits the balance sheet while income_statement/cash_flow parse fine) is a
    safe, structurally-guaranteed borrow -- not a guess."""
    out = ParsedStatement()
    out.scale, out.scale_found = _detect_scale(text)
    out.currency = _detect_currency(text)
    out.col_years = _detect_columns(text)
    borrowed = False
    if not out.col_years and col_years_hint:
        out.col_years = list(col_years_hint)
        borrowed = True
    n_cols = len(out.col_years)
    if n_cols == 0:
        out.flags.append("no_columns")
        return out
    if borrowed:
        out.flags.append("borrowed_columns")
    out.period_ends = _detect_period_ends(text, out.col_years)

    section: str | None = None
    zone: str | None = None
    pending_prefix = ""
    prev_data_label = ""
    line_no = 0
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        parsed = _split_line(ln, n_cols)
        if parsed is None:
            # No trailing value cells: either layout noise, a section header,
            # a top-level zone header, or the first half of a wrapped label.
            if _ZONE_RE.match(ln):
                zone = _clean_section(ln).lower()
                section = None
                pending_prefix = ""
                prev_data_label = ""
                continue
            if _NON_SECTION_RE.search(ln):
                continue
            words = ln.split()
            if len(words) <= _MAX_SECTION_WORDS:
                section = _clean_section(ln)
            # Either way it may also be a wrapped label's first line.
            pending_prefix = ln
            prev_data_label = ""
            continue

        label, values = parsed
        nums = [_to_number(v) for v in values]
        # Junk guard: the column-header line itself ("2025 2024") and
        # (year, page#) furniture remnants. A YEAR prints without a thousands
        # separator ("2025"); a money value of that size prints WITH one
        # ("2,025") -- so a bare 4-digit token matching a column year is a
        # year token, not data.
        def _year_like(raw) -> bool:
            return (isinstance(raw, str) and raw.isdigit() and len(raw) == 4
                    and int(raw) in out.col_years)
        non_null = [n for n in nums if n is not None]
        if non_null and all(float(n).is_integer() and int(n) in out.col_years
                            for n in non_null):
            pending_prefix = ln
            continue
        if values and _year_like(values[0]) and all(
                v is None or _year_like(v)
                or (isinstance(v, str) and v.isdigit() and int(v) < 2500)
                for v in values):
            # leading column-year token + small page-number-ish ints -> furniture
            continue

        if label and not re.search(r"[A-Za-z]", label):
            # '$'/punctuation-only "label" (currency-marker column): the real
            # label is the preceding no-value line when one is pending;
            # otherwise treat as a value-only line (section running total).
            if pending_prefix:
                label = pending_prefix
                pending_prefix = ""
            else:
                label = ""

        if not label:
            # VALUE-ONLY LINE = the running total of the current section.
            if section is None:
                if zone:   # zone-level running total ("Assets ... 1,234")
                    section = _clean_section(zone)
                else:
                    out.flags.append("orphan_subtotal")
                    continue
            if _ALLOWANCE_ROW_RE.match(prev_data_label):
                label = f"{section} net total"
            else:
                label = f"{section} total"
            line_no += 1
            out.rows.append(ParsedLine(line_no, section, label, nums,
                                       synthetic=True, zone=zone))
            prev_data_label = label
            continue

        # Wrapped label: attach a preceding no-value line when this row's own
        # label continues in lowercase ("for the year ...").
        if pending_prefix and label[:1].islower():
            label = (pending_prefix + " " + label).strip()
        pending_prefix = ""
        line_no += 1
        out.rows.append(ParsedLine(line_no, section, label, nums, zone=zone))
        prev_data_label = label

    if not out.rows:
        out.flags.append("no_rows")
    return out


# ---------------------------------------------------------------------------
# primary_block slicing: when step 2 stored a statement section EMPTY but the
# whole primary-statements span exists, split the span at statement-title
# lines and use the right slice. (The old fallback parsed the ENTIRE block as
# each missing statement -- a balance sheet's rows showed up as "cash flow".)
# ---------------------------------------------------------------------------

_SLICE_HDR_RES = {
    # "_equity" is a BOUNDARY-ONLY mark: a changes-in-equity/net-assets title
    # ends the previous statement's slice but never claims one itself.
    # Checked FIRST so "statements of changes in net assets" never falls
    # through to the balance_sheet "statements of net assets" pattern.
    "_equity": re.compile(
        r"statements? of changes in (net assets|equity|shareholders'? equity"
        r"|unitholders'? equity)|statement of equity", re.I),
    "income_statement": re.compile(
        r"statements? of (comprehensive )?(income|loss|operations|earnings)|"
        r"statements? of profit|statements? of loss and comprehensive loss|"
        r"[ée]tats? du r[ée]sultat", re.I),
    "balance_sheet": re.compile(
        r"balance sheets?|statements? of financial position|"
        r"statements? of net assets\b|"
        r"[ée]tats? de la situation financi[èe]re", re.I),
    "cash_flow": re.compile(
        r"statements? of cash flows?|cash flow statements?|"
        r"tableaux? des flux de tr[ée]sorerie", re.I),
}


def slice_primary(text: str) -> dict[str, str]:
    """Split a primary-statements block into per-statement segments at title
    lines. Keeps the FIRST occurrence per statement (the proper statement
    precedes OCI/equity look-alikes)."""
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or len(s) > 90:
            continue
        for stmt, rx in _SLICE_HDR_RES.items():
            if rx.search(s):
                marks.append((i, stmt))
                break
    segs: dict[str, str] = {}
    for j, (i, stmt) in enumerate(marks):
        if stmt == "_equity":     # boundary only, never a segment of its own
            continue
        end = marks[j + 1][0] if j + 1 < len(marks) else len(lines)
        if stmt not in segs:
            segs[stmt] = "\n".join(lines[i:end])
    return segs


# ---------------------------------------------------------------------------
# Storage (filings.db raw_line_items) + step-3 runner
# ---------------------------------------------------------------------------

_RAW_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS raw_line_items (
    ticker          TEXT NOT NULL,
    pdf_year        INTEGER NOT NULL,   -- fiscal year label of the SOURCE PDF
    statement_type  TEXT NOT NULL,
    line_no         INTEGER NOT NULL,   -- order within the statement
    section         TEXT,
    zone            TEXT,               -- assets / liabilities / equity (balance sheet)
    label           TEXT NOT NULL,
    synthetic       INTEGER DEFAULT 0,  -- 1 = subtotal label derived from section
    col_year        INTEGER NOT NULL,   -- the comparative-period fiscal year
    period_end      TEXT,
    value_printed   REAL,               -- as printed, pre-scale (NULL = dash cell)
    unit_scale      REAL,
    currency        TEXT,
    parsed_at       TIMESTAMP,
    PRIMARY KEY (ticker, pdf_year, statement_type, line_no, col_year)
);
CREATE INDEX IF NOT EXISTS idx_raw_line_items_ticker
    ON raw_line_items(ticker, col_year);
"""


def _rows_to_parse(conn, tickers: set[str] | None, limit: int | None,
                   force: bool) -> list[dict]:
    done: set[tuple] = set()
    if not force:
        try:
            done = {(t, y) for t, y in conn.execute(
                "SELECT DISTINCT ticker, pdf_year FROM raw_line_items")}
        except sqlite3.OperationalError:
            pass
    cur = conn.execute(
        "SELECT ticker, fiscal_year, income_statement, balance_sheet, cash_flow, "
        "primary_block, unit_scale_hint, doc_type FROM pdf_extractions "
        "WHERE extract_ok = 1 AND scanned = 0 ORDER BY ticker, fiscal_year DESC")
    names = [c[0] for c in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    rows = [r for r in rows
            if (r.get("doc_type") or "primary_statements") in
               ("primary_statements", "annual_report_wrapper")]
    if tickers:
        rows = [r for r in rows if r["ticker"] in tickers]
    rows = [r for r in rows if (r["ticker"], r["fiscal_year"]) not in done]
    if limit:
        rows = rows[:limit]
    return rows


def run_line_item_parsing(cfg, *, force: bool = False, limit: int | None = None,
                          tickers: set[str] | None = None, progress=None) -> dict:
    db_path = cfg.get("storage", {}).get("db_path", "output/filings.db")
    conn = sqlite3.connect(db_path)
    t0 = time.monotonic()
    try:
        conn.executescript(FILINGS_SCHEMA.read_text(encoding="utf-8"))
        conn.executescript(_RAW_TABLE_SQL)
        conn.commit()
        rows = _rows_to_parse(conn, tickers, limit, force)
        if progress:
            progress(f"Line-item parsing: {len(rows)} pdf row(s) from {db_path}")

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        total_lines = statements_done = empty = 0
        for row in rows:
            ticker, pdf_year = row["ticker"], row["fiscal_year"]
            conn.execute("DELETE FROM raw_line_items WHERE ticker=? AND pdf_year=?",
                         (ticker, pdf_year))
            sliced: dict[str, str] | None = None
            stmt_texts: dict[str, str] = {}
            for stmt in STATEMENTS:
                text = (row.get(stmt) or "").strip()
                if not text:
                    if sliced is None:
                        sliced = slice_primary((row.get("primary_block") or ""))
                    text = (sliced.get(stmt) or "").strip()
                stmt_texts[stmt] = text
            # Sibling-statement year fallback (see parse_lines docstring): detect
            # columns from whichever statement's OWN text has a header, so a
            # statement missing its header (year printed above the title, not in
            # it) can still be parsed instead of contributing zero rows.
            sibling_years: list[int] = []
            for stmt, text in stmt_texts.items():
                if text:
                    yrs = _detect_columns(text)
                    if yrs:
                        sibling_years = yrs
                        break
            for stmt in STATEMENTS:
                text = stmt_texts[stmt]
                if not text:
                    empty += 1
                    continue
                p = parse_lines(text, stmt, col_years_hint=sibling_years)
                if not p.rows:
                    empty += 1
                    continue
                # unit_scale NULL when the section text had no units note --
                # step 4 then falls back to pdf_extractions.unit_scale_hint.
                scale_val = p.scale if p.scale_found else None
                data = []
                for r in p.rows:
                    for i, v in enumerate(r.values):
                        y = p.col_years[i]
                        data.append((ticker, pdf_year, stmt, r.line_no, r.section,
                                     r.zone, r.label, 1 if r.synthetic else 0, y,
                                     p.period_ends.get(y), v, scale_val,
                                     p.currency, now))
                conn.executemany(
                    "INSERT OR REPLACE INTO raw_line_items (ticker, pdf_year, "
                    "statement_type, line_no, section, zone, label, synthetic, "
                    "col_year, period_end, value_printed, unit_scale, currency, "
                    "parsed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
                total_lines += len(p.rows)
                statements_done += 1
                if progress:
                    progress(f"  {ticker} {pdf_year} {stmt:16} -> {len(p.rows)} lines "
                             f"({sum(1 for r in p.rows if r.synthetic)} subtotals)")
            conn.commit()
    finally:
        conn.close()
    return {"pdf_rows": len(rows), "statements": statements_done,
            "lines": total_lines, "empty": empty,
            "elapsed": time.monotonic() - t0, "db_path": db_path}
