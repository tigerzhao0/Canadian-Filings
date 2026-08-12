#!/usr/bin/env python3
"""Compare our PDF-extracted financials against the GuruFocus API (QA / validation).

For each company in scope we pull GuruFocus's fundamentals (the paid dataset API),
align them field-by-field and year-by-year against what the pipeline stored in
output/pdf_financials.db, and emit a discrepancy report. The point is an independent
check on extraction accuracy: extraction bugs show up as concrete per-metric diffs.

    # default: a small TSX-weighted validation sample (cheapest to iterate)
    python src/gurufocus_compare.py

    python src/gurufocus_compare.py --tickers RY,AC,ACT
    python src/gurufocus_compare.py --exchange TSX --limit 100
    python src/gurufocus_compare.py --from-cache        # no API calls, replay cache

Outputs (into --out-dir, default output/):
    gurufocus_compare.csv             long-format per (ticker, year, metric) diff
    gurufocus_compare_dashboard.html  self-contained per-metric match-rate report

Two shapes are reconciled (see docs/ / the plan):
- pipeline: canonical Yahoo/QuoteMedia keys, RAW absolute dollars (EPS/shares native).
- GuruFocus: human labels, values in MILLIONS local currency (per-share/ratios native,
  share counts in millions). So money -> /1e6, shares -> /1e6, per-share/ratio as-is.

The sign/aggregation conventions QuoteMedia & GuruFocus share are ALREADY baked into
statement_lines by schema_map.py at store time, so we compare stored values directly;
a genuine extraction sign error surfaces here as a `sign_flip` status.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import httpx

import gurufocus_api as gf

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "output" / "pdf_financials.db"
# Prefer the real (git-ignored) config.yaml if the user made one -- that's where the
# GuruFocus api_token belongs -- else fall back to the committed template. (webapp.py
# uses the same config.yaml-over-example precedence.)
DEFAULT_CONFIG = ROOT / "config.yaml" if (ROOT / "config.yaml").exists() else ROOT / "config.example.yaml"

# --- the mapping table: GuruFocus field -> our canonical key(s) ------------------
# The table is GuruFocus-DRIVEN: one entry per GuruFocus field, because GuruFocus is
# the ground truth we diff against. Each entry lists our candidate canonical keys in
# PRIORITY order -- several of our keys are synonyms for one GuruFocus concept (e.g.
# Total/OperatingRevenue both = "Revenue"; CapitalExpenditure/PurchaseOfPPE both =
# "Capital Expenditure"), and we compare GuruFocus's single value against whichever
# candidate the pipeline actually populated. That avoids double-counting one GuruFocus
# number against two of our keys.
#
# unit: money (raw $ -> /1e6), shares (raw count -> /1e6), pershare/ratio (as-is).
# section is the GuruFocus JSON key (Title Case in this API). Each field carries a
# TUPLE of candidate GuruFocus labels because GuruFocus renders DIFFERENT label text
# per industry template -- e.g. a bank (RY) prints "Balance Statement Cash and cash
# equivalents" / "Net Interest Income (for Banks)" where an industrial prints "Cash
# and Cash Equivalents" -- and we match whichever label the response actually
# contains. Label strings were PINNED from a live RY (bank) response 2026-07-24;
# non-bank variants are added as extra candidates and refined via the tool's
# "labels never seen" report. Each field is
# (section, (gf_label candidates...), unit, (canonical key candidates...)).
M, S, P, R = "money", "shares", "pershare", "ratio"
IS, BS, CF, PS = "Income Statement", "Balance Sheet", "Cashflow Statement", "Per Share Data"

GF_FIELDS: list[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = [
    # ---- income statement ----
    (IS, ("Revenue",), M, ("TotalRevenue", "OperatingRevenue")),
    (IS, ("Cost of Goods Sold",), M, ("CostOfRevenue",)),
    (IS, ("Gross Profit",), M, ("GrossProfit",)),
    (IS, ("Selling, General, & Admin. Expense",), M, ("SellingGeneralAndAdministration",)),
    (IS, ("Research & Development",), M, ("ResearchAndDevelopment",)),
    (IS, ("Operating Income",), M, ("OperatingIncome",)),
    (IS, ("Interest Expense",), M, ("InterestExpense", "InterestExpenseNonOperating")),
    (IS, ("Interest Income",), M, ("InterestIncome",)),
    (IS, ("Net Interest Income (for Banks)", "Net Interest Income"), M, ("NetInterestIncome",)),
    (IS, ("Non Interest Income",), M, ("NonInterestIncome",)),
    (IS, ("Credit Losses Provision",), M, ("CreditLossesProvision",)),
    (IS, ("Other Income (Expense)",), M, ("OtherIncomeExpense",)),
    (IS, ("Pretax Income",), M, ("PretaxIncome",)),
    (IS, ("Tax Provision",), M, ("TaxProvision",)),
    (IS, ("Net Income Including Noncontrolling Interests",), M, ("NetIncomeFromContinuingAndDiscontinuedOperation",)),
    (IS, ("Net Income (Continuing Operations)",), M, ("NetIncomeContinuousOperations",)),
    (IS, ("Net Income (Discontinued Operations)",), M, ("NetIncomeDiscontinuousOperations",)),
    (IS, ("Net Income",), M, ("NetIncome",)),
    (IS, ("Preferred Dividends",), M, ("PreferredStockDividends",)),
    (IS, ("EBIT",), M, ("EBIT",)),
    (IS, ("EBITDA",), M, ("EBITDA",)),
    (IS, ("Depreciation, Depletion and Amortization",), M, ("DepreciationAmortizationDepletion",)),
    (IS, ("Other Income (Minority Interest)", "Minority Interest"), M, ("MinorityInterests",)),
    (IS, ("Shares Outstanding (Diluted Average)",), S, ("DilutedAverageShares",)),
    # NOTE: "Shares Outstanding (Basic Average)" does NOT exist in this API's
    # response (verified across all 43 cached templates) -- no BasicAverageShares
    # comparison is possible.
    (IS, ("EPS (Basic)",), P, ("BasicEPS",)),
    (IS, ("EPS (Diluted)",), P, ("DilutedEPS",)),
    # ---- per-share ----
    (PS, ("Dividends per Share",), P, ("DividendPerShare",)),
    # ---- balance sheet ----
    (BS, ("Cash and Cash Equivalents", "Balance Statement Cash and cash equivalents"), M, ("CashAndCashEquivalents",)),
    # labels below pinned against the real industrial template (WPK response)
    (BS, ("Cash, Cash Equivalents, Marketable Securities",), M, ("CashCashEquivalentsAndShortTermInvestments",)),
    (BS, ("Accounts Receivable",), M, ("AccountsReceivable",)),
    (BS, ("Total Receivables",), M, ("Receivables",)),
    (BS, ("Notes Receivable",), M, ("NotesReceivable",)),
    (BS, ("Other Current Receivables",), M, ("OtherReceivables",)),
    (BS, ("Total Inventories",), M, ("Inventory",)),
    (BS, ("Other Current Assets",), M, ("OtherCurrentAssets",)),
    (BS, ("Total Current Assets",), M, ("CurrentAssets",)),
    (BS, ("Gross Property, Plant and Equipment",), M, ("GrossPPE",)),
    (BS, ("Accumulated Depreciation",), M, ("AccumulatedDepreciation",)),
    (BS, ("Property, Plant and Equipment",), M, ("NetPPE",)),
    (BS, ("Goodwill",), M, ("Goodwill",)),
    (BS, ("Intangible Assets",), M, ("OtherIntangibleAssets",)),
    (BS, ("Total Long-Term Assets",), M, ("TotalNonCurrentAssets",)),
    (BS, ("Total Assets",), M, ("TotalAssets",)),
    (BS, ("Accounts Payable & Accrued Expense", "Accounts Payable"), M, ("AccountsPayable",)),
    (BS, ("Short-Term Debt & Capital Lease Obligation",), M, ("CurrentDebtAndCapitalLeaseObligation",)),
    (BS, ("Total Current Liabilities",), M, ("CurrentLiabilities",)),
    (BS, ("Long-Term Debt & Capital Lease Obligation",), M, ("LongTermDebtAndCapitalLeaseObligation",)),
    (BS, ("Long-Term Debt",), M, ("LongTermDebt",)),
    (BS, ("Total Long-Term Liabilities",), M, ("TotalNonCurrentLiabilities",)),
    (BS, ("Total Liabilities",), M, ("TotalLiabilities",)),
    (BS, ("Total Stockholders Equity",), M, ("StockholdersEquity",)),
    (BS, ("Total Equity",), M, ("TotalEquityGrossMinorityInterest",)),
    (BS, ("Retained Earnings",), M, ("RetainedEarnings",)),
    (BS, ("Additional Paid-In Capital",), M, ("AdditionalPaidInCapital",)),
    (BS, ("Treasury Stock",), M, ("TreasuryStock",)),
    (BS, ("Total Deposits",), M, ("TotalDeposits",)),
    (BS, ("Minority Interest",), M, ("MinorityInterest",)),
    # present in every cached template; we extract these keys -- compare them
    (BS, ("Common Stock",), M, ("CommonStock", "CapitalStock")),
    (BS, ("Preferred Stock",), M, ("PreferredStock",)),
    (BS, ("Accumulated other comprehensive income (loss)",), M,
        ("GainsLossesNotAffectingRetainedEarnings",)),
    (BS, ("Other Stockholders Equity",), M, ("OtherEquityInterest", "OtherEquityAdjustments")),
    # ---- cash flow ----
    (CF, ("Net Income From Continuing Operations", "Net Income"), M, ("NetIncomeFromContinuingOperations", "NetIncome")),
    (CF, ("Cash Flow Depreciation, Depletion and Amortization",), M, ("DepreciationAndAmortization", "DepreciationAmortizationDepletion")),
    (CF, ("Change In Receivables",), M, ("ChangeInReceivables",)),
    (CF, ("Change In Inventory",), M, ("ChangeInInventory",)),
    (CF, ("Change In Payables And Accrued Expense", "Change In Payables"), M, ("ChangeInPayablesAndAccruedExpense",)),
    (CF, ("Change In Working Capital",), M, ("ChangeInWorkingCapital",)),
    (CF, ("Stock Based Compensation",), M, ("StockBasedCompensation",)),
    (CF, ("Cash Flow from Operations",), M, ("OperatingCashFlow",)),
    (CF, ("Purchase Of Property, Plant, Equipment",), M, ("PurchaseOfPPE",)),
    (CF, ("Capital Expenditure",), M, ("CapitalExpenditure", "PurchaseOfPPE")),
    (CF, ("Cash Flow from Investing",), M, ("InvestingCashFlow",)),
    (CF, ("Issuance of Stock",), M, ("CommonStockIssuance",)),
    (CF, ("Repurchase of Stock",), M, ("RepurchaseOfCapitalStock",)),
    (CF, ("Net Issuance of Debt",), M, ("NetIssuancePaymentsOfDebt",)),
    (CF, ("Cash Flow for Dividends",), M, ("CashDividendsPaid",)),
    (CF, ("Cash Flow from Financing",), M, ("FinancingCashFlow",)),
    (CF, ("Free Cash Flow",), M, ("FreeCashFlow",)),
    (CF, ("Beginning Cash Position",), M, ("BeginningCashPosition",)),
    (CF, ("Ending Cash Position",), M, ("EndCashPosition",)),
    # present in every cached template; we extract these keys -- compare them
    (CF, ("Change In Prepaid Assets",), M, ("ChangeInPrepaidAssets",)),
    (CF, ("Change In Other Working Capital",), M, ("ChangeInOtherWorkingCapital",)),
    (CF, ("Deferred Tax",), M, ("DeferredTax", "DeferredIncomeTax")),
    (CF, ("Asset Impairment Charge",), M, ("AssetImpairmentCharge",)),
    (CF, ("Sale Of Property, Plant, Equipment",), M, ("SaleOfPPE",)),
    (CF, ("Purchase Of Business",), M, ("PurchaseOfBusiness",)),
    (CF, ("Sale Of Business",), M, ("SaleOfBusiness",)),
    (CF, ("Purchase Of Investment",), M, ("PurchaseOfInvestment",)),
    (CF, ("Sale Of Investment",), M, ("SaleOfInvestment",)),
    (CF, ("Net Intangibles Purchase And Sale",), M, ("NetIntangiblesPurchaseAndSale",)),
    (CF, ("Cash From Other Investing Activities",), M, ("NetOtherInvestingChanges",)),
    (CF, ("Issuance of Debt",), M, ("IssuanceOfDebt", "LongTermDebtIssuance")),
    (CF, ("Payments of Debt",), M, ("RepaymentOfDebt", "LongTermDebtPayments")),
    (CF, ("Other Financing",), M, ("NetOtherFinancingCharges",)),
    (CF, ("Effect of Exchange Rate Changes",), M, ("EffectOfExchangeRateChanges",)),
    (CF, ("Net Change in Cash",), M, ("ChangesInCash",)),
]

# The pipeline stores these in native units (per-share amounts / share counts), so
# statement_type isn't a reliable clue to the unit -- the map's `unit` field is
# authoritative. (Mirrors llm_extract.NO_SCALE_KEYS.)
MILLIONS = 1_000_000.0

# Which pdf_financials.db statement_type a GuruFocus section maps to, so we can pull
# the stored value. Per-share (Dividends) is stored under income_statement.
_KEY_STMT = {
    IS: "income_statement", BS: "balance_sheet", CF: "cash_flow", PS: "income_statement",
}

# Headline financials (the numbers an investor actually reads). The long tail of
# GF's REBUCKETED detail rows (their "Accounts Payable & Accrued Expense" vs a
# filing's bare AP line; their derived EBITDA; Other-bucket allocations) differs
# DEFINITIONALLY from the statement face -- reported separately so bucket noise
# doesn't bury the core-accuracy signal.
CORE_KEYS = {
    "TotalRevenue", "NetIncome", "PretaxIncome", "TaxProvision", "TotalAssets",
    "TotalLiabilities", "StockholdersEquity", "TotalEquityGrossMinorityInterest",
    "CashAndCashEquivalents", "OperatingCashFlow", "InvestingCashFlow",
    "FinancingCashFlow", "BasicEPS", "DilutedEPS", "RetainedEarnings",
    "CurrentAssets", "CurrentLiabilities", "NetPPE", "Goodwill", "TotalDeposits",
    "CapitalExpenditure", "FreeCashFlow", "EndCashPosition", "NetInterestIncome",
    "InterestIncome", "InterestExpense", "DilutedAverageShares", "CommonStock",
}

# GuruFocus/Morningstar CALCULATED constructs -- values neither source reads off a
# filing, they compute them (and each side may use a different formula). The project
# cares about RAW reported line items, so these are reported separately and excluded
# from the headline rate rather than counted as extraction failures.
DERIVED_KEYS = {
    "EBITDA", "EBIT", "FreeCashFlow", "CapitalExpenditure", "OperatingIncome",
    "GrossProfit", "PretaxIncome", "NetIncomeContinuousOperations",
    "NetIncomeFromContinuingAndDiscontinuedOperation", "ChangeInWorkingCapital",
    "NetIssuancePaymentsOfDebt", "NetOtherInvestingChanges", "NetOtherFinancingCharges",
    "NetIntangiblesPurchaseAndSale", "TotalNonCurrentAssets",
    "TotalNonCurrentLiabilities", "WorkingCapital", "TotalEquityGrossMinorityInterest",
}

# Verdicts where OUR number is proven correct against the filing even though it
# differs from GuruFocus (their analyst normalization / sign convention / a
# documented definitional gap). match + these = "verified correct".
_VERIFIED_VERDICTS = {"pdf_backs_us", "both_ok_sign", "definitional"}

# comparison thresholds
DEFAULT_TOL_PCT = 2.0
# Below this absolute magnitude (in the compared unit) both sides are treated as
# noise-equal -- avoids flagging $0.0003M rounding dust or 0-vs-0.
_NOISE_FLOOR = {M: 0.05, S: 0.05, P: 0.005, R: 0.005}  # $50k, 50k shares, half a cent

# --- 3-way adjudication (GuruFocus vs ours vs the raw PDF line) -------------------
# When we disagree with GuruFocus the PDF is the arbiter. We reuse verify_company's
# hard-won QuoteMedia classifications: keys where a sign convention or a genuine
# definitional gap is EXPECTED (so a delta there is not our extraction error).
try:  # reuse the canonical lists so they can't drift from the QM comparison path
    from verify_company import SIGN_ONLY_KEYS, DEFINITIONAL_DIFFS  # type: ignore
except Exception:  # pragma: no cover - fallback copy (src/verify_company.py:23-38)
    SIGN_ONLY_KEYS = {"AllowanceForLoansAndLeaseLosses", "CreditLossesProvision",
                      "CashDividendsPaid", "MinorityInterests", "PreferredStockDividends"}
    DEFINITIONAL_DIFFS = {
        "InterestIncome": "face value matches GF display; QM nets deposit interest",
        "InterestExpense": "face value matches GF display; QM nets deposit interest",
        "ChangesInCash": "QM excludes FX effect; face matches GF display",
        "BasicEPS": "QM recomputes (~2% off reported); face matches GF",
        "DilutedEPS": "QM recomputes (~2% off reported); face matches GF",
        "BasicContinuousOperations": "fanned from BasicEPS; same recompute gap",
        "DilutedContinuousOperations": "fanned from DilutedEPS; same recompute gap",
        "DividendPerShare": "we store DECLARED per the face; QM/GF use paid",
    }
# GuruFocus prints income-statement expense/charge lines and cash OUTFLOWS as
# negative, where we (and the filing face) carry positive magnitudes -- a pure sign
# convention, not an error, confirmed on RY (Interest expense printed +7,958M).
_GF_SIGNS_NEGATIVE = {
    "InterestExpense", "InterestExpenseNonOperating", "TaxProvision", "CostOfRevenue",
    "SellingGeneralAndAdministration", "OperatingExpense", "ResearchAndDevelopment",
    "CreditLossesProvision", "PreferredStockDividends",
    "CapitalExpenditure", "PurchaseOfPPE", "RepurchaseOfCapitalStock",
    "CashDividendsPaid", "ChangeInInventory", "ChangeInReceivables",
    # pure outflows / one-direction charges (magnitude-safe): GF prints them
    # negative, filings often print bare magnitudes -- same number either way.
    "PurchaseOfInvestment", "PurchaseOfBusiness", "RepaymentOfDebt",
    "LongTermDebtPayments", "AssetImpairmentCharge",
}
# match_kind ranked best->worst; exact/compound/suffix = the number is literally on
# the filing face, fuzzy = a lower-confidence label match worth a human look.
_KIND_ORDER = {"exact": 0, "compound": 1, "suffix": 2, "fuzzy": 3, None: 9}


# --------------------------------------------------------------------------- utils
def _load_config(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        return {}
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _resolve_token(cfg: dict) -> str:
    """config gurufocus.api_token first, then GURUFOCUS_API_TOKEN env (the exact
    pattern the Google-CSE key uses in run.py / search_provider.py)."""
    return ((cfg.get("gurufocus", {}) or {}).get("api_token")
            or os.environ.get("GURUFOCUS_API_TOKEN", "") or "").strip()


def _coerce(v) -> Optional[float]:
    """GuruFocus values arrive as numbers OR strings like '1,234.5' / 'N/A' / '-'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "N/A", "NaN", "null", "None"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = s.rstrip("%")
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def _fy_int(fy) -> Optional[int]:
    """'2024-05' / '2024' / 2024 -> 2024; 'TTM' / '' -> None."""
    s = str(fy).strip()
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    return None


