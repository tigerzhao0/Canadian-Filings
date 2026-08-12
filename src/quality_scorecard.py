#!/usr/bin/env python3
"""Corpus-wide data-quality scorecard -- a single, GuruFocus-free snapshot of where
the PDF-extraction pipeline stands, so every fix round can be measured against a
baseline. Read-only against output/pdf_financials.db.

    python src/quality_scorecard.py                 # print + append a snapshot
    python src/quality_scorecard.py --no-log         # print only

Tracks the objective, ground-truth-free signals (per the fix methodology):
  - balance-sheet identity pass rate (the correctness anchor)
  - statement-section coverage + SECTION-MISSING company-years (IS but no BS/CF)
  - fill composition (mapped / derived / zero-filled) and zero-fill by key
  - mean statement confidence + needs-review count
Each run appends one row to output/quality_scorecard_history.csv so you can see the
trend as aliases/fixes land.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "output" / "pdf_financials.db"
HISTORY = ROOT / "output" / "quality_scorecard_history.csv"


def _scalar(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return r[0] if r and r[0] is not None else 0


def collect(conn: sqlite3.Connection) -> dict:
    m = {}
    m["tickers"] = _scalar(conn, "SELECT COUNT(*) FROM companies")
    m["company_years"] = _scalar(conn, "SELECT COUNT(*) FROM company_years")

    # --- fill composition from statement_line_quality (match_kind = provenance) ---
    try:
        rows = dict(conn.execute(
            "SELECT match_kind, COUNT(*) FROM statement_line_quality GROUP BY match_kind"))
        m["mapped"] = rows.get("mapped", 0)
        m["from_notes"] = rows.get("from_notes", 0)
        m["derived"] = rows.get("derived", 0)
        m["zero_filled"] = rows.get("absent_on_face", 0)
    except sqlite3.OperationalError:
        m["mapped"] = m["from_notes"] = m["derived"] = m["zero_filled"] = 0

    # --- RAW-ONLY fill rate: a value counts as "filled" only if a real number was
    # READ off the filing (face or notes). Computed (derived) cells are excluded
    # entirely -- not counted as filled, not counted in the denominator -- because
    # they're not raw data, they're OUR formula's guess from other raw cells. ---
    raw_filled = m["mapped"] + m["from_notes"]
    raw_total = raw_filled + m["zero_filled"]
    m["raw_filled"] = raw_filled
    m["raw_fill_rate_pct"] = round(raw_filled / raw_total * 100, 1) if raw_total else 0.0

    # --- statement confidence / needs-review ---
    try:
        st = dict(conn.execute(
            "SELECT status, COUNT(*) FROM pdf_llm_status GROUP BY status"))
        m["needs_review"] = st.get("needs_review", 0)
        m["mean_confidence"] = round(_scalar(
            conn, "SELECT AVG(confidence) FROM pdf_llm_status WHERE confidence IS NOT NULL"), 3)
    except sqlite3.OperationalError:
        m["needs_review"] = 0
        m["mean_confidence"] = 0.0

    # --- balance-sheet identity (AUTHORITATIVE): read the pipeline's own per-statement
    # verdict from pdf_llm_status.reason rather than re-deriving from merged values
    # (a per-(ticker,year) re-derivation double-counts restated/multi-filing instances
    # and disagrees with rule_extract._identity_checks, which runs per statement
    # INSTANCE during mapping). "checkable" = a balance_sheet statement whose reason
    # was evaluated for the identity; the pipeline flags only failures, so
    # checked = instances that had assets+liab+equity, reconstructed from line_items_full.
    try:
        fails = _scalar(conn, "SELECT COUNT(*) FROM pdf_llm_status "
                        "WHERE statement_type='balance_sheet' "
                        "AND reason LIKE '%balance_identity_fail%'")
        # checkable instances: distinct (ticker, fy, source_pdf_year) balance sheets
        # that mapped all three identity components.
        checkable = _scalar(conn, """
            SELECT COUNT(*) FROM (
              SELECT ticker, fiscal_year, source_pdf_year
              FROM line_items_full
              WHERE statement_type='balance_sheet' AND canonical_key IN
                ('TotalAssets','TotalLiabilities','TotalEquityGrossMinorityInterest',
                 'StockholdersEquity') AND value IS NOT NULL
              GROUP BY ticker, fiscal_year, source_pdf_year
              HAVING SUM(canonical_key='TotalAssets')>0
                 AND SUM(canonical_key='TotalLiabilities')>0
                 AND SUM(canonical_key IN ('TotalEquityGrossMinorityInterest','StockholdersEquity'))>0)""")
        m["balance_checked"] = checkable
        m["balance_pass"] = max(0, checkable - fails)
        m["balance_pass_pct"] = round((checkable - fails) / checkable * 100, 1) if checkable else 0.0
    except sqlite3.OperationalError:
        m["balance_checked"] = m["balance_pass"] = 0
        m["balance_pass_pct"] = 0.0

    # --- section coverage + SECTION-MISSING (IS present, BS/CF absent) ---
    def stmt_years(st):
        return set(conn.execute(
            "SELECT DISTINCT ticker, fiscal_year FROM statement_lines "
            "WHERE statement_type=? AND value IS NOT NULL AND value!=0", (st,)))
    is_y = stmt_years("income_statement")
    bs_y = stmt_years("balance_sheet")
    cf_y = stmt_years("cash_flow")
    m["cov_income"] = len(is_y)
    m["cov_balance"] = len(bs_y)
    m["cov_cash_flow"] = len(cf_y)
    m["section_missing_bs"] = len(is_y - bs_y)   # has IS, no BS
    m["section_missing_cf"] = len(is_y - cf_y)   # has IS, no CF
    return m


ORDER = ["timestamp", "tickers", "company_years", "mapped", "from_notes", "derived",
         "zero_filled", "raw_filled", "raw_fill_rate_pct",
         "needs_review", "mean_confidence", "balance_checked", "balance_pass",
         "balance_pass_pct", "cov_income", "cov_balance", "cov_cash_flow",
         "section_missing_bs", "section_missing_cf"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Corpus-wide data-quality scorecard.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--no-log", action="store_true", help="Print only; don't append history.")
    ap.add_argument("--label", default="", help="Optional note stored with the snapshot.")
    args = ap.parse_args()
    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")

    conn = sqlite3.connect(args.db)
    m = collect(conn)
    conn.close()
    m["timestamp"] = time.strftime("%Y-%m-%d %H:%M")
    if args.label:
        m["label"] = args.label

    print("\n" + "=" * 52)
    print("  DATA-QUALITY SCORECARD" + (f"  [{args.label}]" if args.label else ""))
    print("=" * 52)
    print(f"  tickers / company-years : {m['tickers']} / {m['company_years']}")
    print(f"  BALANCE-SHEET IDENTITY  : {m['balance_pass']}/{m['balance_checked']} "
          f"= {m['balance_pass_pct']}% pass   <-- correctness anchor")
    print(f"  fill: mapped {m['mapped']:,}  from_notes {m['from_notes']:,}  "
          f"derived {m['derived']:,}  zero-filled {m['zero_filled']:,}")
    print(f"  RAW-ONLY FILL RATE      : {m['raw_filled']:,}/"
          f"{m['raw_filled'] + m['zero_filled']:,} = {m['raw_fill_rate_pct']}%   "
          f"<-- excludes derived; mapped+from_notes vs zero-filled only")
    print(f"  mean confidence         : {m['mean_confidence']}   needs-review: {m['needs_review']}")
    print("  " + "-" * 48)
    print(f"  section coverage (co-yrs): IS {m['cov_income']}  BS {m['cov_balance']}  "
          f"CF {m['cov_cash_flow']}")
    print(f"  SECTION-MISSING         : {m['section_missing_bs']} have IS but no BS, "
          f"{m['section_missing_cf']} no CF")
    print("=" * 52)

    if not args.no_log:
        new = not HISTORY.exists()
        with HISTORY.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=ORDER + (["label"] if args.label else []))
            if new:
                w.writeheader()
            w.writerow({k: m.get(k, "") for k in ORDER + (["label"] if args.label else [])})
        print(f"  snapshot appended -> {HISTORY}")


if __name__ == "__main__":
    main()
