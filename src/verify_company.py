#!/usr/bin/env python3
"""Verify one company's PDF-extracted financials (pdf_financials.db) against
QuoteMedia ground truth (financials.db) -- CAD vs CAD, per (statement, key, year).

    python src/verify_company.py RY
    python src/verify_company.py RY --tolerance 0.01

Reports: years covered, headline-key coverage per year (the GuruFocus display
rows), exact-match rate vs QuoteMedia, parse/map coverage, and the known
DEFINITIONAL differences (keys where QuoteMedia's definition differs from the
statement face -- our value matches the GuruFocus DISPLAY instead).
Read-only. The GuruFocus screenshots are the USD view of the same data; all
comparison here is in reported (CAD) figures, which is what both DBs store.
"""
from __future__ import annotations

import argparse
import sqlite3

# Keys where QuoteMedia's stored key uses a different DEFINITION than the
# statement face -- verified on RY: our face value matches GuruFocus's own
# display (USD-converted), so a mismatch here is expected, not an error.
DEFINITIONAL_DIFFS = {
    "InterestIncome": "QM nets deposit interest out; face-of-statement value matches the GF display",
    "InterestExpense": "QM nets deposit interest out; face value matches the GF display",
    "ChangesInCash": "QM excludes the FX effect; face value matches the GF display",
    "BasicEPS": "QM recomputes (~2% off reported); face value matches the GF display",
    "DilutedEPS": "QM recomputes (~2% off reported); face value matches the GF display",
    "BasicContinuousOperations": "fanned from BasicEPS; same QM recompute gap",
    "DilutedContinuousOperations": "fanned from DilutedEPS; same QM recompute gap",
    "DividendPerShare": "we store dividends DECLARED per the statement face; QM uses paid",
}

# Contra/flow keys where QuoteMedia's SIGN convention flips between years
# (e.g. RY AllowanceForLoansAndLeaseLosses is negative 2018-2021, positive
# 2022+). Our convention is constant, so |ours| == |theirs| counts as a match.
SIGN_ONLY_KEYS = {"AllowanceForLoansAndLeaseLosses", "CreditLossesProvision",
                  "CashDividendsPaid", "MinorityInterests", "PreferredStockDividends"}

HEADLINE_KEYS = [
    ("income_statement", ["InterestIncome", "InterestExpense", "NetInterestIncome",
                          "NonInterestIncome", "TotalRevenue", "CreditLossesProvision",
                          "NonInterestExpense", "PretaxIncome", "TaxProvision",
                          "NetIncome", "BasicEPS", "DilutedEPS"]),
    ("balance_sheet", ["CashAndDueFromBanks", "GrossLoan", "NetLoan",
                       "SecuritiesAndInvestments", "TotalDeposits", "TotalAssets",
                       "TotalLiabilities", "StockholdersEquity",
                       "TotalEquityGrossMinorityInterest", "RetainedEarnings"]),
    ("cash_flow", ["OperatingCashFlow", "InvestingCashFlow", "FinancingCashFlow",
                   "ChangesInCash", "BeginningCashPosition", "EndCashPosition"]),
]