# --------------------------------------------------------- GuruFocus response parse
def extract_annuals(payload: dict) -> Optional[dict]:
    """Locate the annual financials block regardless of small wrapper differences."""
    if not isinstance(payload, dict):
        return None
    fin = payload.get("financials")
    if isinstance(fin, dict) and isinstance(fin.get("annuals"), dict):
        return fin["annuals"]
    if isinstance(payload.get("annuals"), dict):
        return payload["annuals"]
    return None


def parse_gf(payload: dict) -> tuple[dict[int, dict[str, dict[str, Optional[float]]]], set[str], Optional[str]]:
    """Return (by_year, seen_labels, currency).

    by_year: {fiscal_year_int: {section: {gf_label: value}}}
    seen_labels: every '<section>:<label>' present (for map-coverage reporting)
    currency: best-effort reporting currency, else None.
    """
    annuals = extract_annuals(payload) or {}
    fy_list = annuals.get("Fiscal Year") or annuals.get("Fiscal_Year") or []
    years = [_fy_int(x) for x in fy_list]

    by_year: dict[int, dict[str, dict[str, Optional[float]]]] = {}
    seen: set[str] = set()
    for section in (IS, BS, CF, PS):
        sect = annuals.get(section)
        if not isinstance(sect, dict):
            continue
        for label, arr in sect.items():
            if label in ("Fiscal Year", "Fiscal_Year"):
                continue
            seen.add(f"{section}:{label}")
            if not isinstance(arr, list):
                continue
            for i, raw in enumerate(arr):
                if i >= len(years) or years[i] is None:
                    continue
                by_year.setdefault(years[i], {}).setdefault(section, {})[label] = _coerce(raw)

    # best-effort currency: GuruFocus exposes it in a few possible spots
    currency = None
    for path in (("financials", "financial_template_parameters", "currency"),
                 ("summary", "company_data", "currency"),
                 ("financials", "currency")):
        node = payload
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, str) and node:
            currency = node.upper()
            break
    return by_year, seen, currency


