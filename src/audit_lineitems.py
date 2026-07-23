"""Read-only line-item audit: reconcile the spreadsheet output against the source PDFs.

For each template cell of a company we cross-check the finished value in
`statement_lines` against what is actually on the printed page, along three axes:

  A. mapped-value accuracy  -- magnitude AND sign (parentheses); reproduce the
     pipeline's own store-time transform and confirm it lands on the stored value.
  B. zero-fill correctness  -- every 0-filled ("absent_on_face") cell must have NO
     findable line on the statement FACE. A face hit with a real number is a
     missed-mapping BUG; a notes-only hit is a lower-severity "worth a look".
  C. derived-value soundness -- the accounting identity that produced it still holds.

READ-ONLY: both DBs are opened in `mode=ro`; nothing here writes or mutates the
pipeline. The tool only FLAGS candidates -- a human adjudicates the non-PASS rows
against the real PDF. It imports the pipeline's own constants/functions so its
reverse-transform tracks `schema_map`/`derive`/`company_xlsx_export` exactly.

Usage:
    python src/audit_lineitems.py TICKER[,TICKER...] [--years 2023,2024]
                                  [--pdf PATH] [--out DIR]

`--pdf` re-extracts a real PDF with the pipeline's own `_page_texts` for a true
end-to-end check (catches Step-2 text bugs the cache can't); without it the audit
runs off the cached `pdf_raw_pages` / `pdf_extractions` text (validates the
store->display steps only).
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sqlite3
import zlib
from pathlib import Path

# Pipeline constants/functions -- import so the audit tracks behaviour exactly.
from derive import fill_metric_keys
from rule_extract import (
    CURATED_ALIASES, _humanize, _normalize_label, _identity_checks, FUZZY_THRESHOLD,
)
from llm_extract import NO_SCALE_KEYS
from schema_map import (
    AGGREGATE_KEYS, _PER_SHARE_LABEL_RE, _ABS_VALUE_KEYS, _NEGATE_KEYS,
    _NET_INCOME_KEYS, _is_pure_loss_label,
)
from company_xlsx_export import MILLIONS_DIVISOR, _NEGATE_ON_DISPLAY

ROOT = Path(__file__).resolve().parent.parent
SRC_DB = ROOT / "output" / "filings.db"
FIN_DB = ROOT / "output" / "pdf_financials.db"

# A ticker renders the bank template if it mapped any of these.
_BANK_MARKER_KEYS = ("GrossLoan", "TotalDeposits", "NetInterestIncome", "CashAndDueFromBanks")

# Tolerance for magnitude comparison (relative), matching the pipeline's loose feel.
_REL_TOL = 0.01

# A printed number token: optional $, optional sign/parens, digits w/ commas + decimals.
_TOKEN_RE = re.compile(r"\(?\s*-?\s*\$?\s*\d[\d,]*(?:\.\d+)?\s*\)?")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _num_forms(mag: float) -> list[str]:
    """Plausible printed string forms of a magnitude (for face-text search).
    e.g. 14437.2 -> ['14,437.2', '14437.2']; 378.0 -> ['378', '378.0', ...]."""
    forms: set[str] = set()
    # integer-ish?
    if abs(mag - round(mag)) < 0.05:
        n = int(round(mag))
        forms.add(f"{n:,}")
        forms.add(str(n))
    for dec in (1, 2, 3):
        s = f"{mag:,.{dec}f}"
        if s.endswith("0" * dec) and dec > 1:
            continue
        forms.add(s)
        forms.add(s.replace(",", ""))
    return [f for f in forms if f and f not in ("0", "0.0")]


def _face_sign(text: str, mag: float) -> int | None:
    """Look for `mag` in the statement-face `text` and report the on-page sign:
    -1 if it appears parenthesized / with a leading minus, +1 if bare, None if the
    number can't be located unambiguously (appears with conflicting signs or not
    at all). Advisory only -- an unlocatable token yields SUSPECT, not MISMATCH."""
    if not text:
        return None
    signs: set[int] = set()
    for form in _num_forms(mag):
        # escape the form (commas/dots are literals here)
        pat = re.escape(form)
        for m in re.finditer(pat, text):
            i, j = m.start(), m.end()
            # look at immediate wrappers
            pre = text[max(0, i - 2):i]
            post = text[j:j + 2]
            neg = ("(" in pre and ")" in post) or pre.rstrip().endswith("-") or "(" == pre[-1:]
            signs.add(-1 if neg else 1)
    if not signs or len(signs) > 1:
        return None
    return next(iter(signs))


def _decode_pages(blob: bytes | None) -> list[str]:
    if not blob:
        return []
    try:
        return json.loads(zlib.decompress(blob).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# data loading (per ticker)
# --------------------------------------------------------------------------- #
def _load(ticker: str, years: set[int] | None):
    fin = _ro(FIN_DB)
    src = _ro(SRC_DB)
    try:
        sl = {}  # (fy, stmt, key) -> value
        for r in fin.execute(
            "SELECT fiscal_year, statement_type, line_item, value FROM statement_lines "
            "WHERE ticker=?", (ticker,)):
            sl[(r["fiscal_year"], r["statement_type"], r["line_item"])] = r["value"]

        quality = {}  # (fy, stmt, key) -> match_kind
        for r in fin.execute(
            "SELECT fiscal_year, statement_type, line_item, match_kind "
            "FROM statement_line_quality WHERE ticker=?", (ticker,)):
            quality[(r["fiscal_year"], r["statement_type"], r["line_item"])] = r["match_kind"]

        # line_items_full: winning-pdf originating lines per (fy, stmt, key)
        full = {}  # (fy, stmt, key) -> list[dict(line_no, raw_label, value, src_pdf_year)]
        for r in fin.execute(
            "SELECT fiscal_year, statement_type, source_pdf_year, line_no, raw_label, "
            "canonical_key, value FROM line_items_full WHERE ticker=? AND canonical_key IS NOT NULL",
            (ticker,)):
            full.setdefault((r["fiscal_year"], r["statement_type"], r["canonical_key"]), []).append(dict(
                line_no=r["line_no"], raw_label=r["raw_label"], value=r["value"],
                src_pdf_year=r["source_pdf_year"]))

        # raw_line_items: printed number per (pdf_year, stmt, line_no, col_year)
        raw = {}  # (pdf_year, stmt, line_no, col_year) -> dict(value_printed, unit_scale, label)
        for r in src.execute(
            "SELECT pdf_year, statement_type, line_no, col_year, value_printed, unit_scale, label "
            "FROM raw_line_items WHERE ticker=?", (ticker,)):
            raw[(r["pdf_year"], r["statement_type"], r["line_no"], r["col_year"])] = dict(
                value_printed=r["value_printed"], unit_scale=r["unit_scale"], label=r["label"])

        # sliced statement-face text per (fy, stmt)
        face = {}
        for r in src.execute(
            "SELECT fiscal_year, income_statement, balance_sheet, cash_flow FROM pdf_extractions "
            "WHERE ticker=? AND extract_ok=1", (ticker,)):
            face[(r["fiscal_year"], "income_statement")] = r["income_statement"] or ""
            face[(r["fiscal_year"], "balance_sheet")] = r["balance_sheet"] or ""
            face[(r["fiscal_year"], "cash_flow")] = r["cash_flow"] or ""

        # full pages per fy
        pages = {}
        for r in src.execute(
            "SELECT fiscal_year, pages_compressed FROM pdf_raw_pages WHERE ticker=?", (ticker,)):
            pages[r["fiscal_year"]] = "\n".join(_decode_pages(r["pages_compressed"]))

        all_years = sorted({fy for (fy, _s, _k) in sl})
        if years:
            all_years = [y for y in all_years if y in years]
        is_bank = any(k in _BANK_MARKER_KEYS for (_fy, _s, k) in sl)
        return dict(sl=sl, quality=quality, full=full, raw=raw, face=face,
                    pages=pages, years=all_years, is_bank=is_bank)
    finally:
        fin.close()
        src.close()


# --------------------------------------------------------------------------- #
# reverse transform (reproduce schema_map store-time chain from a printed cell)
# --------------------------------------------------------------------------- #
def _reproduce(printed: float, unit: float, key: str, label: str) -> float:
    per_share = (key in NO_SCALE_KEYS) or bool(_PER_SHARE_LABEL_RE.search(label or ""))
    val = printed if per_share else printed * unit
    if key in _ABS_VALUE_KEYS:
        val = abs(val)
    if key in _NEGATE_KEYS:
        val = -val
    if key in _NET_INCOME_KEYS and val > 0 and _is_pure_loss_label(label or ""):
        val = -val
    return val


def _sheet_value(key: str, stored: float, scale: str = "m") -> float:
    """Reproduce company_xlsx_export display: /1e6 for scale=='m' (all but EPS),
    then negate for the display-only set."""
    v = stored / MILLIONS_DIVISOR if scale == "m" else stored
    if key in _NEGATE_ON_DISPLAY:
        v = -v
    return v


# --------------------------------------------------------------------------- #
# Axis A -- mapped value accuracy (magnitude + sign)
# --------------------------------------------------------------------------- #
def _axis_a(d, fy, stmt, key, V, rows_out):
    lines = d["full"].get((fy, stmt, key))
    if not lines:
        rows_out.append(_row(stmt, key, fy, "mapped", V, None, "SUSPECT",
                             "", None, "no direct raw line (fanout/equivalent/derived-source?)"))
        return
    newest = max(l["src_pdf_year"] for l in lines)
    # Sign check must use the SOURCE pdf's face text (keyed by that pdf's own
    # fiscal-year label), NOT this fiscal_year's -- a value can come from a
    # comparative column of a NEWER filing, whose face is stored under the newer
    # year. Using the wrong doc's face produced false sign mismatches.
    face = d["face"].get((newest, stmt), "")
    win = [l for l in lines if l["src_pdf_year"] == newest]
    is_agg = key in AGGREGATE_KEYS and len(win) > 1
    # A key can have MULTIPLE candidate lines in the newest PDF (e.g. a spurious
    # suffix/fuzzy mis-map alongside the exact one). For a non-aggregate key the
    # reduction stored exactly ONE of them (V); pick the candidate whose value
    # matches V so the audit checks the line the pipeline actually used, not a
    # sibling mis-map. For aggregate keys, keep all win rows (V = their sum).
    if not is_agg and len(win) > 1:
        win = [min(win, key=lambda l: abs((l["value"] or 0.0) - V))]
    label0 = win[0]["raw_label"] or ""

    # recover printed number(s) from raw_line_items
    printed_cells = []
    for l in win:
        rr = d["raw"].get((newest, stmt, l["line_no"], fy))
        if rr and rr["value_printed"] is not None:
            printed_cells.append(rr)

    per_share = (key in NO_SCALE_KEYS) or bool(_PER_SHARE_LABEL_RE.search(label0))

    if not printed_cells:
        rows_out.append(_row(stmt, key, fy, "mapped", V, None, "SUSPECT",
                             label0, newest, "printed cell not locatable in raw_line_items"))
        return

    # effective scale from the winning cell (|full.value| / |value_printed|)
    fv = win[0]["value"]
    pv = printed_cells[0]["value_printed"]
    if per_share:
        unit = 1.0
    elif pv and abs(pv) > 1e-9 and fv is not None:
        ratio = abs(fv) / abs(pv)
        unit = min((1.0, 1000.0, 1_000_000.0), key=lambda u: abs(u - ratio))
    else:
        unit = printed_cells[0]["unit_scale"] or 1.0

    # reproduce the pipeline transform and compare to stored V
    if is_agg:
        reproduced = sum(_reproduce(c["value_printed"], unit, key, label0) for c in printed_cells)
        expected_page = "+".join(f"{c['value_printed']:g}" for c in printed_cells)
    else:
        reproduced = _reproduce(pv, unit, key, label0)
        expected_page = f"{pv:g}"

    sheet = _sheet_value(key, V)
    scale_lbl = "" if per_share else (f" x{int(unit):,}" if unit != 1 else "")
    note = f"label={label0!r}{scale_lbl} sheet={sheet:,.3f}"

    tol = _REL_TOL * max(abs(V), abs(reproduced), 1.0)
    if abs(reproduced - V) > tol:
        rows_out.append(_row(stmt, key, fy, "mapped", V, reproduced, "MISMATCH",
                             label0, newest, "stored value != reproduced transform (scale/sign/abs/negate?) " + note))
        return

    # parenthesis / sign check against the face text (advisory) -- skip abs & aggregate keys
    if not is_agg and key not in _ABS_VALUE_KEYS:
        mag = abs(pv) if per_share else abs(pv)  # printed magnitude
        page_sign = _face_sign(face, mag)
        if page_sign is not None and pv != 0:
            printed_sign = 1 if pv > 0 else -1
            if page_sign != printed_sign:
                rows_out.append(_row(stmt, key, fy, "mapped", V, pv, "MISMATCH",
                                     label0, newest, f"parenthesis/sign: page reads {'(neg)' if page_sign<0 else 'pos'} "
                                     f"but parser stored {printed_sign:+d} — {note}"))
                return
    rows_out.append(_row(stmt, key, fy, "mapped", V, expected_page, "PASS", label0, newest, note))


# --------------------------------------------------------------------------- #
# Axis B -- zero-fill correctness (priority)
# --------------------------------------------------------------------------- #
def _aliases_for(key: str) -> list[str]:
    al = list(CURATED_ALIASES.get(key, []))
    al.append(_humanize(key))
    return [_normalize_label(a) for a in al if a]


def _concept(norm: str) -> str:
    """Drop pure-digit words so a face line ("goodwill 7 155 8") reduces to the
    concept ("goodwill") the mapper would have matched — the number columns are
    separated from the label by the line parser upstream, so we mimic that here."""
    return " ".join(w for w in norm.split() if not w.isdigit())


def _numbered_norm_lines(text: str) -> list[tuple[str, str]]:
    """Precompute (concept, raw) for every line of `text` that carries a number.
    Done ONCE per statement/notes blob, not per cell."""
    out = []
    for ln in (text or "").splitlines():
        if not _TOKEN_RE.search(ln):
            continue
        norm = _concept(_normalize_label(ln))
        if norm and len(norm) >= 3:
            out.append((norm, ln.strip()))
    return out


def _face_alias_hit(aliases: list[str], face_lines: list[tuple[str, str]]):
    """Exact-normalized then difflib-fuzzy over the SMALL statement-face line set."""
    aset = set(aliases)
    for norm, raw in face_lines:
        if norm in aset:
            return (raw, "exact")
    for norm, raw in face_lines:
        for a in aliases:
            if a and len(a) >= 6 and difflib.SequenceMatcher(None, norm, a).ratio() >= FUZZY_THRESHOLD:
                return (raw, "fuzzy")
    return None


def _notes_alias_hit(aliases: list[str], notes_blob: str):
    """Cheap whole-string substring check over the normalized notes blob (no
    difflib — notes are huge). Only multi-char aliases, LOW severity anyway."""
    for a in aliases:
        if a and len(a) >= 6 and a in notes_blob:
            return a
    return None


def _axis_b(d, fy, stmt, key, face_lines, notes_blob, rows_out):
    aliases = _aliases_for(key)
    hit = _face_alias_hit(aliases, face_lines)
    if hit:
        rows_out.append(_row(stmt, key, fy, "absent_on_face", 0.0, None, "BUG_FACE",
                             hit[0], None, f"zero-filled but concept on FACE ({hit[1]}): {hit[0]!r}"))
        return
    a = _notes_alias_hit(aliases, notes_blob)
    if a:
        rows_out.append(_row(stmt, key, fy, "absent_on_face", 0.0, None, "NOTE_ONLY",
                             "", None, f"alias {a!r} appears only in notes"))
        return
    # genuine zero -- PASS (not emitted to console, only to full CSV)
    rows_out.append(_row(stmt, key, fy, "absent_on_face", 0.0, None, "PASS", "", None, "no face/notes hit"))


# --------------------------------------------------------------------------- #
# Axis C -- derived soundness (identity checks)
# --------------------------------------------------------------------------- #
def _axis_c(d, fy, stmt, derived_keys, rows_out):
    by_key = {k: v for (yy, ss, k), v in d["sl"].items() if yy == fy and ss == stmt}
    flags = _identity_checks(by_key, stmt)
    if not flags:
        for key in derived_keys:
            rows_out.append(_row(stmt, key, fy, "derived", d["sl"][(fy, stmt, key)], None,
                                 "PASS", "", None, "identities clean"))
        return
    for key in derived_keys:
        rows_out.append(_row(stmt, key, fy, "derived", d["sl"][(fy, stmt, key)], None,
                             "MISMATCH", "", None, "identity failed: " + ", ".join(flags)))


# --------------------------------------------------------------------------- #
# row + report
# --------------------------------------------------------------------------- #
def _row(stmt, key, fy, match_kind, sheet_value, expected, verdict, raw_label, src_pdf_year, note):
    return dict(statement=stmt, key=key, year=fy, match_kind=match_kind,
                stored_value=sheet_value, expected_from_page=expected, verdict=verdict,
                raw_label=raw_label, src_pdf_year=src_pdf_year, note=note)


def audit_ticker(ticker: str, years: set[int] | None, out_dir: Path) -> dict:
    d = _load(ticker, years)
    template = set(fill_metric_keys(is_bank=d["is_bank"]))  # {(stmt, key)}
    rows: list[dict] = []

    for fy in d["years"]:
        # precompute normalized numbered lines ONCE per fy (face per statement; notes blob)
        face_lines = {s: _numbered_norm_lines(d["face"].get((fy, s), ""))
                      for s in ("income_statement", "balance_sheet", "cash_flow")}
        # cheap normalized notes blob (one pass; the notes check is a substring
        # test for LOW-severity flags, so it doesn't need per-line normalization)
        notes_blob = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (d["pages"].get(fy, "") or "").lower()))
        derived_by_stmt: dict[str, list[str]] = {}
        for (stmt, key) in sorted(template):
            slot = (fy, stmt, key)
            if slot not in d["sl"]:
                continue  # absent from output entirely (rendered "-"): out of template-fill scope
            mk = d["quality"].get(slot)
            V = d["sl"][slot]
            if mk == "mapped":
                _axis_a(d, fy, stmt, key, V, rows)
            elif mk == "absent_on_face":
                _axis_b(d, fy, stmt, key, face_lines.get(stmt, []), notes_blob, rows)
            elif mk == "derived":
                derived_by_stmt.setdefault(stmt, []).append(key)
            else:
                rows.append(_row(stmt, key, fy, str(mk), V, None, "SUSPECT", "", None,
                                 f"unexpected match_kind={mk!r}"))
        for stmt, dks in derived_by_stmt.items():
            _axis_c(d, fy, stmt, dks, rows)

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{ticker}_audit.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["statement", "key", "year", "match_kind", "stored_value",
                            "expected_from_page", "verdict", "raw_label", "src_pdf_year", "note"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    return dict(ticker=ticker, is_bank=d["is_bank"], years=d["years"],
                counts=counts, rows=rows, csv=str(csv_path))


def _print_report(res: dict) -> None:
    print(f"\n{'='*70}\n{res['ticker']}  (bank={res['is_bank']})  years={res['years']}")
    print("  verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(res["counts"].items())))
    # Console shows the ACTIONABLE verdicts individually; NOTE_ONLY (low-severity
    # "worth a look") is summarized as a count — full detail is in the CSV.
    actionable = [r for r in res["rows"] if r["verdict"] in ("BUG_FACE", "MISMATCH", "SUSPECT")]
    note_only = sum(1 for r in res["rows"] if r["verdict"] == "NOTE_ONLY")
    if not actionable:
        print(f"  no actionable flags — all mapped/derived cells PASS, all zeros clean on the face"
              f"  ({note_only} NOTE_ONLY in CSV)")
    else:
        order = {"BUG_FACE": 0, "MISMATCH": 1, "SUSPECT": 2}
        for r in sorted(actionable, key=lambda x: (order.get(x["verdict"], 9), x["year"], x["statement"], x["key"])):
            sv = r["stored_value"]
            svs = f"{sv:,.3f}" if isinstance(sv, (int, float)) else str(sv)
            print(f"  [{r['verdict']:9}] {r['year']} {r['statement'][:3]} {r['key']:<32} "
                  f"stored={svs:>18}  {r['note']}")
        print(f"  (+{note_only} NOTE_ONLY low-severity in CSV)")
    print(f"  full table -> {res['csv']}")


def main() -> None:
    try:  # Windows consoles default to cp1252; the report/notes are UTF-8.
        import sys as _sys
        _sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Read-only line-item audit vs source PDFs.")
    ap.add_argument("tickers", help="comma-separated ticker(s)")
    ap.add_argument("--years", default=None, help="comma-separated fiscal years (default: all)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "audit"), help="output dir for CSVs")
    ap.add_argument("--pdf", default=None, help="(reserved) real PDF path for true end-to-end")
    args = ap.parse_args()

    years = {int(y) for y in args.years.split(",")} if args.years else None
    out_dir = Path(args.out)
    for tk in [t.strip() for t in args.tickers.split(",") if t.strip()]:
        res = audit_ticker(tk, years, out_dir)
        _print_report(res)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    main()