def verify(ticker: str, pdf_db: str, qm_db: str, tolerance: float) -> None:
    pf = sqlite3.connect(pdf_db)
    qm = sqlite3.connect(qm_db)

    ours = {(s, k, y): v for s, k, y, v in pf.execute(
        "SELECT statement_type, line_item, fiscal_year, value FROM statement_lines "
        "WHERE ticker = ? AND value IS NOT NULL", (ticker,))}
    theirs = {(s, k, y): v for s, k, y, v in qm.execute(
        "SELECT statement_type, line_item, fiscal_year, value FROM statement_lines "
        "WHERE ticker = ? AND value IS NOT NULL", (ticker,))}

    years = sorted({y for (_, _, y) in ours})
    print(f"=== {ticker}: PDF-extracted vs QuoteMedia (CAD) ===")
    print(f"years covered (PDF): {years[0]}-{years[-1]} ({len(years)} years)"
          if years else "NO DATA -- run steps 1-4 first")
    if not years:
        return

    # headline coverage per year
    print("\nHEADLINE COVERAGE (the GuruFocus display rows)")
    for stmt, keys in HEADLINE_KEYS:
        # skip bank rows entirely for non-banks
        present_any = [k for k in keys if any((stmt, k, y) in ours for y in years)]
        cov = []
        for y in years:
            n = sum(1 for k in present_any if (stmt, k, y) in ours)
            cov.append(f"{y}:{n}/{len(present_any)}")
        print(f"  {stmt:16} {' '.join(cov)}")

    # exact-match vs QM on overlap -- MAPPED values only: derived / zero-filled
    # cells state "absent on the statement face", while QM derives from notes;
    # comparing those judges different claims, not extraction accuracy.
    try:
        kinds_cmp = {(s, k, y): mk for s, k, y, mk in pf.execute(
            "SELECT statement_type, line_item, fiscal_year, match_kind "
            "FROM statement_line_quality WHERE ticker = ?", (ticker,))}
    except sqlite3.OperationalError:
        kinds_cmp = {}
    mapped_ours = {slot: v for slot, v in ours.items()
                   if kinds_cmp.get(slot, "mapped") == "mapped"}
    common = sorted(set(mapped_ours) & set(theirs))
    match = mismatch = defn = sign = 0
    bad: list[tuple] = []
    for slot in common:
        s, k, y = slot
        o, t = mapped_ours[slot], theirs[slot]
        if abs(o - t) <= tolerance * max(abs(t), 1.0):
            match += 1
        elif k in SIGN_ONLY_KEYS and abs(abs(o) - abs(t)) <= tolerance * max(abs(t), 1.0):
            sign += 1
        elif k in DEFINITIONAL_DIFFS:
            defn += 1
        else:
            mismatch += 1
            if len(bad) < 15:
                bad.append((s, k, y, o, t))
    print(f"\nVS QUOTEMEDIA ({len(common)} overlapping values, tolerance {tolerance:.1%})")
    print(f"  exact matches        : {match}")
    print(f"  sign-convention only : {sign}  (|value| matches; QM's sign flips between years)")
    print(f"  definitional diffs   : {defn}  (expected -- face value matches the GF display)")
    print(f"  REAL mismatches      : {mismatch}")
    for s, k, y, o, t in bad:
        print(f"    {y} {s}/{k}: ours={o:,.2f} qm={t:,.2f}")

    # template fill rate -- the number the xlsx user actually sees
    try:
        import sys as _sys
        from pathlib import Path as _P
        _sys.path.insert(0, str(_P(__file__).resolve().parent))
        import derive as _derive
        kinds = {(s, k, y): mk for s, k, y, mk in pf.execute(
            "SELECT statement_type, line_item, fiscal_year, match_kind "
            "FROM statement_line_quality WHERE ticker = ?", (ticker,))}
        is_bank = any(k == "GrossLoan" for (_s, k, _y) in ours)
        # fill_metric_keys (not template_keys) -- excludes the industrial BS
        # leaf rows a bank's face never disaggregates, matching schema_map's
        # own summary. Using template_keys directly would double-count a
        # bank's mandatory-blank rows as "missing".
        keys = _derive.fill_metric_keys(is_bank=is_bank)
        cells = len(keys) * len(years)
        filled = mapped_n = derived_n = zero_n = 0
        for y in years:
            for s, k in keys:
                if (s, k, y) in ours:
                    filled += 1
                    mk = kinds.get((s, k, y), "mapped")
                    if mk == "derived":
                        derived_n += 1
                    elif mk == "absent_on_face":
                        zero_n += 1
                    else:
                        mapped_n += 1
        print(f"\nTEMPLATE FILL: {filled}/{cells} cells = {filled / cells * 100:.0f}%"
              f"  (mapped {mapped_n}, derived {derived_n}, zero-filled {zero_n})")
    except Exception as e:  # noqa: BLE001 - fill report must never kill verify
        print(f"\nTEMPLATE FILL: unavailable ({e})")

    # parse/map coverage from line_items_full
    try:
        total, mapped = pf.execute(
            "SELECT COUNT(DISTINCT source_pdf_year || '/' || statement_type || '/' || line_no), "
            "COUNT(DISTINCT CASE WHEN canonical_key IS NOT NULL THEN "
            "source_pdf_year || '/' || statement_type || '/' || line_no END) "
            "FROM line_items_full WHERE ticker = ?", (ticker,)).fetchone()
        print(f"\nLINE COVERAGE: {total} parsed lines, {mapped} mapped "
              f"({mapped / total * 100:.0f}%) -- ALL {total} kept in line_items_full "
              "and on the 'All Line Items' xlsx sheet")
    except sqlite3.OperationalError:
        pass
    pf.close()
    qm.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ticker")
    ap.add_argument("--pdf-db", default="output/pdf_financials.db")
    ap.add_argument("--qm-db", default="output/financials.db")
    ap.add_argument("--tolerance", type=float, default=0.01)
    args = ap.parse_args()
    verify(args.ticker.upper(), args.pdf_db, args.qm_db, args.tolerance)


if __name__ == "__main__":
    main()