# --------------------------------------------------------------- the value compare
def _normalize_pipeline(value: float, unit: str) -> float:
    if unit in (M, S):
        return value / MILLIONS
    return value  # per-share / ratio already native


def classify(pipe_norm: Optional[float], gf_val: Optional[float], unit: str,
             tol_pct: float) -> tuple[str, Optional[float]]:
    """Return (status, abs_pct_diff). status in match/mismatch/sign_flip/
    missing_pipeline/missing_gf."""
    if pipe_norm is None and gf_val is None:
        return "both_absent", None
    if pipe_norm is None:
        return "missing_pipeline", None
    if gf_val is None:
        return "missing_gf", None

    floor = _NOISE_FLOOR.get(unit, 0.0)
    if abs(pipe_norm) <= floor and abs(gf_val) <= floor:
        return "match", 0.0

    denom = max(abs(gf_val), abs(pipe_norm), 1e-9)
    abs_pct = abs(pipe_norm - gf_val) / denom * 100.0
    # magnitudes agree but signs oppose -> extraction sign error
    if pipe_norm * gf_val < 0:
        mag_pct = abs(abs(pipe_norm) - abs(gf_val)) / max(abs(gf_val), 1e-9) * 100.0
        if mag_pct <= tol_pct:
            return "sign_flip", abs_pct
    if abs_pct <= tol_pct:
        return "match", abs_pct
    return "mismatch", abs_pct


# ------------------------------------------------------------------- DB / selection
def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _evenly_spaced(items: list, n: int) -> list:
    if n >= len(items):
        return items
    step = len(items) / n
    idxs = sorted({min(len(items) - 1, int(i * step)) for i in range(n)})
    return [items[i] for i in idxs]


def select_companies(conn: sqlite3.Connection, *, tickers: Optional[list[str]],
                     exchange: Optional[str], sample: int, limit: Optional[int],
                     progress) -> list[tuple[str, str, Optional[str]]]:
    """Return [(ticker, exchange, currency)] for the run's scope."""
    if tickers:
        rows = []
        for t in tickers:
            r = conn.execute("SELECT ticker, exchange, currency FROM companies WHERE ticker=?",
                             (t,)).fetchone()
            if r:
                rows.append((r["ticker"], r["exchange"], r["currency"]))
            else:
                progress(f"  (skip) {t}: not in the DB -- no extracted data to compare")
        selected = rows
    elif exchange:
        selected = [(r["ticker"], r["exchange"], r["currency"]) for r in conn.execute(
            "SELECT ticker, exchange, currency FROM companies WHERE exchange=? ORDER BY ticker",
            (exchange.upper(),))]
    else:
        # default validation sample: TSX-weighted (best GuruFocus coverage), evenly
        # spaced across the ticker list so it isn't just the alphabetical head.
        tsx = [(r["ticker"], r["exchange"], r["currency"]) for r in conn.execute(
            "SELECT ticker, exchange, currency FROM companies WHERE exchange='TSX' ORDER BY ticker")]
        selected = _evenly_spaced(tsx, sample)
    if limit is not None:
        selected = selected[:limit]
    return selected


def load_pipeline_values(conn: sqlite3.Connection, ticker: str
                         ) -> dict[int, dict[str, dict[str, Optional[float]]]]:
    """{fiscal_year: {statement_type: {line_item: value}}} for one ticker."""
    out: dict[int, dict[str, dict[str, Optional[float]]]] = {}
    for r in conn.execute(
        "SELECT fiscal_year, statement_type, line_item, value FROM statement_lines "
        "WHERE ticker=?", (ticker,)):
        out.setdefault(r["fiscal_year"], {}).setdefault(r["statement_type"], {})[r["line_item"]] = r["value"]
    return out


def load_provenance(conn: sqlite3.Connection, ticker: str
                    ) -> dict[tuple[int, str, str], str]:
    """TRUE provenance per stored cell, straight from statement_line_quality:
    'mapped' (read off the statement face) | 'from_notes' (read off a note) |
    'derived' (computed by OUR OWN formulas, not printed anywhere) |
    'absent_on_face' (zero-filled placeholder -- no real number was ever read).
    Keyed by (fiscal_year, statement_type, canonical_key).

    This is NOT the same thing as load_pdf_evidence's match_kind: that one only
    covers face-mapped rows (line_items_full), so it can't tell a from_notes value
    apart from a derived one -- both show up as 'no evidence'. For a genuine
    raw-vs-computed split this table is the source of truth."""
    return {(r["fiscal_year"], r["statement_type"], r["line_item"]): r["match_kind"]
            for r in conn.execute(
                "SELECT fiscal_year, statement_type, line_item, match_kind "
                "FROM statement_line_quality WHERE ticker=?", (ticker,))}


