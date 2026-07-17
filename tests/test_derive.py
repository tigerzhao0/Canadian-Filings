"""Tests for the derivation/zero-fill engine (derive.py), the schema_map
prose guard + aggregation, and the annualreports.com URL builders.

Runs under pytest, or standalone:  python tests/test_derive.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import annualreports_source as ars  # noqa: E402
import derive as d  # noqa: E402
from line_items import parse_lines  # noqa: E402
from schema_map import AGGREGATE_KEYS, _looks_like_prose  # noqa: E402

IS, BS, CF = d.IS, d.BS, d.CF
ALL3 = {IS, BS, CF}


def test_template_keys_match_row_spec():
    from company_xlsx_export import ROW_SPEC
    tmpl = d.template_keys(include_bank=True)
    spec = {(r[2], r[3]) for r in ROW_SPEC
            if isinstance(r, tuple) and r[0] in ("data", "data_bank") and r[2] and r[3]}
    got = {(s, k) for s, ks in tmpl.items() for k in ks}
    assert got == spec
    # zero-fill never targets a key the template doesn't render
    tmpl_nb = d.template_keys(include_bank=False)
    for stmt, keys in d.ZEROABLE_KEYS.items():
        assert keys <= set(tmpl_nb[stmt]), (stmt, keys - set(tmpl_nb[stmt]))


def test_shell_company_income_chain():
    # a CPC that prints only expenses and a net loss
    vals = {(IS, "NetIncome"): -100.0,
            (IS, "SellingGeneralAndAdministration"): 80.0,
            (IS, "OtherOperatingExpenses"): 20.0}
    new = d.derive_year(vals, has_stmt={IS})
    assert new[(IS, "PretaxIncome")] == -100.0          # no tax line -> NI
    assert new[(IS, "OperatingExpense")] == 100.0       # SG&A + other
    assert new[(IS, "NetIncomeContinuousOperations")] == -100.0


def test_service_co_gross_profit_equals_revenue():
    vals = {(IS, "TotalRevenue"): 500.0, (IS, "NetIncome"): 50.0}
    new = d.derive_year(vals, has_stmt={IS})
    assert new[(IS, "GrossProfit")] == 500.0
    assert new[(IS, "CostOfRevenue")] == 0.0


def test_never_overwrites_mapped():
    vals = {(IS, "TotalRevenue"): 500.0, (IS, "CostOfRevenue"): 200.0,
            (IS, "GrossProfit"): 123.0}   # mapped, inconsistent on purpose
    new = d.derive_year(vals, has_stmt={IS})
    assert (IS, "GrossProfit") not in new  # existing slot untouched


def test_balance_sheet_two_of_three_and_plug():
    vals = {(BS, "TotalAssets"): 100.0, (BS, "CurrentAssets"): 60.0,
            (BS, "TotalLiabilities"): 30.0,
            (BS, "CashAndCashEquivalents"): 25.0,
            (BS, "AccountsReceivable"): 10.0}
    new = d.derive_year(vals, has_stmt={BS})
    assert new[(BS, "TotalNonCurrentAssets")] == 40.0
    assert new[(BS, "TotalEquityGrossMinorityInterest")] == 70.0   # A - L
    assert new[(BS, "StockholdersEquity")] == 70.0                 # MI default 0
    assert new[(BS, "Receivables")] == 10.0
    # OtherCurrentAssets = 60 - (25 cash agg + 10 receivables) = 25
    assert new[(BS, "OtherCurrentAssets")] == 25.0


def test_plug_refuses_large_negative():
    assert d._plug(100.0, [80.0, 30.0]) is None       # details exceed total
    assert d._plug(100.0, [60.0, 40.0000001]) == 0.0  # rounding clamps


def test_cash_rollforward_and_fcf():
    vals = {(CF, "BeginningCashPosition"): 10.0, (CF, "ChangesInCash"): 5.0,
            (CF, "OperatingCashFlow"): 8.0, (CF, "PurchaseOfPPE"): 3.0}
    new = d.derive_year(vals, has_stmt={CF})
    assert new[(CF, "EndCashPosition")] == 15.0
    assert new[(CF, "CapitalExpenditure")] == -3.0
    assert new[(CF, "FreeCashFlow")] == 5.0


def test_zero_fill_requires_anchor_and_skips_aggregates():
    vals = {(BS, "TotalAssets"): 100.0}
    zf = d.zero_fill(vals, has_stmt={BS}, is_bank=False)
    assert zf[(BS, "Inventory")] == 0.0
    assert zf[(BS, "Goodwill")] == 0.0
    assert (BS, "CurrentAssets") not in zf         # roll-up: derive-only
    assert (BS, "TotalLiabilities") not in zf      # anchor: never assumed
    # income statement had no anchor -> untouched even if in has_stmt
    zf2 = d.zero_fill(vals, has_stmt={BS, IS}, is_bank=False)
    assert not any(s == IS for (s, _k) in zf2)


def test_zero_fill_never_touches_bank_keys():
    vals = {(BS, "TotalAssets"): 100.0}
    zf = d.zero_fill(vals, has_stmt={BS}, is_bank=True)
    assert (BS, "GrossLoan") not in zf and (BS, "TotalDeposits") not in zf


def test_prose_guard():
    prose = {i: [dict(label=("the fund unanimously voted to raise the overnight "
                             "lending rate by several points"), value_printed=25.0)]
             for i in range(6)}
    assert _looks_like_prose(prose)
    stmt = {i: [dict(label=lbl, value_printed=v)] for i, (lbl, v) in enumerate([
        ("Cash and cash equivalents", 119389.0), ("Dividends receivable", 3738.0),
        ("Investments", 1231374.0), ("Accounts payable", 1240.0),
        ("Credit facility", 222586.0), ("NET ASSETS", 1129704.0)])}
    assert not _looks_like_prose(stmt)


def test_aggregate_keys_never_contain_totals():
    for k in AGGREGATE_KEYS:
        assert not k.startswith("Total") and "EPS" not in k, k


def test_dollar_label_recovers_wrapped_line():
    text = ("Balance Sheet\n(in thousands) 2025 2024\n"
            "Cash 100 90\n"
            "Accounts payable and accrued\n"
            "$ 55 $ 44\n")
    p = parse_lines(text, "balance_sheet")
    labels = {r.label for r in p.rows}
    assert "Accounts payable and accrued" in labels
    assert "$" not in labels


def test_annualreports_url_builders():
    assert ars.current_url("TSX", "RY", 2021) == \
        "https://www.annualreports.com/HostedData/AnnualReports/PDF/TSX_RY_2021.pdf"
    assert ars.exchange_prefixes("TSXV")[0] == "TSX-V"
    assert ars.exchange_prefixes("TSX")[0] == "TSX"
    assert ars.exchange_prefixes("CNSX")[0] == "OTC"
    assert ars.ticker_variants("RAY.A") == ["RAY.A", "RAY", "RAYA"]
    letters = ars.archive_letters("Royal Bank of Canada", "RY")
    assert "r" in letters and "R" in letters
    urls = ars.year_urls("TSX", "RY", 2006, "Royal Bank of Canada")
    assert urls[0].endswith("/AnnualReports/PDF/TSX_RY_2006.pdf")
    assert any("/AnnualReportArchive/R/TSX_RY_2006.pdf" in u for u in urls)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
