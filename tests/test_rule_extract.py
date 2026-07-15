"""Tests for the deterministic (no-LLM) rule extractor: parser, matcher, and
accounting-identity checks.

Runs under pytest, or standalone:  python tests/test_rule_extract.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import rule_extract as r  # noqa: E402

_VOCAB = r._load_vocab(r.DEFAULT_VOCAB)
_IDX = r.build_alias_index(_VOCAB)


def _extract(text, stmt):
    return r.extract_one_statement(text, stmt, set(_VOCAB[stmt]), _IDX)


# ---- parser ---------------------------------------------------------------

def test_two_column_with_note_dollar_and_parens():
    text = (
        "Consolidated Statements of Income\n"
        "(In thousands of Canadian dollars, except per share amounts) Note 2025 2024\n"
        "Revenues 5 $ 386,891 $ 345,428\n"
        "Cost of sales 6 255,282 226,849\n"
        "Gross profit 131,609 118,579\n"
        "Net income (loss) $ 36,440 $ (13,741)\n"
    )
    p = r.parse_statement(text, "income_statement")
    assert p["col_years"] == [2025, 2024]
    assert p["scale"] == 1000.0 and "scale_assumed" not in p["flags"]
    labels = {lbl for lbl, _ in p["rows"]}
    assert not any("2025" in l and "2024" in l for l in labels)  # header row skipped
    rev = next(v for lbl, v in p["rows"] if lbl.lower().startswith("revenues"))
    assert rev == [386891.0, 345428.0]
    ni = next(v for lbl, v in p["rows"] if lbl.lower().startswith("net income"))
    assert ni == [36440.0, -13741.0]  # parentheses negative


def test_null_cells_dash():
    text = ("Balance Sheet\nFebruary 28, 2023 2022\n"
            "Cash 377,433 448,823\n"
            "Prepaid assets - 5,460\n")
    p = r.parse_statement(text, "balance_sheet")
    assert p["col_years"] == [2023, 2022]
    prepaid = next(v for lbl, v in p["rows"] if lbl.lower().startswith("prepaid"))
    assert prepaid == [None, 5460.0]


def test_millions_scale_detected():
    text = "Income\n(Millions of Canadian dollars) 2024 2023\nRevenue 12 10\n"
    p = r.parse_statement(text, "income_statement")
    assert p["scale"] == 1_000_000.0


def test_scale_assumed_flag_when_no_note():
    # parse_statement reports scale_found=False; the scale_assumed FLAG is now
    # emitted by _resolve_scale (which first tries the whole-doc hint).
    text = "Income\n2024 2023\nRevenue 12 10\n"
    p = r.parse_statement(text, "income_statement")
    assert p["scale"] == 1.0 and p["scale_found"] is False
    res = r.extract_one_statement(text, "income_statement",
                                  set(_VOCAB["income_statement"]), _IDX)
    assert "scale_assumed" in res["flags"]
    # ...but if step 2 found a units note elsewhere in the doc, the hint resolves it
    res2 = r.extract_one_statement(text, "income_statement",
                                   set(_VOCAB["income_statement"]), _IDX,
                                   unit_scale_hint=1000.0)
    assert res2["scale"] == 1000.0 and "scale_from_doc_hint" in res2["flags"]


def test_two_column_page_contamination_lands_in_label_not_value():
    # auditor prose glued to the front of a data line -> stays in the label,
    # which then fails to match; the trailing values are still parsed correctly.
    text = ("Income\n(In thousands) 2025 2024\n"
            "precludes public disclosure of the matter Impairment of goodwill 16 56,119 0\n")
    p = r.parse_statement(text, "income_statement")
    lbl, vals = p["rows"][0]
    assert vals == [56119.0, 0.0]
    assert "precludes" in lbl.lower()  # prose captured in label, not mis-read as a value


def test_label_wrap_association():
    text = ("Income\n(In thousands) 2025 2024\n"
            "Net income (loss) and comprehensive income (loss)\n"
            "for the year $ 639,140 $ (1,108,807)\n")
    res = _extract(text, "income_statement")
    ni = [ln for ln in res["line_rows"] if ln["line_item"] == "NetIncome" and ln["fiscal_year"] == 2025]
    assert ni and ni[0]["value"] == 639140.0 * 1000


# ---- matcher --------------------------------------------------------------

def test_exact_alias_and_humanized():
    assert r.match_label("Total assets", "balance_sheet", _IDX) == ("TotalAssets", 1.0)
    assert r.match_label("Total current liabilities", "balance_sheet", _IDX)[0] == "CurrentLiabilities"
    # humanized canonical key match (no curated alias needed)
    assert r.match_label("Retained earnings", "balance_sheet", _IDX)[0] == "RetainedEarnings"


def test_note_ref_and_dollar_stripped_before_match():
    assert r.match_label("Revenues 5 $", "income_statement", _IDX)[0] == "TotalRevenue"
    assert r.match_label("Cost of sales 6", "income_statement", _IDX)[0] == "CostOfRevenue"


def test_unknown_label_rejected():
    key, conf = r.match_label("Blorptastic widget flummox", "income_statement", _IDX)
    assert key is None


def test_statement_scoping():
    # a cash-flow concept must not map when asked as balance_sheet
    key, _ = r.match_label("Net cash from operating activities", "balance_sheet", _IDX)
    assert key != "OperatingCashFlow"


# ---- accounting identities ------------------------------------------------

def test_balance_identity_pass_and_fail():
    ok = {"TotalAssets": 100.0, "TotalLiabilities": 60.0, "StockholdersEquity": 40.0}
    assert "balance_identity_fail" not in r._identity_checks(ok, "balance_sheet")
    bad = {"TotalAssets": 100.0, "TotalLiabilities": 60.0, "StockholdersEquity": 50.0}
    assert "balance_identity_fail" in r._identity_checks(bad, "balance_sheet")


def test_gross_profit_identity():
    bad = {"GrossProfit": 40.0, "TotalRevenue": 100.0, "CostOfRevenue": 55.0}
    assert "gross_profit_fail" in r._identity_checks(bad, "income_statement")
    good = {"GrossProfit": 45.0, "TotalRevenue": 100.0, "CostOfRevenue": 55.0}
    assert "gross_profit_fail" not in r._identity_checks(good, "income_statement")


def test_cashflow_reconciliation():
    good = {"ChangesInCash": 10.0, "OperatingCashFlow": 30.0,
            "InvestingCashFlow": -25.0, "FinancingCashFlow": 5.0}
    assert "cashflow_recon_fail" not in r._identity_checks(good, "cash_flow")
    bad = {**good, "ChangesInCash": 99.0}
    assert "cashflow_recon_fail" in r._identity_checks(bad, "cash_flow")


def test_cash_rollforward():
    good = {"BeginningCashPosition": 100.0, "ChangesInCash": 10.0, "EndCashPosition": 110.0}
    assert "cash_rollforward_fail" not in r._identity_checks(good, "cash_flow")
    bad = {"BeginningCashPosition": 100.0, "ChangesInCash": 10.0, "EndCashPosition": 999.0}
    assert "cash_rollforward_fail" in r._identity_checks(bad, "cash_flow")


def test_full_extract_balance_identity_and_scale():
    text = ("Consolidated Balance Sheets\n"
            "(In thousands of Canadian dollars) 2025 2024\n"
            "Total assets 816,658 700,000\n"
            "Total liabilities 549,824 500,000\n"
            "Shareholders equity 266,834 200,000\n")
    res = _extract(text, "balance_sheet")
    by = {ln["line_item"]: ln["value"] for ln in res["line_rows"] if ln["fiscal_year"] == 2025}
    assert by["TotalAssets"] == 816_658_000.0  # x1000 applied
    assert "balance_identity_fail" not in res["flags"]  # 816658 == 549824 + 266834


def test_percent_column_skipped():
    # a trailing "% change" column must not be parsed as a year value.
    text = "Income\n(In thousands) 2025 2024\nRevenue 386,891 345,428 12%\n"
    p = r.parse_statement(text, "income_statement")
    rev = next(v for lbl, v in p["rows"] if lbl.lower().startswith("revenue"))
    assert rev == [386891.0, 345428.0]   # the 12% is dropped


def test_bank_key_gated_on_nonbank():
    # 'non-interest income' would fuzzy-match NonInterestIncome, but on a non-bank
    # statement the bank-only key is gated out.
    text = ("Income\n(In thousands) 2025 2024\n"
            "Revenue 100 90\nNon-interest income 5 4\nNet income (loss) 20 15\n")
    res = _extract(text, "income_statement")
    assert not any(ln["line_item"] == "NonInterestIncome" for ln in res["line_rows"])
    assert "bank_key_on_nonbank" in res["flags"]


def test_bank_key_allowed_on_bank():
    text = ("Income\n(In thousands) 2025 2024\n"
            "Interest income 500 480\nNet interest income 300 290\n"
            "Non-interest income 50 45\nNet income 100 90\n")
    res = _extract(text, "income_statement")
    assert any(ln["line_item"] == "NetInterestIncome" for ln in res["line_rows"])


def test_eps_requires_per_share_context():
    # a bare 'Diluted' note line must NOT map to DilutedEPS (no per-share context).
    text = "Income\n(In thousands) 2025 2024\nRevenue 100 90\nDiluted 11 12\n"
    res = _extract(text, "income_statement")
    assert not any(ln["line_item"] == "DilutedEPS" for ln in res["line_rows"])
    # ...but a real per-share line does map.
    text2 = ("Income\n(In thousands) 2025 2024\nRevenue 100 90\n"
             "Diluted earnings per share 0.53 0.40\n")
    res2 = _extract(text2, "income_statement")
    assert any(ln["line_item"] == "DilutedEPS" and abs(ln["value"] - 0.53) < 1e-9
               for ln in res2["line_rows"])


def test_net_income_not_overridden_by_comprehensive():
    # "Total comprehensive income" must NOT override the real "Net income" line.
    text = ("Income\n(In thousands) 2025 2024\n"
            "Net income (loss) 36,440 30,000\n"
            "Total comprehensive income 44,751 41,000\n")
    res = _extract(text, "income_statement")
    ni = [ln for ln in res["line_rows"] if ln["line_item"] == "NetIncome" and ln["fiscal_year"] == 2025]
    assert ni and ni[0]["value"] == 36_440_000.0   # net income, not comprehensive


def test_period_end_detected():
    text = ("Income\nYears ended March 31, 2025 and 2024\n"
            "(In thousands) 2025 2024\nRevenue 100 90\n")
    p = r.parse_statement(text, "income_statement")
    assert p["period_ends"].get(2025) == "2025-03-31"
    assert p["period_ends"].get(2024) == "2024-03-31"


def test_confidence_reflects_identity_and_coverage():
    good = ("Balance Sheet\n(In thousands) 2025 2024\n"
            "Total current assets 40 35\nTotal non-current assets 60 55\n"
            "Total assets 100 90\nTotal current liabilities 30 25\n"
            "Total liabilities 60 55\nShareholders equity 40 35\n")
    res = _extract(good, "balance_sheet")
    # identity holds (100 == 60+40 and 100 == 40+60), key totals present, not sparse
    assert "balance_identity_fail" not in res["flags"]
    assert res["confidence"] > 0.8


def test_confidence_low_on_identity_fail():
    bad = ("Balance Sheet\n(In thousands) 2025 2024\n"
           "Total current assets 40 35\nTotal non-current assets 60 55\n"
           "Total assets 100 90\nTotal current liabilities 30 25\n"
           "Total liabilities 60 55\nShareholders equity 99 90\n")  # 100 != 60+99
    res = _extract(bad, "balance_sheet")
    assert "balance_identity_fail" in res["flags"]
    assert res["confidence"] < _extract(
        bad.replace("99 90", "40 35"), "balance_sheet")["confidence"]


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