def load_pdf_evidence(conn: sqlite3.Connection, ticker: str
                      ) -> dict[tuple[int, str], dict]:
    """The raw PDF line(s) behind each canonical value, so we can adjudicate a
    disagreement with GuruFocus. Keyed by (fiscal_year, canonical_key) ->
    {raw_label, value, src_year, match_kind}. Resolution mirrors the pipeline
    (schema_map.py): keep only the NEWEST source_pdf_year for that cell, then SUM
    sibling rows (AGGREGATE_KEYS split one concept across lines) and keep the best
    (lowest-rank) match_kind + a joined raw_label. Absence of a key here = the
    stored value was derived/zero-filled, NOT read off the PDF face."""
    groups: dict[tuple[int, str], list[sqlite3.Row]] = {}
    for r in conn.execute(
        "SELECT fiscal_year, canonical_key, source_pdf_year, raw_label, value, match_kind "
        "FROM line_items_full WHERE ticker=? AND canonical_key IS NOT NULL "
        "AND value IS NOT NULL", (ticker,)):
        groups.setdefault((r["fiscal_year"], r["canonical_key"]), []).append(r)

    ev: dict[tuple[int, str], dict] = {}
    for key, rs in groups.items():
        newest = max(x["source_pdf_year"] for x in rs)
        grp = [x for x in rs if x["source_pdf_year"] == newest]
        total = sum(x["value"] for x in grp)
        labels = "; ".join(dict.fromkeys(x["raw_label"] for x in grp if x["raw_label"]))
        best_kind = min((x["match_kind"] for x in grp),
                        key=lambda k: _KIND_ORDER.get(k, 9))
        ev[key] = {"raw_label": labels, "value": total, "src_year": newest,
                   "match_kind": best_kind, "n_lines": len(grp)}
    return ev


def _mag_agrees(a: Optional[float], b: Optional[float], tol_pct: float) -> bool:
    """|a| == |b| within tol. A sign-convention verdict is only honest when the two
    sides carry the SAME magnitude -- otherwise the numbers genuinely disagree and
    calling it 'both correct' would over-report our accuracy."""
    if a is None or b is None:
        return False
    return abs(abs(a) - abs(b)) <= (tol_pct / 100.0) * max(abs(b), 1e-9)


def adjudicate(status: str, canon: str, unit: str, pipe_norm: Optional[float],
               gf_val: Optional[float], ev: Optional[dict],
               tol_pct: float = DEFAULT_TOL_PCT) -> str:
    """Who is actually correct? The PDF (ev) is the arbiter. Returns a verdict:
      match               -- already agree
      both_ok_sign        -- |equal|, opposite sign: GF's negative convention, PDF
                             confirms our magnitude. Both correct.
      definitional        -- a known face-vs-GF definitional gap (EPS recompute etc.)
      pdf_backs_us        -- our number is literally on the filing face (exact/
                             compound/suffix) and GF differs -> GF restated/redefined
      ours_fuzzy_suspect  -- our value came from a FUZZY label match and disagrees
                             -> our mapping is the suspect, check it
      ours_not_pdf_backed -- our value was derived/zero-filled (no face line) and GF
                             has a real number -> GF likelier right; we missed a line
      scale_error         -- ours ~= GF x1000 or /1000 -> a scale bug (ours)
      gf_only             -- GF reports it, we have nothing at all
      unresolved          -- PDF-backed, not sign/scale/def: genuine, needs eyes
    """
    if status == "match":
        return "match"
    # One-sided cells are NOT extraction disagreements, resolve them first:
    if status == "missing_gf":   # we report it, GuruFocus doesn't (often a derived
        return "gf_absent"       # metric GF omits for this template, e.g. a bank's
                                 # GrossProfit/EBIT) -- not our issue.
    if status == "missing_pipeline":  # GF reports it, we have nothing at all
        return "gf_only"
    # from here both sides have a value and disagree.
    # x1000 scale mistakes -- unambiguous and actionable
    if pipe_norm and gf_val:
        ratio = abs(pipe_norm) / max(abs(gf_val), 1e-12)
        if 900 < ratio < 1100 or 0.0009 < ratio < 0.0011:
            return "scale_error"
    # Sign-convention verdict requires the MAGNITUDES to actually agree. (Bug found
    # 2026-07-25 via the GF cross-check: this used to fire on any sign_flip status or
    # any key in the sign sets, so e.g. AAB OperatingCashFlow ours=-6.75 vs GF=+3.39
    # was reported as "both correct" -- a genuine 78% disagreement dressed up as a
    # presentation difference, inflating apparent accuracy.)
    if (status == "sign_flip" or (canon in _GF_SIGNS_NEGATIVE and canon in SIGN_ONLY_KEYS)) \
            and _mag_agrees(pipe_norm, gf_val, tol_pct):
        return "both_ok_sign"
    if canon in DEFINITIONAL_DIFFS:
        return "definitional"
    # let the PDF adjudicate the remaining genuine disagreements
    if ev is None:
        # ours has no PDF line. If ours is ~0 it's a zero-fill COVERAGE GAP (often a
        # template difference -- a bank has no "SG&A" line, an airline no "COGS" --
        # GF forces a value into a slot the filing doesn't have), NOT an extraction
        # error. A non-zero value with no face line is a DERIVED figure that
        # disagrees, which is a likelier real problem.
        if pipe_norm is None or abs(pipe_norm) <= _NOISE_FLOOR.get(unit, 0.0):
            return "ours_zerofill_gap"
        return "ours_derived_diff"
    if ev["match_kind"] == "fuzzy":
        return "ours_fuzzy_suspect"
    if ev["match_kind"] in ("exact", "compound", "suffix"):
        # our number is on the face. An opposite sign is only a CONVENTION difference
        # when the magnitudes match; otherwise the values genuinely differ and this is
        # a real discrepancy (see _mag_agrees).
        if pipe_norm and gf_val and pipe_norm * gf_val < 0 \
                and _mag_agrees(pipe_norm, gf_val, tol_pct):
            return "both_ok_sign"
        return "pdf_backs_us"
    return "unresolved"


# ------------------------------------------------------------------------- compare
# Keys where GuruFocus's sign is a presentation convention (expenses/outflows shown
# negative; contra/attribution lines flipped) -- both sides carry the SAME number, so
# magnitude equality counts as a match (verdict keeps both_ok_sign for transparency).
_SIGN_MAG_KEYS = _GF_SIGNS_NEGATIVE | SIGN_ONLY_KEYS

# FX-band guards for per-year currency detection: a uniform ours/GF ratio across many
# independent money metrics in one year is a currency conversion, not coincidence.
_FX_MIN_CELLS = 6
_FX_BAND = (0.50, 2.00)       # plausible FX factors (excl. ~1.0, see _FX_NEUTRAL)
_FX_NEUTRAL = 0.06            # |ratio-1| below this = same currency, no FX
_FX_CONSISTENCY = 0.05        # IQR/median must be tighter than this


