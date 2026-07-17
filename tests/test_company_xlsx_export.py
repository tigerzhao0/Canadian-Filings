"""Regression test for the Sheet1 display-sign fix: GuruFocus's own export
shows Tax Provision and (industrial) Interest Expense as a SIGNED
CONTRIBUTION to the next subtotal (negative = reduces income), while every
OTHER expense-type row (COGS, SG&A, R&D, Other Operating Expense, D&A) stays
a positive magnitude in that same export. Confirmed against a real
user-provided GuruFocus export for Stingray Group (RAY): Tax Provision
-1.7/-16/..., Interest Expense -17.5/-17.8/..., but SG&A 19.5 / Other
Operating Expense 230.7 / D&A 40.3 (all positive).

Internal storage MUST stay positive -- derive.py's EBIT and NetIncome
identities are written assuming positive magnitudes -- so the flip is
render-only, applied in company_xlsx_export.build_workbook's Sheet1 loop.

Runs under pytest, or standalone:  python tests/test_company_xlsx_export.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from company_xlsx_export import build_workbook  # noqa: E402


def _seed(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE companies (ticker TEXT, company_name TEXT, exchange TEXT, currency TEXT);
        CREATE TABLE company_years (ticker TEXT, fiscal_year INTEGER, period_end TEXT);
        CREATE TABLE statement_lines (ticker TEXT, fiscal_year INTEGER,
            statement_type TEXT, line_item TEXT, value REAL);
    """)
    conn.execute("INSERT INTO companies VALUES ('ACME', 'Acme Corp', 'TSX', 'CAD')")
    conn.execute("INSERT INTO company_years VALUES ('ACME', 2020, '2020-03-31')")
    rows = [
        ("income_statement", "TotalRevenue", 306_721_000.0),
        ("income_statement", "SellingGeneralAndAdministration", 19_500_000.0),
        ("income_statement", "OtherOperatingExpenses", 230_700_000.0),
        ("income_statement", "TaxProvision", 1_692_000.0),          # stored positive
        ("income_statement", "InterestExpenseNonOperating", 17_500_000.0),  # stored positive
        ("income_statement", "PretaxIncome", 15_662_000.0),
        ("income_statement", "NetIncome", 13_970_000.0),
    ]
    for stmt, item, val in rows:
        conn.execute("INSERT INTO statement_lines VALUES (?,?,?,?,?)",
                     ("ACME", 2020, stmt, item, val))
    conn.commit()


def _cell(wb, label: str):
    ws = wb["Sheet1"]
    for row in ws.iter_rows():
        if row[0].value and str(row[0].value).strip() == label:
            return row[1].value
    raise AssertionError(f"row {label!r} not found")


def test_tax_and_interest_expense_render_negative():
    conn = sqlite3.connect(":memory:")
    _seed(conn)
    wb = build_workbook(conn, "ACME")
    assert _cell(wb, "Tax Provision") == -1.692
    assert _cell(wb, "Interest Expense") == -17.5


def test_other_expense_rows_stay_positive():
    conn = sqlite3.connect(":memory:")
    _seed(conn)
    wb = build_workbook(conn, "ACME")
    assert _cell(wb, "Selling, General, & Admin. Expense") == 19.5
    assert _cell(wb, "Other Operating Expense") == 230.7


def test_all_line_items_sheet_unaffected_by_display_flip():
    # the raw "All Line Items" tab must keep the TRUE stored (positive) value
    # for traceability -- only Sheet1 is display-adjusted.
    conn = sqlite3.connect(":memory:")
    _seed(conn)
    conn.executescript("""
        CREATE TABLE line_items_full (ticker TEXT, statement_type TEXT, section TEXT,
            raw_label TEXT, canonical_key TEXT, fiscal_year INTEGER, value REAL,
            source_pdf_year INTEGER, line_no INTEGER);
    """)
    conn.execute("INSERT INTO line_items_full VALUES "
                 "('ACME','income_statement',NULL,'Income taxes','TaxProvision',2020,1692000.0,2020,1)")
    conn.commit()
    wb = build_workbook(conn, "ACME")
    ws = wb["All Line Items"]
    # columns: A Statement, B Section, C Line item, D Canonical key, E.. years
    vals = [row[4].value for row in ws.iter_rows(min_row=2) if row[3].value == "TaxProvision"]
    assert vals and all(v > 0 for v in vals), vals


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
