#!/usr/bin/env python3
"""Export financials.db into browsable Excel workbooks.

Two modes:
    python export_financials.py RY
        One company's full statements as three sheets (Income Statement,
        Balance Sheet, Cash Flow), each a grid of line_item (rows) x fiscal
        year (columns) -- the exact nested-table shape financials.db models
        per ticker. Writes financials_RY.xlsx.

    python export_financials.py --summary
        Cross-company browsing: one row per (ticker, fiscal_year), with a
        curated set of near-universal metrics as columns (Total Revenue, Net
        Income, Total Assets, ...). Good for skimming/filtering/sorting the
        whole database in Excel. Writes financials_summary.xlsx.

Both modes are plain read-only queries against financials.db (companies /
company_years / statement_lines) -- re-run any time after a fetch to refresh
the export; nothing here writes back to the DB.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

import pandas as pd

# Near-universal line items (reported by >2450 of 2528 resolved companies as
# of the last full run) -- good defaults for a cross-company summary. A
# pre-revenue junior will just show blank cells for ones it doesn't report.
KEY_METRICS = [
    "TotalRevenue", "NetIncome", "EBITDA", "BasicEPS",
    "TotalAssets", "TotalLiabilities", "StockholdersEquity",
    "CurrentAssets", "CurrentLiabilities", "CashAndCashEquivalents",
    "OperatingCashFlow", "FreeCashFlow",
]


def export_company(conn: sqlite3.Connection, ticker: str, out_path: str) -> None:
    info = pd.read_sql("SELECT * FROM companies WHERE ticker = ?", conn, params=(ticker,))
    if info.empty:
        status = pd.read_sql(
            "SELECT status, reason FROM tmx_financials_status WHERE ticker = ?",
            conn, params=(ticker,))
        if status.empty:
            print(f"'{ticker}' was never attempted (not in tmx_financials_status).")
        else:
            row = status.iloc[0]
            print(f"'{ticker}' has no financials -- status={row['status']!r}, "
                 f"reason={row['reason']!r}")
        return

    lines = pd.read_sql(
        "SELECT statement_type, line_item, fiscal_year, value FROM statement_lines "
        "WHERE ticker = ?", conn, params=(ticker,))

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        info.to_excel(writer, sheet_name="Company", index=False)
        for stmt in ("income_statement", "balance_sheet", "cash_flow"):
            sub = lines[lines["statement_type"] == stmt]
            if sub.empty:
                continue
            grid = sub.pivot_table(index="line_item", columns="fiscal_year",
                                   values="value", aggfunc="first")
            grid = grid.reindex(sorted(grid.columns, reverse=True), axis=1)  # newest year first
            grid.to_excel(writer, sheet_name=stmt[:31])  # Excel sheet-name limit
    print(f"Wrote {out_path} ({len(lines)} line items across "
         f"{lines['fiscal_year'].nunique()} year(s))")


def export_summary(conn: sqlite3.Connection, out_path: str) -> None:
    placeholders = ", ".join("?" for _ in KEY_METRICS)
    query = f"""
        SELECT c.ticker, c.company_name, c.exchange, cy.fiscal_year, cy.currency,
               cy.source, sl.line_item, sl.value
        FROM statement_lines sl
        JOIN companies c ON c.ticker = sl.ticker
        JOIN company_years cy ON cy.ticker = sl.ticker AND cy.fiscal_year = sl.fiscal_year
        WHERE sl.line_item IN ({placeholders})
    """
    df = pd.read_sql(query, conn, params=KEY_METRICS)
    if df.empty:
        print("No data found -- has financials.db been populated yet? "
             "(run.py --financials or the default run.py flow)")
        return
    wide = df.pivot_table(
        index=["ticker", "company_name", "exchange", "fiscal_year", "currency", "source"],
        columns="line_item", values="value", aggfunc="first").reset_index()
    ordered_cols = (["ticker", "company_name", "exchange", "fiscal_year", "currency", "source"]
                    + [m for m in KEY_METRICS if m in wide.columns])
    wide = wide[ordered_cols].sort_values(["ticker", "fiscal_year"], ascending=[True, False])
    wide.to_excel(out_path, index=False, sheet_name="Financials Summary")
    print(f"Wrote {out_path} ({len(wide)} rows: "
         f"{wide['ticker'].nunique()} companies x up to "
         f"{wide['fiscal_year'].nunique()} years)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker", nargs="?",
                    help="Export one company's full statements (nested grid).")
    ap.add_argument("--summary", action="store_true",
                    help="Export a cross-company summary (key metrics x year).")
    ap.add_argument("--db", default="output/financials.db", help="Path to financials.db.")
    ap.add_argument("--out", default=None, help="Output .xlsx path (default auto-named).")
    args = ap.parse_args()

    if not args.ticker and not args.summary:
        ap.error("Provide a ticker (e.g. RY) or --summary for the cross-company export.")

    conn = sqlite3.connect(args.db)
    try:
        if args.summary:
            export_summary(conn, args.out or "output/financials_summary.xlsx")
        else:
            ticker = args.ticker.upper()
            export_company(conn, ticker, args.out or f"output/financials_{ticker}.xlsx")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
