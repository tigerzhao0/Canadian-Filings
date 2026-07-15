"""Tests for the step-3 line-item parser (section tracking, value-only
subtotal capture, zones, junk guard) and the step-4 context-aware matcher.

The balance-sheet fixture is RY's REAL extracted text layout -- the exact case
that motivated the redesign (GrossLoan/NetLoan/InterestIncome subtotals were
being silently dropped).

Runs under pytest, or standalone:  python tests/test_line_items.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from line_items import parse_lines  # noqa: E402
import rule_extract as r  # noqa: E402

_VOCAB = r._load_vocab(r.DEFAULT_VOCAB)
_IDX = r.build_alias_index(_VOCAB)
_CIDX = r.build_compound_index(_VOCAB)

RY_BS = """Consolidated Balance Sheets
As at
October 31 October 31
(Millions of Canadian dollars) 2025 2024
Assets
Cash and due from banks $ 37,024 $ 56,723
Interest-bearing deposits with banks 50,364 66,020
Securities (Note 4)
Trading 219,067 183,300
Investment, net of applicable allowance 342,721 256,618
561,788 439,918
Assets purchased under reverse repurchase agreements and securities borrowed 309,683 350,803
Loans (Note 5)
Retail 652,344 626,978
Wholesale 397,171 360,439
1,049,515 987,417
Allowance for loan losses (Note 5) (7,093) (6,037)
1,042,422 981,380
Other
Derivatives (Note 9) 177,206 150,612
Total assets $ 2,325,006 $ 2,171,582
Liabilities and equity
Deposits (Note 14)
Personal $ 529,740 $ 522,139
Business and government 946,314 839,670
Bank 39,562 47,722
1,515,616 1,409,531
Other
Derivatives (Note 9) 183,953 163,763
Total liabilities 2,185,855 2,044,390
"""


def _labels(p):
    return {row.label: row.values for row in p.rows}


def test_subtotal_capture_gross_and_net_loans():
    p = parse_lines(RY_BS, "balance_sheet")
    lbl = _labels(p)
    assert lbl["Loans total"] == [1049515.0, 987417.0]          # -> GrossLoan
    assert lbl["Loans net total"] == [1042422.0, 981380.0]      # -> NetLoan
    assert lbl["Securities total"] == [561788.0, 439918.0]      # NOT "net" (allowance
    # was mid-label in "Investment, net of applicable allowance", not a deduction row)
    assert lbl["Deposits total"] == [1515616.0, 1409531.0]      # -> TotalDeposits


def test_section_and_zone_tracking():
    p = parse_lines(RY_BS, "balance_sheet")
    by_label = {}
    for row in p.rows:
        by_label.setdefault(row.label, []).append(row)
    retail = by_label["Retail"][0]
    assert retail.section == "Loans" and retail.zone == "assets"
    derivs = by_label["Derivatives (Note 9)"]
    assert derivs[0].zone == "assets"
    assert derivs[1].zone == "liabilities and equity"


def test_compound_matching_disambiguates():
    key, _, kind = r.match_label_ctx("Retail", "Loans", "assets",
                                     "balance_sheet", _IDX, _CIDX)
    assert key == "ConsumerLoan" and kind == "compound"
    key, _, _ = r.match_label_ctx("Wholesale", "Loans", "assets",
                                  "balance_sheet", _IDX, _CIDX)
    assert key == "CommercialLoan"
    # Derivatives: assets vs liabilities zone
    ka, _, _ = r.match_label_ctx("Derivatives (Note 9)", "Other", "assets",
                                 "balance_sheet", _IDX, _CIDX)
    kl, _, _ = r.match_label_ctx("Derivatives (Note 9)", "Other",
                                 "liabilities and equity", "balance_sheet", _IDX, _CIDX)
    assert ka == "DerivativeAssets" and kl == "DerivativeProductLiabilities"
    # Loans under INTEREST INCOME is interest income, not a loan balance
    ki, _, _ = r.match_label_ctx("Loans $", "Interest and dividend income", None,
                                 "income_statement", _IDX, _CIDX)
    assert ki == "InterestIncomeFromLoans"


def test_synthetic_subtotals_map_to_bank_keys():
    for label, want in (("Loans total", "GrossLoan"),
                        ("Loans net total", "NetLoan"),
                        ("Securities total", "SecuritiesAndInvestments"),
                        ("Deposits total", "TotalDeposits")):
        key, _, _ = r.match_label_ctx(label, None, None, "balance_sheet", _IDX, _CIDX)
        assert key == want, (label, key)


def test_note_ref_stripped_anywhere():
    key, _, _ = r.match_label_ctx("Provision for credit losses (Notes 4 and 5)",
                                  None, None, "income_statement", _IDX, _CIDX)
    assert key == "CreditLossesProvision"
    key, _, _ = r.match_label_ctx("Goodwill (Note 11)", None, None,
                                  "balance_sheet", _IDX, _CIDX)
    assert key == "Goodwill"


def test_suffix_salvages_prose_glued_label():
    glued = ("The engagement partner on the audit is Marie David. "
             "Income before income taxes")
    key, conf, kind = r.match_label_ctx(glued, None, None,
                                        "income_statement", _IDX, _CIDX)
    assert key == "PretaxIncome" and kind == "suffix"


def test_junk_guard_rejects_year_page_rows():
    text = ("Income Statement\n(Millions) 2025 2024\n"
            "Revenue 100 90\n"
            "Some header 2025 145\n")   # (year, page#) -- must not become a row
    p = parse_lines(text, "income_statement")
    lbl = _labels(p)
    assert "Revenue" in lbl
    assert not any("Some header" in k for k in lbl)


def test_column_year_detection_and_scale():
    p = parse_lines(RY_BS, "balance_sheet")
    assert p.col_years == [2025, 2024]
    assert p.scale == 1_000_000.0 and p.scale_found


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