def _detect_fx(pairs: list[tuple]) -> Optional[float]:
    """pairs = [(unit, pipe_norm, gf_val), ...] for ONE company-year. Returns the
    FX factor r (compare ours against GF*r) when the money cells show a uniform
    non-1.0 ratio, else None."""
    import statistics
    ratios = sorted(abs(p / g) for u, p, g in pairs
                    if u == M and p and g and abs(g) > 1e-9)
    if len(ratios) < _FX_MIN_CELLS:
        return None
    med = statistics.median(ratios)
    if not (_FX_BAND[0] <= med <= _FX_BAND[1]) or abs(med - 1.0) <= _FX_NEUTRAL:
        return None
    n = len(ratios)
    iqr = ratios[(3 * n) // 4] - ratios[n // 4]
    if iqr / med >= _FX_CONSISTENCY:
        return None
    return med


def _fx_currency_label(fx: float) -> str:
    """Human label for the detected reporting currency (GF side is CAD)."""
    return "USD" if 0.55 <= fx <= 0.92 else f"FX x{1/fx:.2f}"


def compare_company(ticker: str, exchange: Optional[str], pipe_currency: Optional[str],
                    pipe_vals: dict, gf_payload: Optional[dict], tol_pct: float,
                    seen_labels: set[str], pdf_evidence: Optional[dict] = None,
                    provenance: Optional[dict] = None
                    ) -> tuple[list[dict], dict]:
    """Produce per-(year, metric) rows for one company plus a small stats dict.

    Three correctness layers beyond raw cell compare:
    - fiscal-year OFFSET: our year y is paired with GF year y+offset, offset chosen
      from {0,-1,+1} by whichever strictly maximizes matches (off-calendar FYE
      labeling drift, e.g. Jan year-ends).
    - per-year FX detection: GF converts TSX values to CAD; USD reporters otherwise
      mismatch every money cell by the FX rate. See _detect_fx.
    - sign-convention magnitude scoring for _SIGN_MAG_KEYS.
    """
    pdf_evidence = pdf_evidence or {}
    provenance = provenance or {}
    base_stats = {"ticker": ticker, "exchange": exchange or "", "gf_covered": False,
                  "years_pipeline": sorted(pipe_vals),
                  "years_gf": [], "years_overlap": [], "cells": 0, "match": 0,
                  "offset": 0, "currency": "", "fx_years": 0}
    if not gf_payload or gf.is_no_coverage(gf_payload):
        return [], base_stats

    gf_by_year, labels, gf_currency = parse_gf(gf_payload)
    seen_labels.update(labels)
    if not gf_by_year:
        return [], base_stats

    def run(offset: int) -> tuple[list[dict], dict]:
        stats = dict(base_stats, gf_covered=True, years_gf=sorted(gf_by_year),
                     offset=offset)
        rows: list[dict] = []
        overlap = sorted(y for y in pipe_vals if (y + offset) in gf_by_year)
        stats["years_overlap"] = overlap
        fx_years = 0
        detected_cur = ""
        for year in overlap:
            gf_year = year + offset
            gf_sections = gf_by_year[gf_year]
            year_stmts = pipe_vals.get(year, {})
            # ---- pass 1: collect the year's field pairs -----------------------
            collected = []  # (section,label,unit,candidates,canon,stmt,pipe_norm,gf_val)
            for section, labs, unit, candidates in GF_FIELDS:
                stmt = _KEY_STMT[section]
                sect = gf_sections.get(section, {})
                gf_val, label = None, labs[0]
                for lab in labs:
                    if lab in sect:
                        gf_val, label = sect[lab], lab
                        break
                pipe_raw, canon = None, candidates[0]
                for cand in candidates:
                    v = year_stmts.get(stmt, {}).get(cand)
                    if v is not None:
                        pipe_raw, canon = v, cand
                        break
                if pipe_raw is None and gf_val is None:
                    continue
                pipe_norm = None if pipe_raw is None else _normalize_pipeline(pipe_raw, unit)
                collected.append((section, label, unit, canon, stmt, pipe_norm, gf_val))
            # ---- FX detection over this year's money cells --------------------
            fx = _detect_fx([(u, p, g) for _s, _l, u, _c, _st, p, g in collected])
            if fx:
                fx_years += 1
                detected_cur = detected_cur or _fx_currency_label(fx)
            # ---- pass 2: classify with fx + sign-magnitude --------------------
            for section, label, unit, canon, stmt, pipe_norm, gf_val in collected:
                gf_adj = gf_val
                if fx and gf_val is not None and unit in (M, P):
                    gf_adj = gf_val * fx     # shares/ratios are currency-free
                status, abs_pct = classify(pipe_norm, gf_adj, unit, tol_pct)
                if status == "both_absent":
                    continue
                # sign-convention keys: magnitude equality IS a match
                sign_conv = False
                if status in ("mismatch", "sign_flip") and canon in _SIGN_MAG_KEYS \
                        and pipe_norm is not None and gf_adj is not None:
                    mag_pct = (abs(abs(pipe_norm) - abs(gf_adj))
                               / max(abs(gf_adj), 1e-9) * 100.0)
                    if mag_pct <= tol_pct:
                        status, abs_pct, sign_conv = "match", mag_pct, True
                ev = pdf_evidence.get((year, canon))
                verdict = adjudicate(status, canon, unit, pipe_norm, gf_adj, ev, tol_pct)
                if status == "match" and (sign_conv or (
                        pipe_norm and gf_adj and pipe_norm * gf_adj < 0)):
                    verdict = "both_ok_sign"  # matched, but flag the sign convention
                rows.append({
                    "ticker": ticker, "exchange": exchange or "", "fiscal_year": year,
                    "gf_year": gf_year,
                    "statement_type": stmt, "canonical_key": canon, "gf_label": label,
                    "unit": unit,
                    "pipeline_value": None if pipe_norm is None else round(pipe_norm, 6),
                    "gurufocus_value": None if gf_val is None else round(gf_val, 6),
                    "abs_pct_diff": None if abs_pct is None else round(abs_pct, 3),
                    "status": status,
                    "verdict": verdict,
                    "fx_factor": round(fx, 4) if fx else "",
                    "pdf_raw_label": (ev or {}).get("raw_label", ""),
                    "pdf_value": (None if not ev else round(_normalize_pipeline(ev["value"], unit), 6)),
                    "pdf_source_year": (ev or {}).get("src_year", ""),
                    "match_kind": (ev or {}).get("match_kind", "") or "",
                    "provenance": provenance.get((year, stmt, canon), ""),
                    "currency_pipeline": (detected_cur if fx else (pipe_currency or "")),
                    "currency_gf": ("CAD" if fx else (gf_currency or "")),
                })
                if status in ("match", "mismatch", "sign_flip", "currency_mismatch"):
                    stats["cells"] += 1
                    if status == "match":
                        stats["match"] += 1
        stats["fx_years"] = fx_years
        stats["currency"] = detected_cur or (pipe_currency or "")
        return rows, stats

    # A3: pick the year-pairing offset that strictly maximizes matches (ties -> 0).
    best_rows, best_stats = run(0)
    for off in (-1, 1):
        rows_o, stats_o = run(off)
        if stats_o["match"] > best_stats["match"]:
            best_rows, best_stats = rows_o, stats_o
    return best_rows, best_stats


# --------------------------------------------------------------------------- output
CSV_COLS = ["ticker", "exchange", "fiscal_year", "gf_year", "statement_type",
            "canonical_key", "gf_label", "unit", "pipeline_value", "gurufocus_value",
            "abs_pct_diff", "status", "verdict", "fx_factor", "pdf_raw_label",
            "pdf_value", "pdf_source_year", "match_kind", "provenance",
            "currency_pipeline", "currency_gf"]


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _per_metric_rates(rows: list[dict]) -> list[dict]:
    agg: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["status"] not in ("match", "mismatch", "sign_flip", "currency_mismatch"):
            continue
        a = agg.setdefault(r["canonical_key"], {"n": 0, "match": 0})
        a["n"] += 1
        if r["status"] == "match":
            a["match"] += 1
    out = [{"key": k, "n": v["n"], "match": v["match"],
            "rate": (v["match"] / v["n"] * 100.0) if v["n"] else 0.0}
           for k, v in agg.items()]
    out.sort(key=lambda d: (d["rate"], -d["n"]))  # worst first
    return out


def per_company_stats(rows: list[dict], company_stats: list[dict]) -> list[dict]:
    """One row per covered company: match rate, cell count, verdict breakdown, and
    its single worst genuine-issue cell -- the "what was different for this stock"
    view. Sorted worst-match-rate first (the companies most worth a human look)."""
    _ISSUE_V = {"scale_error", "ours_derived_diff", "ours_fuzzy_suspect"}
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(r)

    out = []
    for s in company_stats:
        tk = s["ticker"]
        if not s.get("gf_covered"):
            continue
        trows = by_ticker.get(tk, [])
        comparable = [r for r in trows if r["status"] in
                     ("match", "mismatch", "sign_flip", "currency_mismatch")]
        matched = sum(1 for r in comparable if r["status"] == "match")
        issues = [r for r in trows if r.get("verdict") in _ISSUE_V]
        worst = max((r for r in issues if r["abs_pct_diff"] is not None),
                   key=lambda r: r["abs_pct_diff"], default=None)
        vc = Counter(r.get("verdict", "") for r in trows)
        out.append({
            "ticker": tk, "exchange": s.get("exchange", ""),
            "cells": len(comparable),
            "match_pct": round(matched / len(comparable) * 100, 1) if comparable else 0.0,
            "our_issues": len(issues),
            "zerofill_gaps": vc.get("ours_zerofill_gap", 0),
            "pdf_backs_us": vc.get("pdf_backs_us", 0),
            "worst": worst,
            "years": len(s.get("years_overlap", [])),
            "currency": s.get("currency", ""),
            "offset": s.get("offset", 0),
        })
    out.sort(key=lambda c: (c["match_pct"], -c["our_issues"]))
    return out


def print_summary(rows: list[dict], company_stats: list[dict], seen_labels: set[str],
                  tol_pct: float, progress) -> None:
    attempted = len(company_stats)
    covered = sum(1 for s in company_stats if s["gf_covered"])
    no_cov = attempted - covered
    comparable = [r for r in rows if r["status"] in ("match", "mismatch", "sign_flip", "currency_mismatch")]
    total = len(comparable)
    matched = sum(1 for r in comparable if r["status"] == "match")
    sign = sum(1 for r in comparable if r["status"] == "sign_flip")
    curm = sum(1 for r in comparable if r["status"] == "currency_mismatch")
    miss_p = sum(1 for r in rows if r["status"] == "missing_pipeline")
    miss_g = sum(1 for r in rows if r["status"] == "missing_gf")

    def pct(n, d): return (n / d * 100.0) if d else 0.0

    progress("\n" + "=" * 56)
    progress("  GuruFocus comparison")
    progress("=" * 56)
    progress(f"  companies attempted        : {attempted}")
    progress(f"  with GuruFocus data        : {covered}")
    progress(f"  no GuruFocus coverage      : {no_cov}")
    progress("  " + "-" * 52)
    core = [r for r in comparable if r["canonical_key"] in CORE_KEYS]
    core_m = sum(1 for r in core if r["status"] == "match")
    raw = [r for r in comparable if r["canonical_key"] not in DERIVED_KEYS]
    raw_m = sum(1 for r in raw if r["status"] == "match")
    raw_v = sum(1 for r in raw if r["status"] == "match"
                or r.get("verdict") in _VERIFIED_VERDICTS)
    # TRUE-RAW: scoped by OUR OWN provenance (statement_line_quality.match_kind), not
    # just by GF's concept name. A cell only counts here if we actually READ a number
    # off the filing (face or notes) for it -- excludes every cell where OUR side is
    # 'derived' (computed by our own formulas) or 'absent_on_face' (zero-filled, no
    # real number was ever extracted), regardless of what GF calls the metric.
    true_raw = [r for r in comparable if r.get("provenance") in ("mapped", "from_notes")]
    tr_m = sum(1 for r in true_raw if r["status"] == "match")
    tr_v = sum(1 for r in true_raw if r["status"] == "match"
              or r.get("verdict") in _VERIFIED_VERDICTS)
    progress(f"  comparable cells           : {total}")
    progress(f"  matched (<= {tol_pct:g}%)         : {matched}  ({pct(matched, total):.1f}%)  (all metrics)")
    progress("  " + "-" * 52)
    progress(f"  RAW-DATA match             : {raw_m}/{len(raw)}  "
             f"({pct(raw_m, len(raw)):.1f}%)   <- reported line items (excl. calculated)")
    progress(f"  RAW-DATA verified-correct  : {raw_v}/{len(raw)}  "
             f"({pct(raw_v, len(raw)):.1f}%)   <- matches GF, or proven vs the filing")
    progress(f"  TRUE-RAW match (our prov.) : {tr_m}/{len(true_raw)}  "
             f"({pct(tr_m, len(true_raw)):.1f}%)   <- WE extracted it (not derived/empty)")
    progress(f"  TRUE-RAW verified-correct  : {tr_v}/{len(true_raw)}  "
             f"({pct(tr_v, len(true_raw)):.1f}%)   <- of what we actually extracted")
    progress(f"  CORE headline metrics      : {core_m}/{len(core)}  "
             f"({pct(core_m, len(core)):.1f}%)   <- revenue/income/assets/equity/CF/EPS")
    progress(f"  sign-flipped               : {sign}")
    progress(f"  currency-mismatch flagged  : {curm}")
    progress(f"  we have, GuruFocus absent  : {miss_g}")
    progress(f"  GuruFocus has, we absent   : {miss_p}")

    # 3-way adjudication: when we disagree with GuruFocus, who does the PDF back?
    vc = Counter(r.get("verdict", "") for r in rows)
    # verdicts where WE are (or may be) wrong -- the actionable extraction bugs
    OURS = ["scale_error", "ours_derived_diff", "ours_fuzzy_suspect"]
    OK = ["both_ok_sign", "definitional", "pdf_backs_us"]
    progress("  " + "-" * 52)
    progress("  ADJUDICATION vs the PDF (who is correct on disagreements):")
    progress(f"    both correct (GF sign/definitional; PDF backs us) : "
             f"{sum(vc.get(v,0) for v in OK)}")
    progress(f"      - both_ok_sign   (GF's negative convention)     : {vc.get('both_ok_sign',0)}")
    progress(f"      - pdf_backs_us   (our number is on the filing)  : {vc.get('pdf_backs_us',0)}")
    progress(f"      - definitional   (known face-vs-GF gap)         : {vc.get('definitional',0)}")
    progress(f"    OUR genuine issues to fix                         : "
             f"{sum(vc.get(v,0) for v in OURS)}")
    progress(f"      - scale_error        (off by ~1000x)           : {vc.get('scale_error',0)}")
    progress(f"      - ours_derived_diff  (our derived # disagrees)  : {vc.get('ours_derived_diff',0)}")
    progress(f"      - ours_fuzzy_suspect (low-confidence mapping)   : {vc.get('ours_fuzzy_suspect',0)}")
    progress(f"    coverage gaps (softer -- often template diffs)    :")
    progress(f"      - ours_zerofill_gap  (we 0-fill, GF has a value): {vc.get('ours_zerofill_gap',0)}")
    progress(f"    unresolved (PDF-backed, genuine diff - needs eyes): {vc.get('unresolved',0)}")
    progress("    " + "." * 48)
    progress(f"    gf_only  (GF reports it, we have nothing)         : {vc.get('gf_only',0)}")
    progress(f"    gf_absent(we report it, GF omits it)             : {vc.get('gf_absent',0)}")

    metrics = _per_metric_rates(rows)
    if metrics:
        progress("  " + "-" * 52)
        progress("  Lowest per-metric match rates (n>=3):")
        shown = [m for m in metrics if m["n"] >= 3][:12]
        for m in shown:
            progress(f"    {m['key']:<38} {m['rate']:5.0f}%  ({m['match']}/{m['n']})")

    # Only rows whose VERDICT says it's our genuine issue -- a raw "sign_flip"/
    # "mismatch" STATUS often resolves to both_ok_sign/definitional (correct on
    # both sides), so filtering + labeling by verdict avoids implying a bug that
    # isn't one.
    _ISSUE_V = {"scale_error", "ours_derived_diff", "ours_fuzzy_suspect"}
    worst = sorted([r for r in comparable if r.get("verdict") in _ISSUE_V
                    and r["abs_pct_diff"] is not None],
                   key=lambda r: -r["abs_pct_diff"])[:12]
    if worst:
        progress("  " + "-" * 52)
        progress("  Largest discrepancies (verdict = genuinely our issue):")
        for r in worst:
            progress(f"    {r['ticker']:<7} {r['fiscal_year']} {r['canonical_key']:<30} "
                     f"ours={r['pipeline_value']}  gf={r['gurufocus_value']}  "
                     f"({r['abs_pct_diff']:.0f}%, {r['verdict']})")

    per_co = per_company_stats(rows, company_stats)
    if per_co:
        progress("  " + "-" * 52)
        progress("  PER-STOCK accuracy (worst first, covered companies only):")
        progress(f"    {'ticker':<8} {'match%':>7} {'cells':>6} {'cur':>5} {'ours-issue':>10}  worst metric (verdict)")
        for c in per_co[:20]:
            worst_m = c["worst"]
            wtxt = (f"{worst_m['canonical_key']} ours={worst_m['pipeline_value']} "
                    f"gf={worst_m['gurufocus_value']} ({worst_m['verdict']})") if worst_m else "-"
            progress(f"    {c['ticker']:<8} {c['match_pct']:>6.1f}% {c['cells']:>6} "
                     f"{(c['currency'] or 'CAD')[:5]:>5} {c['our_issues']:>10}  {wtxt}")

    # mapping-coverage: mapped labels that NEVER matched any response -> likely a
    # wrong label string in GF_FIELD_MAP (drives step-2 pinning).
    mapped_labels = {f"{sec}:{lab}" for (sec, labs, _u, _c) in GF_FIELDS for lab in labs}
    never = sorted(mapped_labels - seen_labels)
    if never and covered:
        progress("  " + "-" * 52)
        progress(f"  Mapped GuruFocus labels never seen in any response ({len(never)}) "
                 "-- verify these strings against a live dump:")
        for lab in never[:20]:
            progress(f"    {lab}")
    progress("=" * 56)


def write_dashboard(rows: list[dict], company_stats: list[dict], tol_pct: float,
                    path: Path) -> None:
    comparable = [r for r in rows if r["status"] in ("match", "mismatch", "sign_flip", "currency_mismatch")]
    metrics = _per_metric_rates(rows)
    attempted = len(company_stats)
    covered = sum(1 for s in company_stats if s["gf_covered"])
    matched = sum(1 for r in comparable if r["status"] == "match")
    overall = (matched / len(comparable) * 100.0) if comparable else 0.0
    vc = Counter(r.get("verdict", "") for r in rows)
    # table: genuine "our issue" rows first (scale/derived/fuzzy), then the rest,
    # each sorted by materiality (abs %). Keeps the actionable ones on top.
    _ISSUE = {"scale_error", "ours_derived_diff", "ours_fuzzy_suspect"}
    disc = [r for r in rows if r["status"] != "match" and r["abs_pct_diff"] is not None]
    disc.sort(key=lambda r: (r.get("verdict") not in _ISSUE, -r["abs_pct_diff"]))
    disc = disc[:500]

    per_co = per_company_stats(rows, company_stats)
    co_rows = [{"ticker": c["ticker"], "exchange": c["exchange"], "cells": c["cells"],
               "match_pct": c["match_pct"], "our_issues": c["our_issues"],
               "zerofill_gaps": c["zerofill_gaps"], "years": c["years"],
               "currency": c["currency"] or "CAD",
               "worst_key": (c["worst"] or {}).get("canonical_key", ""),
               "worst_ours": (c["worst"] or {}).get("pipeline_value"),
               "worst_gf": (c["worst"] or {}).get("gurufocus_value"),
               "worst_pct": (c["worst"] or {}).get("abs_pct_diff")}
              for c in per_co]

    core = [r for r in comparable if r["canonical_key"] in CORE_KEYS]
    core_m = sum(1 for r in core if r["status"] == "match")
    true_raw = [r for r in comparable if r.get("provenance") in ("mapped", "from_notes")]
    tr_v = sum(1 for r in true_raw if r["status"] == "match"
              or r.get("verdict") in _VERIFIED_VERDICTS)
    payload = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "tol": tol_pct,
        "kpis": {"attempted": attempted, "covered": covered,
                 "cells": len(comparable), "matched": matched,
                 "overall": round(overall, 1),
                 "core": round(core_m / len(core) * 100, 1) if core else 0.0,
                 "trueRawN": len(true_raw),
                 "trueRawVerified": round(tr_v / len(true_raw) * 100, 1) if true_raw else 0.0},
        "verdicts": dict(vc),
        "metrics": [{"key": m["key"], "rate": round(m["rate"], 1), "n": m["n"]}
                    for m in metrics],
        "companies": co_rows,
        "rows": disc,
    }
    html = _DASHBOARD_HTML.replace("/*DATA*/{}", json.dumps(payload))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GuruFocus comparison</title>
