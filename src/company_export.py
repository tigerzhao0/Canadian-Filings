#!/usr/bin/env python3
"""Emit one company's raw fundamentals as JSON, straight from financials.db.

    python src/company_export.py RY          -> output/companies/RY.json
    python src/company_export.py --all        -> output/companies/<TICKER>.json for all

Just the fundamentals we actually have: identity + the per-year income statement,
balance sheet, and cash-flow line items (see sql/company_schema.json). No computed
ratios, no GuruFocus ranks/scores -- nothing invented. This is the same three-
statement shape the PDF step (run.py --step 2) targets, so QuoteMedia-sourced and
future PDF/LLM-sourced companies converge on one per-company document.

Standalone (sqlite3 + json only). Read-only against the DB; re-run any time.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

# statement_lines.statement_type -> the camelCase key used in the JSON document.
STATEMENT_KEYS = {
    "income_statement": "incomeStatement",
    "balance_sheet": "balanceSheet",
    "cash_flow": "cashFlow",
}


def build_document(conn: sqlite3.Connection, ticker: str) -> dict | None:
    ident = conn.execute(
        "SELECT ticker, company_name, exchange, currency, primary_source "
        "FROM companies WHERE ticker = ?", (ticker,)).fetchone()
    if ident is None:
        return None

    years = conn.execute(
        "SELECT fiscal_year, period_end, currency FROM company_years "
        "WHERE ticker = ? ORDER BY fiscal_year DESC", (ticker,)).fetchall()

    lines = conn.execute(
        "SELECT fiscal_year, statement_type, line_item, value "
        "FROM statement_lines WHERE ticker = ?", (ticker,)).fetchall()
    # fiscal_year -> statement_key -> {line_item: value}
    by_year: dict[int, dict[str, dict]] = {}
    for fy, stmt_type, line_item, value in lines:
        key = STATEMENT_KEYS.get(stmt_type)
        if key is None:
            continue
        by_year.setdefault(fy, {}).setdefault(key, {})[line_item] = value

    financials = []
    for fy, period_end, currency in years:
        stmts = by_year.get(fy, {})
        financials.append({
            "fiscalYear": fy,
            "periodEnd": period_end,
            "currency": currency,
            "incomeStatement": stmts.get("incomeStatement", {}),
            "balanceSheet": stmts.get("balanceSheet", {}),
            "cashFlow": stmts.get("cashFlow", {}),
        })

    return {
        "ticker": ident[0],
        "companyName": ident[1],
        "exchange": ident[2],
        "currency": ident[3],
        "primarySource": ident[4],
        "financials": financials,
    }


def _validate(doc: dict, schema_path: Path) -> str | None:
    """Best-effort schema validation; returns an error string or None. Silently
    skips if jsonschema isn't installed (it's optional)."""
    try:
        import jsonschema
    except Exception:  # noqa: BLE001
        return None
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.validate(doc, schema)
        return None
    except Exception as exc:  # noqa: BLE001 - jsonschema.ValidationError et al.
        return str(exc).splitlines()[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker", nargs="?", help="Export one company (e.g. RY).")
    ap.add_argument("--all", action="store_true", help="Export every company in the DB.")
    ap.add_argument("--db", default="output/financials.db")
    ap.add_argument("--out-dir", default="output/companies")
    args = ap.parse_args()

    if not args.ticker and not args.all:
        ap.error("Provide a ticker (e.g. RY) or --all.")

    schema_path = Path(__file__).resolve().parent.parent / "sql" / "company_schema.json"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    try:
        if args.all:
            tickers = [r[0] for r in conn.execute("SELECT ticker FROM companies ORDER BY ticker")]
        else:
            tickers = [args.ticker.upper()]

        written = 0
        invalid = 0
        for tk in tickers:
            doc = build_document(conn, tk)
            if doc is None:
                if not args.all:
                    print(f"'{tk}' not in companies table (never resolved). "
                         "Run the financials stage first.")
                continue
            err = _validate(doc, schema_path)
            if err:
                invalid += 1
                if not args.all:
                    print(f"  schema warning for {tk}: {err}")
            (out_dir / f"{tk}.json").write_text(
                json.dumps(doc, indent=2), encoding="utf-8")
            written += 1
        note = f" ({invalid} schema warnings)" if invalid else ""
        if args.all:
            print(f"Wrote {written} company JSON file(s) to {out_dir}/{note}")
        elif written:
            print(f"Wrote {out_dir}/{tickers[0]}.json{note}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
