"""Quick manual-check helper for a single ticker's step-3 output.

    python src/check_ticker.py AAA.P

Prints statement_lines, pdf_llm_status (with reasons/drops), and any
pdf_llm_consistency flags for that ticker, straight from financials.db.
"""
from __future__ import annotations

import sqlite3
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python src/check_ticker.py <TICKER>")
        raise SystemExit(2)
    ticker = sys.argv[1]
    conn = sqlite3.connect("output/financials.db")

    print(f"--- statement_lines ({ticker}) ---")
    for r in conn.execute(
            "SELECT fiscal_year, statement_type, line_item, value FROM statement_lines "
            "WHERE ticker = ? ORDER BY fiscal_year DESC, statement_type, line_item", (ticker,)):
        print(r)

    print(f"\n--- pdf_llm_status ({ticker}) ---")
    for r in conn.execute(
            "SELECT fiscal_year, statement_type, status, n_lines, reason FROM pdf_llm_status "
            "WHERE ticker = ? ORDER BY fiscal_year DESC, statement_type", (ticker,)):
        print(r)

    print(f"\n--- pdf_llm_consistency ({ticker}) ---")
    rows = conn.execute(
        "SELECT concept_group, pattern, keys_used FROM pdf_llm_consistency WHERE ticker = ?",
        (ticker,)).fetchall()
    if not rows:
        print("(no consistency warnings)")
    for r in rows:
        print(r)

    conn.close()


if __name__ == "__main__":
    main()