<style>
 :root{--bg:#0f1115;--card:#1a1d24;--fg:#e6e8ec;--mut:#9aa0aa;--line:#2a2e37;
   --good:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff;}
 @media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--card:#fff;--fg:#1a1d24;
   --mut:#5a616b;--line:#e2e5ea;}}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
   font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:24px}
 h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);margin-bottom:20px}
 .kpis{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:24px}
 .kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;
   padding:14px 18px;min-width:130px}
 .kpi .v{font-size:26px;font-weight:600}.kpi .l{color:var(--mut);font-size:12px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
   padding:18px;margin-bottom:24px}
 h2{font-size:15px;margin:0 0 14px}
 .bar{display:flex;align-items:center;gap:10px;margin:6px 0}
 .bar .name{width:260px;font-size:12px;color:var(--mut);white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis}
 .track{flex:1;height:14px;background:var(--line);border-radius:7px;overflow:hidden}
 .fill{height:100%;border-radius:7px}
 .bar .pct{width:74px;text-align:right;font-variant-numeric:tabular-nums;font-size:12px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);
   white-space:nowrap}
 th{cursor:pointer;color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}
 td.num{text-align:right;font-variant-numeric:tabular-nums}
 .tag{font-size:11px;padding:2px 8px;border-radius:20px;white-space:nowrap}
 .s-mismatch{background:rgba(248,81,73,.15);color:var(--bad)}
 .s-sign_flip{background:rgba(210,153,34,.15);color:var(--warn)}
 .s-missing_gf,.s-missing_pipeline{background:rgba(88,166,255,.12);color:var(--accent)}
 .s-currency_mismatch{background:rgba(210,153,34,.15);color:var(--warn)}
 /* verdict tags: green = we're right / convention, red = our genuine issue, blue = gap */
 .v-both_ok_sign,.v-pdf_backs_us,.v-definitional{background:rgba(63,185,80,.15);color:var(--good)}
 .v-scale_error,.v-ours_derived_diff,.v-ours_fuzzy_suspect{background:rgba(248,81,73,.15);color:var(--bad)}
 .v-ours_zerofill_gap,.v-gf_only,.v-gf_absent,.v-unresolved{background:rgba(88,166,255,.12);color:var(--accent)}
 .vbars{display:flex;flex-wrap:wrap;gap:8px}
 .vchip{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
 .vchip b{font-size:18px}.vchip span{color:var(--mut);font-size:12px;margin-left:6px}
 .pdfl{color:var(--mut);font-size:11px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .wrap{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:8px}
 input{background:var(--card);border:1px solid var(--line);color:var(--fg);
   border-radius:8px;padding:8px 12px;width:280px;margin-bottom:12px}
</style></head><body>
<h1>GuruFocus comparison</h1>
<div class="sub" id="sub"></div>
<div class="kpis" id="kpis"></div>
<div class="card"><h2>Adjudication vs the PDF — who is correct on disagreements</h2>
  <div class="vbars" id="vbars"></div></div>
<div class="card"><h2>Per-stock accuracy (worst first) — what was different for each company</h2>
  <input id="qco" placeholder="filter by ticker…">
  <div class="wrap"><table id="tblco"><thead><tr>
   <th data-k="ticker">Ticker</th><th data-k="exchange">Exch</th>
   <th data-k="currency">Cur</th>
   <th data-k="years" class="num">Years</th><th data-k="cells" class="num">Cells</th>
   <th data-k="match_pct" class="num">Match %</th>
   <th data-k="our_issues" class="num">Our issues</th>
   <th data-k="zerofill_gaps" class="num">0-fill gaps</th>
   <th>Worst issue for this stock</th></tr></thead><tbody></tbody></table></div>
</div>
<div class="card"><h2>Per-metric match rate (worst first)</h2><div id="bars"></div></div>
<div class="card"><h2>Discrepancies (our genuine issues first, PDF evidence attached)</h2>
  <input id="q" placeholder="filter by ticker / metric / status / verdict…">
  <div class="wrap"><table id="tbl"><thead><tr>
   <th data-k="ticker">Ticker</th><th data-k="fiscal_year">Year</th>
   <th data-k="canonical_key">Metric</th>
   <th data-k="pipeline_value" class="num">Ours</th>
   <th data-k="gurufocus_value" class="num">GuruFocus</th>
   <th data-k="abs_pct_diff" class="num">% diff</th>
   <th data-k="verdict">Verdict</th>
   <th data-k="pdf_raw_label">PDF line (raw)</th></tr></thead><tbody></tbody></table></div>
</div>
<script>
const D = /*DATA*/{};
const col = r => r>=95?'var(--good)':r>=80?'var(--warn)':'var(--bad)';
document.getElementById('sub').textContent =
  `generated ${D.generated} · tolerance ±${D.tol}% · ${D.kpis.covered}/${D.kpis.attempted} companies had GuruFocus data`;
const K=D.kpis, kd=[
  ['Accuracy of what WE extracted',(K.trueRawVerified||0)+'%'],
  ['Core metrics match',(K.core||0)+'%'],['Overall match',K.overall+'%'],
  ['Comparable cells',K.cells],['Matched',K.matched],['Companies covered',K.covered+'/'+K.attempted]];
document.getElementById('kpis').innerHTML = kd.map(([l,v])=>
  `<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');
document.getElementById('bars').innerHTML = D.metrics.map(m=>
  `<div class="bar"><div class="name" title="${m.key}">${m.key}</div>
   <div class="track"><div class="fill" style="width:${m.rate}%;background:${col(m.rate)}"></div></div>
   <div class="pct">${m.rate}% <span style="color:var(--mut)">(${m.n})</span></div></div>`).join('')
  || '<div style="color:var(--mut)">No comparable cells.</div>';
// verdict chips, in a meaningful order with plain-English labels
const VLBL={both_ok_sign:"GF sign convention",pdf_backs_us:"PDF backs us",definitional:"definitional",
  scale_error:"scale error (ours)",ours_derived_diff:"our derived # off",ours_fuzzy_suspect:"fuzzy map (check)",
  ours_zerofill_gap:"we 0-fill, GF has it",unresolved:"unresolved",gf_only:"GF only",gf_absent:"we only"};
const VORD=["pdf_backs_us","both_ok_sign","definitional","scale_error","ours_derived_diff",
  "ours_fuzzy_suspect","ours_zerofill_gap","gf_only","gf_absent"];
document.getElementById('vbars').innerHTML = VORD.filter(v=>D.verdicts[v]).map(v=>
  `<div class="vchip"><b><span class="tag v-${v}" style="padding:0 6px">${D.verdicts[v]}</span></b>`
  +`<span>${VLBL[v]||v}</span></div>`).join('');
const fmt=v=>v===null||v===undefined?'–':(typeof v==='number'?v.toLocaleString(undefined,{maximumFractionDigits:3}):v);
function makeTable(tableId, allRows, renderRow, filterFn){
  // allRows arrives PRE-SORTED (from Python) in the intended default order --
  // render it as-is on init; sortBy is only invoked by a user header click.
  const tb=document.querySelector(`#${tableId} tbody`);
  let rows=allRows.slice(), sortK=null, desc=true;
  function render(){ tb.innerHTML = rows.map(renderRow).join(''); }
  function sortBy(k){ desc = (k===sortK) ? !desc : true; sortK=k;
    rows.sort((a,b)=>{let x=a[k],y=b[k]; if(x===null||x===undefined)return 1; if(y===null||y===undefined)return -1;
      if(typeof x==='string')return desc?y.localeCompare(x):x.localeCompare(y);
      return desc?y-x:x-y;}); render(); }
  document.querySelectorAll(`#${tableId} th`).forEach(th=>th.onclick=()=>sortBy(th.dataset.k));
  render();
  return {sortBy, render, setFilter(term){ rows = term ? allRows.filter(r=>filterFn(r,term)) : allRows.slice();
    if(sortK) sortBy(sortK); else render(); }};
}
const discTbl = makeTable('tbl', D.rows, r=>`<tr>
    <td>${r.ticker}</td><td>${r.fiscal_year}</td><td>${r.canonical_key}</td>
    <td class="num">${fmt(r.pipeline_value)}</td><td class="num">${fmt(r.gurufocus_value)}</td>
    <td class="num">${r.abs_pct_diff===null?'–':r.abs_pct_diff.toFixed(1)+'%'}</td>
    <td><span class="tag v-${r.verdict}">${VLBL[r.verdict]||r.verdict}</span></td>
    <td class="pdfl" title="${(r.pdf_raw_label||'').replace(/"/g,'&quot;')}">${r.pdf_raw_label||'–'}</td></tr>`,
  (r,s)=>(r.ticker+r.canonical_key+r.status+r.verdict+r.gf_label).toLowerCase().includes(s));
document.getElementById('q').oninput=e=>discTbl.setFilter(e.target.value.toLowerCase());

const coColor = p => p>=80?'var(--good)':p>=50?'var(--warn)':'var(--bad)';
const coTbl = makeTable('tblco', D.companies, c=>{
    const worst = c.worst_key
      ? `${c.worst_key} ours=${fmt(c.worst_ours)} gf=${fmt(c.worst_gf)} `
        + `(${c.worst_pct===null||c.worst_pct===undefined?'–':c.worst_pct.toFixed(0)+'%'})`
      : '<span style="color:var(--good)">none</span>';
    return `<tr>
      <td><b>${c.ticker}</b></td><td style="color:var(--mut)">${c.exchange}</td>
      <td style="color:var(--mut)">${c.currency||'CAD'}</td>
      <td class="num">${c.years}</td><td class="num">${c.cells}</td>
      <td class="num" style="color:${coColor(c.match_pct)};font-weight:600">${c.match_pct}%</td>
      <td class="num">${c.our_issues}</td><td class="num">${c.zerofill_gaps}</td>
      <td class="pdfl" style="max-width:420px" title="${worst.replace(/"/g,'&quot;')}">${worst}</td></tr>`;
  }, (c,s)=>c.ticker.toLowerCase().includes(s));
document.getElementById('qco').oninput=e=>coTbl.setFilter(e.target.value.toLowerCase());
</script></body></html>"""


# ------------------------------------------------------------------------------ CLI
def main() -> None:
    ap = argparse.ArgumentParser(description="Compare pipeline financials vs the GuruFocus API.")
    ap.add_argument("--tickers", help="Comma-separated ticker allow-list (exact DB tickers).")
    ap.add_argument("--exchange", help="Compare every company on this exchange (TSX/TSXV/XCNQ/NEOE).")
    ap.add_argument("--sample", type=int, default=40,
                    help="Default-mode sample size (TSX-weighted). Ignored with --tickers/--exchange.")
    ap.add_argument("--limit", type=int, default=None, help="Cap the final company count.")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOL_PCT,
                    help=f"Match threshold, %% (default {DEFAULT_TOL_PCT}).")
    ap.add_argument("--from-cache", action="store_true",
                    help="Never call the API; only use output/gurufocus_cache/. Cache misses are skipped.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="pdf_financials.db path.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "output")
    ap.add_argument("--dump", metavar="EXCH:SYM", default=None,
                    help="Fetch one symbol, print its parsed sections+labels to stdout, and exit "
                         "(for pinning GF_FIELD_MAP labels).")
    args = ap.parse_args()

    cfg = _load_config(args.config)
    gcfg = cfg.get("gurufocus", {}) or {}
    token = _resolve_token(cfg)
    cache_dir = Path(gcfg.get("cache_dir") or ROOT / "output" / "gurufocus_cache")
    timeout = float(gcfg.get("timeout_seconds", 20))

    if not args.from_cache and not token and not args.dump:
        print("\nERROR: no GuruFocus API token. Set gurufocus.api_token in your config "
              "or export GURUFOCUS_API_TOKEN. (Or run with --from-cache to replay "
              "cached responses only.)\n", file=sys.stderr)
        sys.exit(1)

    # one-off dump helper for label pinning
    if args.dump:
        exch, _, sym = args.dump.partition(":")
        try:
            payload = gf.fetch_fundamentals(exch, sym, token, cache_dir=cache_dir,
                                            timeout=timeout, from_cache=args.from_cache,
                                            progress=print)
        except gf.GuruFocusError as exc:
            print(f"\nERROR: {exc}\n", file=sys.stderr)
            sys.exit(1)
        if not payload:
            print("no data (cache miss / no coverage)")
            return
        if gf.is_no_coverage(payload):
            print(f"no GuruFocus coverage for {gf.gf_symbol(exch, sym)} "
                  f"(status {payload.get('_status')})")
            return
        by_year, labels, cur = parse_gf(payload)
        print(f"currency={cur}  years={sorted(by_year)}")
        for lab in sorted(labels):
            print(" ", lab)
        return

    if not args.db.exists():
        print(f"\nERROR: DB not found: {args.db}\n", file=sys.stderr)
        sys.exit(1)

    conn = _connect(args.db)
    tickers = ([t.strip() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else None)
    companies = select_companies(conn, tickers=tickers, exchange=args.exchange,
                                 sample=args.sample, limit=args.limit, progress=print)
    if not companies:
        print("No companies selected.")
        return

    print(f"Comparing {len(companies)} compan(ies)  |  DB: {args.db}  |  "
          f"{'CACHE-ONLY' if args.from_cache else 'live API'}  |  tol +/-{args.tolerance:g}%")

    all_rows: list[dict] = []
    all_stats: list[dict] = []
    seen_labels: set[str] = set()
    t0 = time.time()
    client = None if args.from_cache else httpx.Client(timeout=timeout)
    try:
        for i, (ticker, exchange, currency) in enumerate(companies, 1):
            try:
                payload = gf.fetch_fundamentals(exchange, ticker, token, cache_dir=cache_dir,
                                                timeout=timeout, from_cache=args.from_cache,
                                                client=client, progress=print)
            except gf.GuruFocusError as exc:
                print(f"  [{i}/{len(companies)}] {ticker}: {exc}")
                all_stats.append({"ticker": ticker, "gf_covered": False,
                                  "years_pipeline": [], "years_gf": [], "years_overlap": [],
                                  "cells": 0, "match": 0})
                continue
            pipe_vals = load_pipeline_values(conn, ticker)
            pdf_evidence = load_pdf_evidence(conn, ticker)
            provenance = load_provenance(conn, ticker)
            rows, stats = compare_company(ticker, exchange, currency, pipe_vals,
                                          payload, args.tolerance, seen_labels,
                                          pdf_evidence, provenance)
            all_rows.extend(rows)
            all_stats.append(stats)
            tag = "no-coverage" if not stats["gf_covered"] else (
                f"{stats['match']}/{stats['cells']} match, "
                f"yrs {len(stats['years_overlap'])}")
            print(f"  [{i}/{len(companies)}] {gf.gf_symbol(exchange, ticker):<14} {tag}")
    finally:
        if client is not None:
            client.close()
        conn.close()

    out_dir = Path(args.out_dir)
    csv_path = out_dir / "gurufocus_compare.csv"
    html_path = out_dir / "gurufocus_compare_dashboard.html"
    issues_path = out_dir / "gurufocus_issues.csv"
    write_csv(all_rows, csv_path)
    write_dashboard(all_rows, all_stats, args.tolerance, html_path)
    # Focused, actionable list: only the verdicts that are (or may be) OUR error --
    # scale bugs, our derived figures that disagree, and low-confidence fuzzy maps --
    # sorted by materiality (largest % gap first) so the worst are fixed first.
    ISSUE_VERDICTS = {"scale_error", "ours_derived_diff", "ours_fuzzy_suspect"}
    issues = sorted((r for r in all_rows if r.get("verdict") in ISSUE_VERDICTS
                     and r["abs_pct_diff"] is not None),
                    key=lambda r: -r["abs_pct_diff"])
    write_csv(issues, issues_path)
    print_summary(all_rows, all_stats, seen_labels, args.tolerance, print)
    print(f"\n  full CSV      : {csv_path}")
    print(f"  ISSUES CSV    : {issues_path}  ({len(issues)} genuine our-issues to review)")
    print(f"  dashboard     : {html_path}")
    print(f"  elapsed       : {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
