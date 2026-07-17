"""run.py --step 3 --method rules: DETERMINISTIC (no-LLM) extraction of annual-
report statement TEXT into the canonical financials schema.

Same output contract as llm_extract.run_llm_extraction (writes companies /
company_years / statement_lines tagged source='cse_pdf_extract', plus
pdf_llm_status for resume), so company_xlsx_export.py renders rule-extracted
companies identically to QuoteMedia ones. No model, no GPU, no network.

Pipeline per statement:
  parse_statement(text)  -> columns (years), unit scale, rows of (label, values)
  match_label(label)     -> canonical GuruFocus/QuoteMedia key (alias + fuzzy)
  validate               -> parse-integrity, mapping-integrity, and cross-line
                            ACCOUNTING IDENTITIES (assets = liab + equity, cash
                            reconciliation, ...) incl. x1000 scale self-repair
Kept values are written even when a check fails ("keep + flag"); every flag is
recorded in pdf_llm_status.reason and the printed report. Shell/CPC issuers and
SEC/EDGAR cross-listed companies are intentionally out of scope.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import time
from datetime import date
from pathlib import Path

from financials_pipeline import FinancialsStore, _coerce_numeric
from llm_extract import (
    DEFAULT_VOCAB, FILINGS_SCHEMA, NO_SCALE_KEYS, STATEMENTS, SYNONYM_GROUPS,
    _extract_number_tokens, _load_vocab, _rows_to_extract, _section_text,
    _to_number, _verify_value,
)

CURRENT_YEAR = date.today().year
FUZZY_THRESHOLD = 0.90      # min difflib ratio to accept a non-exact label match
FUZZY_AMBIGUOUS_DELTA = 0.03  # two keys within this of each other -> ambiguous, drop
IDENTITY_TOL = 0.01         # 1% tolerance on accounting-identity checks
MIN_ROWS_OK = 4             # fewer mapped lines than this -> 'sparse' flag

# ---------------------------------------------------------------------------
# PARSER
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"\b(?:19|20)\d\d\b")
# a value cell: optional $/sign/paren, digits with separators, optional decimals.
# NOTE: a trailing '%' is deliberately NOT allowed -- percent columns ("% change",
# margin) are not year-value columns and are skipped in _split_line.
_VALUE_RE = re.compile(r"^\(?\$?[-+]?\d[\d,]*\.?\d*\)?$")
_NULL_TOKENS = {"-", "–", "—", "�", "n.a.", "n/a", "nil", "——"}
# \s* (not a literal space) between words: some text layers keep phrases glued
# even after the tighter-x_tolerance re-extract ("(MillionsofCanadiandollars)"
# in RBC's pre-2019 annual reports), and the units note must still be found.
#
# BUG FIXED (confirmed on Apple's 10-K, a 1000x-scale error across every
# dollar figure): large US filers commonly state
# "(In millions, except number of shares, which are reflected in thousands,
# and par value)" -- ONE sentence naming BOTH scales, where "thousands"
# describes a SHARE-COUNT exception, not the dollar figures. The old
# thousands-checked-first order matched that caveat and returned 1000.0
# before ever looking for "millions", silently under-scaling the whole
# statement. millions is checked FIRST now: when a units note mentions both
# (this common "in millions, except ... thousands" phrasing), the primary
# scale wins; a genuine thousands-only note (no "millions" anywhere) still
# falls through to the second pattern exactly as before.
_SCALE_PATTERNS = [
    (re.compile(r"in\s*millions|millions\s*of", re.I), 1_000_000.0),
    (re.compile(r"in\s*thousands|thousands\s*of|\(000s?\)|\$000s?", re.I), 1000.0),
]
_CURRENCY_PATTERNS = [
    (re.compile(r"canadian\s*dollar|\bcad\b|\bcdn\b", re.I), "CAD"),
    (re.compile(r"u\.?s\.?\s*dollar|\busd\b", re.I), "USD"),
]


def _detect_scale(text: str) -> tuple[float, bool]:
    """Unit multiplier from the statement's own note. (scale, found_explicitly)."""
    for rx, mult in _SCALE_PATTERNS:
        if rx.search(text):
            return mult, True
    return 1.0, False


def _detect_currency(text: str) -> str | None:
    for rx, cur in _CURRENCY_PATTERNS:
        if rx.search(text):
            return cur
    return None


_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
           "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
           "december": 12}
_DATE_RE = re.compile(r"(january|february|march|april|may|june|july|august|"
                      r"september|october|november|december)\s+(\d{1,2})", re.I)
# Cover/first-page fiscal-year phrases that DO capture the year (unlike
# _DATE_RE, which is month+day only). Used to establish a PDF's own headline
# fiscal year from its content -- "for the year(s) ended March 31, 2025",
# "fiscal year ended December 31, 2024", "annual report 2025",
# "years ended ... 2025 and 2024". Bilingual (English + French).
_COVER_YEAR_RE = re.compile(
    r"(?:for the |fiscal )?years?\s+ended[^.\n]{0,40}?((?:19|20)\d\d)"
    r"|exercices?\s+(?:clos|termin[ée]s?)[^.\n]{0,40}?((?:19|20)\d\d)"
    r"|annual report\s+((?:19|20)\d\d)"
    r"|rapport annuel\s+((?:19|20)\d\d)", re.I)


def detect_cover_year(text: str, max_lines: int = 60) -> int | None:
    """Headline fiscal year read from a cover/first-page phrase that names the
    year ('for the year ended March 31, 2025' -> 2025). Scans the first
    `max_lines` lines only. None if no such phrase. Cross-check / fallback for
    _detect_columns (which reads the statement comparative-column header)."""
    head = "\n".join(text.splitlines()[:max_lines])
    best: int | None = None
    for m in _COVER_YEAR_RE.finditer(head):
        y = next((int(g) for g in m.groups() if g), None)
        if y is not None and 1990 <= y <= CURRENT_YEAR + 1:
            best = y if best is None else max(best, y)
    return best


def _detect_period_ends(text: str, col_years: list[int]) -> dict[int, str]:
    """Map each column year to an ISO period-end date (YYYY-MM-DD) from the
    statement's date header ('years ended March 31, 2025 and 2024'). The month/day
    are shared across the comparative columns; the year differs. Empty if no month
    is found (period_end then stays NULL, as before)."""
    head = "\n".join(text.splitlines()[:25])
    m = _DATE_RE.search(head)
    if not m:
        return {}
    mo = _MONTHS[m.group(1).lower()]
    day = min(int(m.group(2)), 31)
    return {y: f"{y:04d}-{mo:02d}-{day:02d}" for y in col_years}


def _detect_columns(text: str) -> list[int]:
    """The comparative-period years, left to right. Uses the header line with the
    most distinct plausible years (e.g. '2025 2024' or 'years ended ... 2025 and
    2024'); scans only the header region so a stray in-body year isn't mistaken
    for a column."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    best: list[int] = []
    for ln in lines[:25]:
        yrs = [int(y) for y in _YEAR_RE.findall(ln) if 1990 <= int(y) <= CURRENT_YEAR + 1]
        # keep left-to-right order, drop dupes
        seen: list[int] = []
        for y in yrs:
            if y not in seen:
                seen.append(y)
        if len(seen) > len(best):
            best = seen
    return best


def _is_null_cell(tok: str) -> bool:
    return tok.lower() in _NULL_TOKENS


def _split_line(line: str, n_cols: int) -> tuple[str, list] | None:
    """Split one line into (label, [n_cols raw value strings]). Values are the
    trailing n_cols numeric/null cells (scanning from the RIGHT, skipping bare
    '$' tokens); the label is everything before them. Returns None if the tail
    doesn't yield n_cols value cells (i.e. not a data row)."""
    toks = line.split()
    if len(toks) < n_cols:
        return None
    values: list = []
    i = len(toks) - 1
    while i >= 0 and len(values) < n_cols:
        t = toks[i]
        if t == "$" or t.endswith("%"):
            # bare '$' column marker, or a trailing '% change'/margin column --
            # not a year value; skip it.
            i -= 1
            continue
        if _is_null_cell(t):
            values.append(None)
            i -= 1
            continue
        if _VALUE_RE.match(t):
            values.append(t)
            i -= 1
            continue
        break  # a non-value token -> the values run has ended
    if len(values) != n_cols:
        return None
    values.reverse()
    label = " ".join(toks[: i + 1]).strip()
    return label, values


def parse_statement(text: str, statement_type: str) -> dict:
    """TEXT -> structured parse. Never raises. Returns:
      col_years, n_cols, scale, scale_found, currency,
      rows: [(label, [float|None per column])], flags: [str]
    Values are AS PRINTED (pre-scale); scaling happens after canonical mapping."""
    flags: list[str] = []
    scale, scale_found = _detect_scale(text)   # scale_assumed flag owned by _resolve_scale
    currency = _detect_currency(text)
    col_years = _detect_columns(text)
    n_cols = len(col_years)
    if n_cols == 0:
        flags.append("no_columns")
        return dict(col_years=[], n_cols=0, scale=scale, scale_found=scale_found,
                    currency=currency, rows=[], flags=flags, period_ends={})
    period_ends = _detect_period_ends(text, col_years)
    # column-year sanity
    if not all(1990 <= y <= CURRENT_YEAR + 1 for y in col_years):
        flags.append("year_out_of_range")
    if n_cols >= 2 and not (col_years[0] > col_years[1]):
        flags.append("years_not_descending")

    rows: list[tuple[str, list]] = []
    pending_prefix = ""  # a preceding no-value line (possible wrapped label)
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        parsed = _split_line(ln, n_cols)
        if parsed is None:
            # no value cells -> a header / section label / wrapped-label start
            pending_prefix = ln
            continue
        label, values = parsed
        nums = [_to_number(v) for v in values]
        # skip the column-header line itself (its "values" ARE the column years)
        if all(n is not None and int(n) in col_years for n in nums):
            pending_prefix = ln
            continue
        # attach a wrapped prefix when this row's own label is empty or a
        # lowercase continuation ("for the year ...")
        if pending_prefix and (not label or label[:1].islower()):
            label = (pending_prefix + " " + label).strip()
        pending_prefix = ""
        if not label:
            continue  # subtotal-only line with no attachable label
        rows.append((label, nums))
    return dict(col_years=col_years, n_cols=n_cols, scale=scale,
                scale_found=scale_found, currency=currency, rows=rows, flags=flags,
                period_ends=period_ends)


# ---------------------------------------------------------------------------
# MATCHER
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TRAIL_DOLLAR_RE = re.compile(r"\s*\$\s*$")
# ONE trailing note-reference token: digits ("5", "10,11") or PARENTHESIZED roman
# ("(iii)"). Parenthesized-only for roman avoids eating a bare trailing "c"/"i".
_NOTE_TAIL_RE = re.compile(r"\s+(?:\d[\d,]*|\([ivxlcdm]+\))\s*$", re.I)
_PUNCT_RE = re.compile(r"[^\w\s&]")


def _humanize(key: str) -> str:
    return _CAMEL_RE.sub(" ", key).replace("_", " ").lower().strip()


# Embedded note references anywhere in a label: "Goodwill (Note 11)",
# "Provision for credit losses (Notes 4 and 5)". These defeated matching when
# only TRAILING refs were stripped.
_NOTE_PAREN_RE = re.compile(r"\(\s*notes?\b[^)]*\)", re.I)


def _normalize_label(label: str) -> str:
    s = label.lower().strip()
    s = _NOTE_PAREN_RE.sub(" ", s)        # "(note 11)" / "(notes 4 and 5)" anywhere
    # peel trailing '$' column markers and note-reference tokens, repeatedly, so
    # "revenues 5 $" and "gain on impairment 6 (iii)" both reduce to the concept.
    prev = None
    while prev != s:
        prev = s
        s = _TRAIL_DOLLAR_RE.sub("", s).strip()
        s = _NOTE_TAIL_RE.sub("", s).strip()
    s = _PUNCT_RE.sub(" ", s)             # keep word chars, whitespace, '&'
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Hand-curated raw-label synonyms -> canonical key. Focused on the keys the
# GuruFocus template actually renders + common industry (bank) terms. The
# humanized canonical key is ALSO an alias, so fuzzy covers the long tail.
CURATED_ALIASES: dict[str, list[str]] = {
    # --- income statement ---
    "TotalRevenue": ["revenue", "revenues", "total revenue", "total revenues",
                     "net revenue", "net revenues", "sales", "net sales", "total sales",
                     "total income", "revenue from operations"],
    "OperatingRevenue": ["operating revenue", "operating revenues"],
    "CostOfRevenue": ["cost of revenue", "cost of sales", "cost of goods sold",
                      "cost of services", "cost of products sold"],
    "GrossProfit": ["gross profit", "gross margin", "gross profit (loss)"],
    "OperatingIncome": ["operating income", "income from operations", "operating profit",
                        "operating earnings", "operating income (loss)",
                        "income (loss) from operations"],
    "OperatingExpense": ["operating expenses", "total operating expenses",
                         "total expenses", "operating costs and expenses"],
    "SellingGeneralAndAdministration": ["selling general and administrative",
                                        "selling general and administrative expenses",
                                        "selling general administrative"],
    "GeneralAndAdministrativeExpense": ["general and administrative",
                                        "general and administrative expenses",
                                        "general and administration", "administrative expenses"],
    "SellingAndMarketingExpense": ["selling and marketing", "sales and marketing",
                                   "selling and marketing expenses"],
    "ResearchAndDevelopment": ["research and development", "research and development expenses"],
    "DepreciationAndAmortization": ["depreciation and amortization",
                                    "amortization and depreciation", "depreciation & amortization"],
    "DepreciationAmortizationDepletion": ["depreciation depletion and amortization"],
    "InterestExpenseOperating": ["interest expense", "interest expenses"],
    "InterestIncomeNonOperating": ["interest income", "interest and dividend income"],
    "PretaxIncome": ["income before taxes", "income before income taxes", "pretax income",
                     "earnings before taxes", "loss before income taxes",
                     "income (loss) before income taxes", "income before tax",
                     "net income before taxes", "income (loss) before taxes"],
    "TaxProvision": ["income tax expense", "provision for income taxes", "income taxes",
                     "tax provision", "income tax expense (recovery)",
                     "provision for (recovery of) income taxes", "income tax provision"],
    # NOTE: standalone "comprehensive income"/"total comprehensive income" are
    # deliberately NOT aliases -- comprehensive income includes OCI and is a
    # DIFFERENT figure from net income. Only the COMBINED single-line phrasings
    # (small issuers with no OCI) map here.
    "NetIncome": ["net income", "net loss", "net income loss", "net income (loss)",
                  "net earnings", "profit for the year", "loss for the year",
                  "net income (loss) for the year",
                  "net income (loss) and comprehensive income (loss)",
                  "net income (loss) and comprehensive income (loss) for the year",
                  "loss and comprehensive loss for the year",
                  "net income (loss) and comprehensive income"],
    "EBITDA": ["ebitda", "adjusted ebitda"],
    "EBIT": ["ebit"],
    "BasicEPS": ["basic earnings per share", "basic eps", "earnings per share basic",
                 "net income per share basic", "basic income (loss) per share",
                 "income (loss) per share basic", "basic and diluted",
                 "earnings (loss) per share basic", "basic and diluted income (loss) per share",
                 "basic and diluted loss per share"],
    "DilutedEPS": ["diluted earnings per share", "diluted eps", "earnings per share diluted",
                   "net income per share diluted", "diluted income (loss) per share"],
    "BasicAverageShares": ["weighted average number of common shares",
                           "weighted average shares outstanding basic",
                           "weighted average number of shares outstanding",
                           "basic weighted average shares"],
    "DilutedAverageShares": ["diluted weighted average shares",
                             "weighted average shares outstanding diluted"],
    # bank-specific (present now that vocab is full-coverage)
    "InterestIncomeFromLoans": ["interest income loans", "interest on loans"],
    "NetInterestIncome": ["net interest income", "net interest income (loss)"],
    # NOTE: "other income" deliberately NOT here -- on non-bank statements it
    # means OtherIncomeExpense, and NonInterestIncome is bank-gated (the alias
    # here would claim the label and then be nulled by the non-bank guard).
    "NonInterestIncome": ["non interest income"],
    "NonInterestExpense": ["non interest expense", "non interest expenses"],
    "CreditLossesProvision": ["provision for credit losses", "provision for loan losses"],
    # --- balance sheet ---
    "TotalAssets": ["total assets"],
    "CurrentAssets": ["total current assets", "current assets"],
    "TotalNonCurrentAssets": ["total non current assets", "non current assets",
                              "total long term assets", "total long-term assets"],
    "CashAndCashEquivalents": ["cash and cash equivalents", "cash and cash equivalent",
                               "cash", "cash and due from banks", "cash and bank balances"],
    "AccountsReceivable": ["accounts receivable", "trade receivables", "trade accounts receivable"],
    "Receivables": ["receivables", "trade and other receivables", "accounts and other receivables"],
    "Inventory": ["inventory", "inventories"],
    "PrepaidAssets": ["prepaid expenses", "prepaid assets", "prepaids",
                      "prepaid expenses and other", "prepaid expenses and deposits"],
    "NetPPE": ["property plant and equipment", "property and equipment",
               "property plant and equipment net", "net property plant and equipment",
               "capital assets"],
    "OtherIntangibleAssets": ["intangible assets", "other intangible assets"],
    "InvestmentsAndAdvances": ["investments", "long term investments", "long-term investments"],
    "OtherNonCurrentAssets": ["other non current assets", "other long term assets",
                              "other long-term assets"],
    "CurrentLiabilities": ["total current liabilities", "current liabilities"],
    "TotalNonCurrentLiabilities": ["total non current liabilities", "non current liabilities",
                                   "total long term liabilities", "total long-term liabilities"],
    "TotalLiabilities": ["total liabilities"],
    "AccountsPayable": ["accounts payable", "trade payables", "trade and other payables"],
    "PayablesAndAccruedExpenses": ["accounts payable and accrued liabilities",
                                   "accounts payable and accrued expenses",
                                   "trade and other payables", "accounts payable and accruals"],
    "CurrentDebt": ["current portion of long term debt", "short term debt",
                    "current portion of long-term debt"],
    "LongTermDebt": ["long term debt", "long-term debt"],
    "CurrentDeferredRevenue": ["deferred revenue", "unearned revenue"],
    # "total equity" belongs to TotalEquityGrossMinorityInterest (it INCLUDES
    # non-controlling interests); StockholdersEquity is the shareholders-only
    # figure -- listing "total equity" under both let StockholdersEquity claim
    # it first and left TotalEquityGrossMinorityInterest unmapped.
    "StockholdersEquity": ["shareholders equity", "stockholders equity",
                           "total shareholders equity", "total stockholders equity",
                           "total equity attributable to shareholders",
                           "shareholders equity (deficiency)"],
    "TotalEquityGrossMinorityInterest": ["total equity",
                                         "total equity and liabilities"],
    "MinorityInterest": ["non-controlling interests", "non-controlling interest",
                         "noncontrolling interests"],
    "RetainedEarnings": ["retained earnings", "deficit", "accumulated deficit",
                         "retained earnings (deficit)", "retained earnings deficit"],
    # "common shares"/"common stock" belong to CommonStock; "share capital" to
    # CapitalStock -- overlapping lists made dict-order decide the winner.
    "CapitalStock": ["share capital", "capital stock"],
    "CommonStock": ["common stock", "common shares"],
    "AdditionalPaidInCapital": ["additional paid in capital", "contributed surplus",
                                "additional paid-in capital"],
    "TotalLiabilitiesAndTotalEquityGrossMinorityInterest":
        ["total liabilities and equity", "total liabilities and shareholders equity",
         "total liabilities and shareholders equity (deficiency)",
         "total equity and liabilities", "total liabilities and stockholders equity"],
    # --- cash flow ---
    "OperatingCashFlow": ["cash from operating activities",
                          "net cash from operating activities",
                          "cash flows from operating activities",
                          "cash provided by operating activities",
                          "net cash provided by operating activities",
                          "net cash provided by (used in) operating activities",
                          "cash used in operating activities",
                          "net cash flows from operating activities",
                          "net cash used in operating activities"],
    "InvestingCashFlow": ["cash from investing activities",
                          "net cash used in investing activities",
                          "cash flows from investing activities",
                          "net cash provided by (used in) investing activities",
                          "cash used in investing activities",
                          "net cash flows from investing activities",
                          "net cash provided by investing activities"],
    "FinancingCashFlow": ["cash from financing activities",
                          "net cash from financing activities",
                          "cash flows from financing activities",
                          "net cash provided by (used in) financing activities",
                          "cash used in financing activities",
                          "net cash flows from financing activities",
                          "net cash provided by financing activities"],
    "ChangesInCash": ["net change in cash", "increase decrease in cash",
                      "net increase in cash", "net decrease in cash", "change in cash",
                      "net increase (decrease) in cash",
                      "net increase (decrease) in cash and cash equivalents",
                      "increase (decrease) in cash and cash equivalents",
                      "net change in cash and cash equivalents"],
    "BeginningCashPosition": ["cash beginning of year", "cash at beginning of year",
                              "cash and cash equivalents beginning of year",
                              "cash and cash equivalents beginning of period",
                              "cash beginning of period"],
    "EndCashPosition": ["cash end of year", "cash at end of year",
                        "cash and cash equivalents end of year",
                        "cash and cash equivalents end of period", "cash end of period"],
    "StockBasedCompensation": ["stock based compensation", "share based compensation",
                               "stock-based compensation", "share-based compensation",
                               "stock based compensation expense"],
    "PurchaseOfPPE": ["purchase of property plant and equipment",
                      "additions to property and equipment",
                      "acquisition of property plant and equipment",
                      "purchase of property and equipment", "additions to capital assets"],
    "EffectOfExchangeRateChanges": ["effect of exchange rate changes",
                                    "effect of foreign exchange on cash",
                                    "foreign exchange effect on cash",
                                    "effect of exchange rate changes on cash"],
    "IssuanceOfDebt": ["proceeds from long term debt", "proceeds from debt",
                       "issuance of debt", "proceeds from long-term debt"],
    "RepaymentOfDebt": ["repayment of long term debt", "repayment of debt",
                        "repayments of long-term debt"],
    "CommonStockIssuance": ["proceeds from issuance of shares", "issuance of common shares",
                            "proceeds from issuance of common shares", "issuance of shares"],
}

# Additional coverage-critical keys the GuruFocus template renders that otherwise
# relied on weak humanized-key fuzzy matching. (Aliases are per-statement scoped
# by build_alias_index, so e.g. "inventories" -> ChangeInInventory only within
# cash_flow, and -> Inventory only within balance_sheet.)
CURATED_ALIASES.update({
    # income statement
    "EBIT": ["ebit", "earnings before interest and taxes"],
    "EBITDA": ["ebitda", "adjusted ebitda", "earnings before interest taxes depreciation"],
    "DepreciationAmortizationDepletion": ["depreciation depletion and amortization",
                                          "depreciation amortization and depletion"],
    "NetIncomeContinuousOperations": ["net income from continuing operations",
                                      "income from continuing operations",
                                      "net income (loss) from continuing operations",
                                      "earnings from continuing operations"],
    "MinorityInterests": ["non-controlling interest", "noncontrolling interest",
                          "minority interest"],
    "DividendPerShare": ["dividends per share", "dividend per share",
                         "dividends declared per share"],
    # balance sheet
    "Goodwill": ["goodwill"],
    "GrossPPE": ["property plant and equipment gross", "gross property plant and equipment"],
    "AccumulatedDepreciation": ["accumulated depreciation",
                                "accumulated depreciation and amortization"],
    "PreferredStock": ["preferred shares", "preferred stock"],
    "TreasuryStock": ["treasury stock", "treasury shares"],
    "ShortTermInvestments": ["short-term investments", "short term investments",
                             "marketable securities"],
    # cash flow
    "FreeCashFlow": ["free cash flow"],
    "CapitalExpenditure": ["capital expenditures", "capital expenditure"],
    "ChangeInInventory": ["change in inventory", "decrease (increase) in inventory",
                          "inventories"],
    "ChangeInReceivables": ["change in receivables",
                            "decrease (increase) in accounts receivable",
                            "accounts receivable"],
    "ChangeInPayablesAndAccruedExpense": ["change in accounts payable",
                                          "increase (decrease) in accounts payable",
                                          "accounts payable and accrued liabilities"],
    "CashDividendsPaid": ["dividends paid", "cash dividends paid",
                          "payment of dividends"],
})

def _extend_aliases(additions: dict[str, list[str]]) -> None:
    """Append aliases to CURATED_ALIASES without clobbering existing lists."""
    for key, aliases in additions.items():
        CURATED_ALIASES.setdefault(key, [])
        for a in aliases:
            if a not in CURATED_ALIASES[key]:
                CURATED_ALIASES[key].append(a)


# Backlog aliases measured from real statements (RY bank + RAY industrial) --
# every target key verified present in the regenerated vocab. Synthetic
# "{section} total" labels come from line_items.py's subtotal capture.
_extend_aliases({
    # --- income statement (bank) ---
    "InterestIncome": ["interest and dividend income total", "interest income total",
                       "total interest income", "total interest and dividend income"],
    "InterestExpense": ["interest expense total", "total interest expense"],
    "NetInterestIncome": ["net interest income", "net interest income (loss)"],
    "NonInterestIncome": ["non-interest income total", "non interest income total",
                          "total non-interest income"],
    "NonInterestExpense": ["non-interest expense total", "non interest expense total",
                           "total non-interest expense"],
    "CreditLossesProvision": ["provision for credit losses", "provision for loan losses",
                              "provision for credit losses (recovery)"],
    "SecuritiesActivities": ["trading revenue"],
    "ServiceChargeOnDepositorAccounts": ["service charges"],
    "CreditCard": ["card service revenue"],
    "InvestmentBankingProfit": ["underwriting and other advisory fees"],
    "TrustFeesbyCommissions": ["investment management and custodial fees"],
    # NOTE: "securities brokerage commissions" deliberately NOT aliased --
    # QuoteMedia's FeesAndCommissions is an aggregate (22.9B for RY), not the
    # brokerage-commissions face line (1.7B); mapping it produced wrong data.
    "SalariesAndWages": ["human resources"],
    "NetOccupancyExpense": ["occupancy"],
    "ProfessionalExpenseAndContractServicesExpense": ["professional fees"],
    "AmortizationOfIntangibles": ["amortization of other intangibles",
                                  "amortization and impairment of other intangibles"],
    "PretaxIncome": ["income before income taxes", "income (loss) before income taxes",
                     "earnings before income taxes"],
    "BasicEPS": ["basic earnings per share in dollars", "basic in dollars",
                 "earnings per share basic in dollars"],
    "DilutedEPS": ["diluted earnings per share in dollars", "diluted in dollars"],
    "DividendPerShare": ["dividends per common share", "dividends per common share in dollars"],
    "DepreciationAmortizationDepletion": ["depreciation amortization and write-off",
                                          "depreciation amortization and write off"],
    # NOTE: "net finance expense (income)" / "finance costs" deliberately NOT
    # mapped here -- proven on RAY/Stingray to be a BROADER netted figure
    # (bundles FX, derivative fair-value swings, accretion) than GuruFocus's
    # narrower "Interest Expense" row. The real, GuruFocus-matching figure is
    # the finance-expense NOTE's "Interest expense and standby fees" +
    # "Interest expense on lease liabilities" (exact match, both FY2024 and
    # FY2025) -- see pdf_extract.py's _find_finance_expense_note, which folds
    # that note's real lines in as additional income_statement rows.
    "InterestExpenseNonOperating": ["interest expense and standby fees",
                                    "interest expense on lease liabilities"],
    # --- balance sheet (bank) ---
    "InterestBearingDepositsAssets": ["interest-bearing deposits with banks",
                                      "interest bearing deposits with banks"],
    "SecurityAgreeToBeResell": [
        "assets purchased under reverse repurchase agreements and securities borrowed"],
    "AllowanceForLoansAndLeaseLosses": ["allowance for loan losses",
                                        "allowance for credit losses"],
    "GrossLoan": ["loans total", "total loans", "gross loans"],
    "NetLoan": ["loans net total", "net loans", "loans net of allowance"],
    "SecuritiesAndInvestments": ["securities total", "total securities"],
    "TotalDeposits": ["deposits total", "total deposits"],
    "NetPPE": ["premises and equipment"],
    "OtherIntangibleAssets": ["other intangibles", "intangible assets",
                              "other intangible assets"],
    "OtherAssets": ["other assets"],
    "TradingLiabilities": ["obligations related to securities sold short"],
    "FinancialInstrumentsSoldUnderAgreementsToRepurchase": [
        "obligations related to assets sold under repurchase agreements and securities loaned"],
    "OtherEquityAdjustments": ["other components of equity"],
    "StockholdersEquity": ["equity attributable to shareholders total"],
    "CashAndDueFromBanks": ["cash and due from banks"],
    "PreferredStock": ["preferred shares and other equity instruments"],
    # --- cash flow ---
    "NetIncomeFromContinuingOperations": ["net income"],   # CF-scoped (vocab scoping)
    "ProvisionForLoanLeaseAndOtherLosses": ["provision for credit losses"],
    "EffectOfExchangeRateChanges": [
        "effect of exchange rate changes on cash and due from banks"],
    "ChangesInCash": ["net change in cash and due from banks"],
    "BeginningCashPosition": ["cash and due from banks at beginning of period"],
    "EndCashPosition": ["cash and due from banks at end of period"],
    "FinancingCashFlow": ["net cash from (used in) financing activities",
                          "net cash used in (from) financing activities"],
    "OperatingCashFlow": ["net cash from (used in) operating activities"],
    "InvestingCashFlow": ["net cash from (used in) investing activities",
                          "net cash used in (from) investing activities"],
    "DeferredIncomeTax": ["deferred income taxes", "deferred income tax"],
})

# Small-cap / fund / synthetic-subtotal pack, DATA-MINED from the 59k-row
# line_items_full corpus (labels unmapped at >=3 tickers). Alias strings shared
# by a balance and a cash-flow concept ("accounts payable" = the BS liability
# AND the CF working-capital change) self-route: build_alias_index only admits
# a key into the statements whose vocab contains it, and the vocab is cleanly
# partitioned (NetIncome IS-only, NetIncomeFromContinuingOperations CF-only...).
_extend_aliases({
    # --- income statement (non-bank) ---
    "NetIncome": ["net loss", "loss for the year", "loss for the period",
                  "net loss for the year", "net loss for the period",
                  "net income for the year", "net income for the period",
                  "net loss and comprehensive loss",
                  "net income and comprehensive income",
                  "net loss and comprehensive loss for the year",
                  "loss and comprehensive loss for the year", "net earnings"],
    "TotalRevenue": ["revenue total", "revenues total"],
    "OperatingExpense": ["expenses total", "operating expenses total", "expenses",
                         "total expenses"],
    "SellingGeneralAndAdministration": [
        "legal fees", "consulting fees", "travel", "office and administration",
        "office and general", "office and miscellaneous", "filing fees",
        "transfer agent and filing fees", "transfer agent fees", "rent",
        "management fees", "shareholder communications", "insurance",
        "regulatory fees", "bank charges", "administration fees",
        "investor relations", "marketing and promotion"],
    "OtherIncomeExpense": ["foreign exchange loss", "foreign exchange gain",
                           "foreign exchange gain (loss)",
                           "foreign exchange loss (gain)", "loss on foreign exchange",
                           "other income", "other expense", "other income (expense)"],
    "InterestIncomeNonOperating": ["interest income earned"],
    # --- balance sheet (non-bank synthetic subtotals + small-cap/fund lines) ---
    "CurrentAssets": ["current assets total"],
    "CurrentLiabilities": ["current liabilities total"],
    "TotalNonCurrentAssets": ["non-current assets total", "non current assets total"],
    "TotalNonCurrentLiabilities": ["non-current liabilities total",
                                   "non current liabilities total"],
    "StockholdersEquity": ["shareholders equity total", "shareholders' equity total",
                           "equity total", "net assets", "total net assets",
                           "net assets attributable to holders of redeemable units",
                           "equity attributable to owners of the company total"],
    "RetainedEarnings": ["deficit", "accumulated deficit"],
    "GainsLossesNotAffectingRetainedEarnings": [
        "accumulated other comprehensive income",
        "accumulated other comprehensive income (loss)",
        "accumulated other comprehensive loss"],
    "AccountsReceivable": ["amounts receivable", "trade and other receivables"],
    "CurrentAccruedExpenses": ["accrued liabilities"],
    "OtherReceivables": ["dividends receivable", "interest receivable",
                         "receivable for portfolio assets sold", "gst receivable",
                         "hst receivable", "sales tax receivable"],
    "PrepaidAssets": ["prepaid expenses", "prepaids", "prepaid expenses and deposits"],
    "OtherPayable": ["distributions payable", "payable for portfolio assets purchased",
                     "due to related parties"],
    "CurrentDebt": ["credit facility", "loan payable", "loans payable"],
    "InvestmentsAndAdvances": ["investments"],
    # "Right-of-use assets on leases" confirmed folded into GuruFocus's own
    # PP&E display row (identity check on RAY/Stingray: PP&E(75.2) =
    # Property and equipment(45.7) + ROU assets(29.5), and separately
    # Investments+PP&E+Intangibles+OtherLTAssets = Total Long-Term Assets
    # EXACTLY -- confirms this is a real aggregation, not a coincidence).
    "NetPPE": ["exploration and evaluation assets", "property and equipment",
              "right-of-use assets on leases", "right-of-use assets"],
    # --- cash flow (anchor + working capital + small-cap lines) ---
    "NetIncomeFromContinuingOperations": [
        "net loss", "loss for the year", "loss for the period",
        "net loss for the year", "net loss for the period", "net income for the year",
        "net income for the period", "net earnings", "net loss and comprehensive loss"],
    "ChangesInCash": ["change in cash during the year", "change in cash",
                      "decrease in cash", "increase in cash", "net decrease in cash",
                      "net increase in cash", "increase (decrease) in cash",
                      "net increase (decrease) in cash",
                      "change in cash and cash equivalents",
                      "net change in cash during the year"],
    "EndCashPosition": ["cash, end of the year", "cash, end of year",
                        "cash end of year", "cash, end of the period",
                        "cash, end of period"],
    "BeginningCashPosition": ["cash, beginning of the year", "cash, beginning of year",
                              "cash beginning of year", "cash, beginning of the period",
                              "cash, beginning of period"],
    "StockBasedCompensation": ["share-based payments", "share based payments",
                               "share-based compensation", "stock-based compensation"],
    "NetOtherFinancingCharges": ["share issuance costs", "share issue costs"],
    "ChangeInReceivables": ["receivables", "accounts receivable", "amounts receivable",
                            "trade and other receivables", "other receivables"],
    "ChangeInPrepaidAssets": ["prepaid expenses", "prepaids",
                              "prepaid expenses and deposits"],
    "ChangeInPayablesAndAccruedExpense": [
        "accounts payable", "accounts payable and accrued liabilities",
        "trade and other payables", "trade payables", "accrued liabilities"],
    "ChangeInOtherWorkingCapital": ["deferred revenue"],
    "ChangeInInventory": ["inventory", "inventories"],
    "NetOtherInvestingChanges": ["exploration and evaluation assets"],
})

# Second mined pass (labels still unmapped at >=2 tickers after the pack above).
_extend_aliases({
    # --- income statement ---
    "OperatingIncome": ["loss from operations", "income from operations",
                        "loss before other items",
                        "loss before other income (expenses)",
                        "income (loss) from operations", "operating profit/(loss)",
                        "operating profit (loss)"],
    "OtherIncomeExpense": ["total other income (expenses)", "total other income (expense)"],
    "TotalRevenue": ["sales revenue", "sales"],
    "NetIncome": ["net (loss) income", "net income (loss)", "net loss (income)",
                  "loss for the year attributable to shareholders"],
    # --- balance sheet ---
    "StockholdersEquity": ["total shareholders' deficit", "total shareholders' deficiency",
                           "total shareholders' (deficit) equity",
                           "total shareholders' equity (deficit)",
                           "total stockholders' and members' equity",
                           "shareholders' deficit total", "unitholders' equity total",
                           "total unitholders' equity", "net asset value"],
    "TotalLiabilitiesAndTotalEquityGrossMinorityInterest": [
        "total liabilities and unitholders' equity",
        "total liabilities and shareholders' deficit",
        "total liabilities and shareholders' deficiency"],
    # --- cash flow ---
    "ChangesInCash": ["change in cash for the year", "change in cash for the period",
                      "decrease in cash and cash equivalents",
                      "increase in cash and cash equivalents",
                      "increase (decrease) in cash and cash equivalents",
                      "net change in cash"],
    "FinancingCashFlow": ["cash flows provided by financing activities",
                          "cash flows used in financing activities",
                          "cash (used in) provided by financing activities",
                          "cash provided by (used in) financing activities",
                          "total cash flows from (used in) financing activities",
                          "total cash flows used in financing activities"],
    "InvestingCashFlow": ["cash flows provided by investing activities",
                          "cash flows used in investing activities",
                          "cash (used in) provided by investing activities",
                          "cash provided by (used in) investing activities",
                          "total cash flows from (used in) investing activities",
                          "total cash flows used in investing activities"],
    "OperatingCashFlow": ["cash flows provided by operating activities",
                          "cash flows used in operating activities",
                          "cash (used in) provided by operating activities",
                          "cash provided by (used in) operating activities",
                          "total cash flows from (used in) operating activities",
                          "total cash flows used in operating activities"],
    "EndCashPosition": ["cash at the end of the year", "cash at the end of the period",
                        "cash at end of year", "cash at end of period"],
    "BeginningCashPosition": ["cash at the beginning of the year",
                              "cash at the beginning of the period",
                              "cash at beginning of year", "cash at beginning of period"],
    "ChangeInWorkingCapital": ["changes in non-cash working capital items total",
                               "changes in non-cash working capital total",
                               "net change in non-cash working capital total",
                               "changes in non-cash operating items total"],
})

# Third mined pass: EPS/share-count phrasings ("Net income per share - Basic"),
# synthetic CF activity totals, and investment-fund income-statement wording.
_extend_aliases({
    "BasicEPS": ["net income per share basic", "net income (loss) per share basic",
                 "net loss per share basic", "loss per share basic",
                 "earnings per share basic", "net earnings per share basic",
                 "increase in net assets from operations per unit",
                 "increase (decrease) in net assets from operations per unit"],
    "DilutedEPS": ["net income per share diluted", "net income (loss) per share diluted",
                   "net loss per share diluted", "loss per share diluted",
                   "earnings per share diluted", "net earnings per share diluted"],
    "BasicAverageShares": ["weighted average number of shares basic",
                           "weighted average shares outstanding basic",
                           "weighted average number of common shares outstanding basic"],
    "DilutedAverageShares": ["weighted average number of shares diluted",
                             "weighted average shares outstanding diluted",
                             "weighted average number of common shares outstanding diluted"],
    "OperatingCashFlow": ["operating activities total"],
    "InvestingCashFlow": ["investing activities total"],
    "FinancingCashFlow": ["financing activities total"],
    "NetIncome": ["increase in net assets from operations",
                  "decrease in net assets from operations",
                  "increase (decrease) in net assets from operations",
                  "increase in net assets attributable to holders of redeemable units",
                  "change in net assets attributable to holders of redeemable units"],
    "TotalRevenue": ["income (loss) on investments total", "income on investments total",
                     "total investment income"],
    "TotalAssets": ["assets total"],
    "TotalLiabilities": ["liabilities total"],
    "TotalLiabilitiesAndTotalEquityGrossMinorityInterest": [
        "liabilities and equity total", "liabilities and shareholders' equity total",
        "liabilities and unitholders' equity total"],
})

# Fourth mined pass (RAY / Stingray Group balance-sheet + cash-flow audit,
# user-reported vs an external GuruFocus-style export -- traced to unmapped
# debt/lease/intangible labels, NOT a sign-convention bug: the external
# export used a different, non-target sign convention for TaxProvision,
# verified WRONG against real QuoteMedia ground truth for RY -- our
# positive-magnitude convention (NetIncome = PretaxIncome - TaxProvision) is
# the correct one; Pretax-Tax=Net held across all 14 years of RY's actual
# QuoteMedia data, Pretax+Tax did not).
_extend_aliases({
    "LongTermDebt": ["subordinated debt"],
    "NonCurrentDeferredTaxesLiabilities": ["deferred tax liabilities"],
    "OtherIntangibleAssets": ["broadcast licences", "broadcast licenses",
                              "intangible assets excluding broadcast licences",
                              "intangible assets excluding broadcast licenses"],
    # --- cash flow ---
    "RepurchaseOfCapitalStock": ["shares repurchased and cancelled"],
    "IssuanceOfCapitalStock": ["proceeds from the exercise of stock options",
                               "proceeds from exercise of stock options"],
    "PurchaseOfBusiness": ["business acquisitions net of cash acquired"],
    "NetIssuancePaymentsOfDebt": ["increase (decrease) of credit facilities",
                                  "decrease of subordinated debt",
                                  "increase of subordinated debt"],
    "StockBasedCompensation": ["share-based compensation psu and dsu expenses"],
})

# CONTEXT-DEPENDENT aliases: the same label means different things under
# different sections/zones ("Loans" under Interest income vs under Assets;
# "Derivatives" on the asset vs liability side). Tried BEFORE bare aliases.
# Key = "normalized-context > normalized-label".
COMPOUND_ALIASES: dict[str, dict[str, str]] = {
    "income_statement": {
        "interest and dividend income > loans": "InterestIncomeFromLoans",
        "interest income > loans": "InterestIncomeFromLoans",
        "interest and dividend income > securities": "InterestIncomeFromSecurities",
        "interest income > securities": "InterestIncomeFromSecurities",
        "interest and dividend income > assets purchased under reverse repurchase "
        "agreements and securities borrowed":
            "InterestIncomeFromFederalFundsSoldAndSecuritiesPurchaseUnderAgreementsToResell",
        # NOTE: "interest income > deposits and other" NOT mapped to
        # InterestIncomeFromDeposits -- QM's key holds a different (negative,
        # netted) figure; verified mismatch on RY.
        "interest expense > deposits and other": "InterestExpenseForDeposit",
        "interest expense > other liabilities": "OtherInterestExpense",
        "interest expense > subordinated debentures":
            "InterestExpenseForLongTermDebtAndCapitalSecurities",
        "net income attributable to > shareholders": "NetIncome",
        "net income attributable to > non-controlling interests": "MinorityInterests",
        "loss per share > basic and diluted": "BasicEPS",
        "earnings per share > basic": "BasicEPS",
        "earnings per share > diluted": "DilutedEPS",
    },
    "balance_sheet": {
        "securities > trading": "TradingSecurities",
        # NOTE: "Investment, net of applicable allowance" is deliberately NOT
        # mapped to AvailableForSaleSecurities -- under IFRS 9 the investment
        # bucket is FVOCI + amortized cost, a wider aggregate than AFS (RY
        # 2020: 139,743 investment vs 115,550 AFS). The section subtotal still
        # maps to SecuritiesAndInvestments; the line stays in line_items_full.
        "loans > retail": "ConsumerLoan",
        "loans > wholesale": "CommercialLoan",
        "assets > derivatives": "DerivativeAssets",
        "liabilities > derivatives": "DerivativeProductLiabilities",
        "liabilities and equity > derivatives": "DerivativeProductLiabilities",
        "liabilities and equity > other liabilities": "OtherPayable",
        # synthetic "Current total" is ambiguous without the zone
        "assets > current total": "CurrentAssets",
        "liabilities > current total": "CurrentLiabilities",
        "liabilities and equity > current total": "CurrentLiabilities",
        "assets > non-current total": "TotalNonCurrentAssets",
        "liabilities > non-current total": "TotalNonCurrentLiabilities",
        "liabilities and equity > non-current total": "TotalNonCurrentLiabilities",
        # "Credit facilities" / lease liabilities print under BOTH current and
        # non-current sections with the IDENTICAL bare label -- confirmed on
        # RAY (Stingray Group): the non-current instance (Credit facilities
        # 309.1M + Subordinated debt 39.6M = 348.8M) was entirely UNMAPPED,
        # so the OtherNonCurrentLiabilities plug silently absorbed the whole
        # non-current-liabilities total instead of isolating real LongTermDebt.
        "current liabilities > credit facilities": "CurrentDebt",
        "non-current liabilities > credit facilities": "LongTermDebt",
        "current liabilities > current portion of lease liabilities": "CurrentCapitalLeaseObligation",
        "non-current liabilities > lease liabilities": "LongTermCapitalLeaseObligation",
    },
    "cash_flow": {},
}


def build_alias_index(vocab: dict[str, list[str]]) -> dict[str, dict[str, str]]:
    """{statement_type: {normalized_alias: canonical_key}}. Humanized canonical
    key + curated synonyms; only keys actually in that statement's vocab."""
    index: dict[str, dict[str, str]] = {s: {} for s in STATEMENTS}
    for stmt in STATEMENTS:
        allowed = set(vocab.get(stmt, []))
        for key in allowed:
            index[stmt].setdefault(_normalize_label(_humanize(key)), key)
        for key, aliases in CURATED_ALIASES.items():
            if key not in allowed:
                continue
            for alias in aliases:
                index[stmt].setdefault(_normalize_label(alias), key)
    return index


def match_label(label: str, statement_type: str,
                alias_index: dict[str, dict[str, str]]) -> tuple[str | None, float]:
    """(canonical_key, confidence) or (None, 0.0). Exact normalized-alias match
    first (confidence 1.0), else difflib fuzzy >= FUZZY_THRESHOLD; ambiguous
    near-ties are rejected rather than guessed."""
    aliases = alias_index.get(statement_type, {})
    norm = _normalize_label(label)
    if not norm:
        return None, 0.0
    if norm in aliases:
        return aliases[norm], 1.0
    # fuzzy: best ratio per candidate key
    best_key, best_ratio, second_ratio = None, 0.0, 0.0
    for alias, key in aliases.items():
        r = difflib.SequenceMatcher(None, norm, alias).ratio()
        if r > best_ratio:
            best_key, second_ratio, best_ratio = key, best_ratio, r
        elif r > second_ratio and key != best_key:
            second_ratio = r
    if best_ratio >= FUZZY_THRESHOLD and (best_ratio - second_ratio) >= FUZZY_AMBIGUOUS_DELTA:
        return best_key, best_ratio
    if best_ratio >= FUZZY_THRESHOLD:
        return None, best_ratio  # ambiguous near-tie
    return None, 0.0


def build_compound_index(vocab: dict[str, list[str]]) -> dict[str, dict[str, str]]:
    """{statement_type: {normalized 'context > label': canonical_key}} --
    context-dependent aliases, filtered to keys present in that statement's vocab."""
    index: dict[str, dict[str, str]] = {s: {} for s in STATEMENTS}
    for stmt in STATEMENTS:
        allowed = set(vocab.get(stmt, []))
        for compound, key in COMPOUND_ALIASES.get(stmt, {}).items():
            if key not in allowed:
                continue
            ctx, _, lbl = compound.partition(">")
            norm = f"{_normalize_label(ctx)} > {_normalize_label(lbl)}"
            index[stmt][norm] = key
    return index


# Match-kind priority for duplicate resolution: a context-specific match beats a
# bare exact, which beats a suffix salvage, which beats fuzzy.
_KIND_RANK = {"compound": 4, "exact": 3, "suffix": 2, "fuzzy": 1}
_SUFFIX_MIN_ALIAS_LEN = 12  # only long, distinctive aliases may suffix-match


def match_label_ctx(label: str, section: str | None, zone: str | None,
                    statement_type: str, alias_index, compound_index
                    ) -> tuple[str | None, float, str | None]:
    """(canonical_key, confidence, match_kind). Order:
    1. compound  'section > label' / 'zone > label'  (context disambiguates)
    2. exact     bare normalized alias
    3. suffix    normalized label ENDS WITH a long alias -- salvages labels with
                 prose glued to the front (2-column-page contamination)
    4. fuzzy     difflib >= threshold, ambiguity-guarded (via match_label)."""
    norm = _normalize_label(label)
    if not norm:
        return None, 0.0, None
    compounds = compound_index.get(statement_type, {})
    for ctx in (section, zone):
        if ctx:
            hit = compounds.get(f"{_normalize_label(ctx)} > {norm}")
            if hit:
                return hit, 1.0, "compound"
    aliases = alias_index.get(statement_type, {})
    if norm in aliases:
        return aliases[norm], 1.0, "exact"
    # suffix salvage: only when the tail IS a known long alias
    for alias, key in aliases.items():
        if len(alias) >= _SUFFIX_MIN_ALIAS_LEN and norm.endswith(alias):
            return key, 0.93, "suffix"
    key, conf = match_label(label, statement_type, alias_index)
    return key, conf, ("fuzzy" if key else None)


# ---------------------------------------------------------------------------
# VALIDATION — accounting identities across the mapped line items
# ---------------------------------------------------------------------------

def _close(a: float, b: float, tol: float = IDENTITY_TOL) -> bool:
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= tol * scale


def _identity_checks(by_key: dict[str, float], statement_type: str) -> list[str]:
    """Cross-line accounting identities on ONE year's mapped {key: value}.
    Returns a list of flags for identities that were checkable but FAILED."""
    flags: list[str] = []
    g = by_key.get
    if statement_type == "balance_sheet":
        assets = g("TotalAssets")
        liab = g("TotalLiabilities")
        equity = g("TotalEquityGrossMinorityInterest", g("StockholdersEquity"))
        if assets is not None and liab is not None and equity is not None:
            if not _close(assets, liab + equity):
                flags.append("balance_identity_fail")
        ca, nca = g("CurrentAssets"), g("TotalNonCurrentAssets")
        if assets is not None and ca is not None and nca is not None:
            if not _close(assets, ca + nca):
                flags.append("asset_split_fail")
    elif statement_type == "income_statement":
        gp, rev, cogs = g("GrossProfit"), g("TotalRevenue"), g("CostOfRevenue")
        if gp is not None and rev is not None and cogs is not None:
            # sign-robust: COGS may be stored positive (subtract) or negative (add).
            if not (_close(gp, rev - cogs) or _close(gp, rev + cogs)):
                flags.append("gross_profit_fail")
    elif statement_type == "cash_flow":
        chg = g("ChangesInCash")
        op, inv, fin = g("OperatingCashFlow"), g("InvestingCashFlow"), g("FinancingCashFlow")
        fx = g("EffectOfExchangeRateChanges") or 0.0
        if None not in (chg, op, inv, fin):
            if not _close(chg, op + inv + fin + fx):
                flags.append("cashflow_recon_fail")
        beg, end = g("BeginningCashPosition"), g("EndCashPosition")
        if None not in (beg, end, chg):
            if not _close(end, beg + chg):
                flags.append("cash_rollforward_fail")
    return flags


def compare_to_quotemedia(rule_db: str, quote_db: str, ticker: str) -> dict:
    """Cross-source ground-truth check (validation check E): compare rule-extracted
    values against the SAME company's QuoteMedia values on overlapping
    (statement_type, line_item, year). Returns match/mismatch counts and, crucially,
    detects a systematic scale error -- if a large share of mismatches are off by
    exactly x1000/x1e6, the units note was mis-read. Read-only."""
    def _load(db: str) -> dict[tuple, float]:
        out: dict[tuple, float] = {}
        conn = sqlite3.connect(db)
        try:
            for st, li, fy, v in conn.execute(
                "SELECT statement_type, line_item, fiscal_year, value "
                "FROM statement_lines WHERE ticker = ?", (ticker,)):
                if v is not None:
                    out[(st, li, int(fy))] = float(v)
        finally:
            conn.close()
        return out

    rule, quote = _load(rule_db), _load(quote_db)
    common = set(rule) & set(quote)
    matched = mismatched = 0
    scale_off = {1000.0: 0, 1e6: 0, 0.001: 0, 1e-6: 0}
    examples: list[str] = []
    for k in sorted(common):
        rv, qv = rule[k], quote[k]
        if _close(rv, qv, tol=0.01):
            matched += 1
            continue
        mismatched += 1
        for f in scale_off:
            if qv != 0 and _close(rv * f, qv, tol=0.01):
                scale_off[f] += 1
        if len(examples) < 8:
            examples.append(f"{k[0]}/{k[1]}/{k[2]}: rule={rv:,.0f} quote={qv:,.0f}")
    return {"ticker": ticker, "overlap": len(common), "matched": matched,
            "mismatched": mismatched,
            "match_pct": (matched / len(common) * 100) if common else 0.0,
            "scale_off_counts": {f: n for f, n in scale_off.items() if n},
            "examples": examples}


# ---------------------------------------------------------------------------
# ORCHESTRATION
# ---------------------------------------------------------------------------

def _sec_crosslisted_tickers(src_db: str) -> set[str]:
    """Tickers the finder resolved via SEC/EDGAR cross-listing (a note, not a
    real first-party PDF) -- excluded from rule extraction."""
    out: set[str] = set()
    conn = sqlite3.connect(src_db)
    try:
        try:
            for (tk,) in conn.execute(
                "SELECT ticker FROM filings WHERE discovery_method LIKE '%sec%' "
                "OR pdf_url LIKE '%sec.gov%' OR pdf_url LIKE 'SEC cross-listed%'"):
                out.add(tk)
        except sqlite3.OperationalError:
            pass
        # belt-and-suspenders: any filing_pdfs row pointing at sec.gov
        try:
            for (tk,) in conn.execute(
                "SELECT DISTINCT ticker FROM filing_pdfs WHERE pdf_url LIKE '%sec.gov%'"):
                out.add(tk)
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return out


# Canonical keys that ONLY make sense on a bank/financial-institution statement.
# Offering them on a non-bank statement is how "NonInterestIncome" fuzzy-matched
# a media company's line. Gated behind _BANK_RE below.
BANK_ONLY_KEYS = {
    "NetInterestIncome", "NonInterestIncome", "InterestIncomeFromLoans",
    "InterestIncomeFromDeposits", "InterestIncomeFromInvestmentSecurities",
    "NonInterestExpense", "OtherNonInterestIncome", "OtherNonInterestExpense",
    "CreditLossesProvision", "InterestExpenseForDeposit",
}
# Genuine bank/financial-institution structure. Deliberately EXCLUDES bare
# "interest income" / "non-interest income" -- those appear on non-banks too (any
# company earns interest income), which would make the gate circular.
_BANK_RE = re.compile(r"net interest income|deposits (with|from) banks?|"
                      r"loans and advances|interest income (from|on) loans|"
                      r"provision for (credit|loan) losses|"
                      r"interest and dividend income", re.I)

# Per-share amounts are tiny; a mapped per-share value bigger than this is almost
# certainly a mis-parse (e.g. a note number matched to EPS).
_PER_SHARE_MAX = 1000.0
# EPS/DPS keys require genuine per-share LABEL context, so a bare "Diluted" note
# number can't map to DilutedEPS.
_EPS_KEYS = {"BasicEPS", "DilutedEPS", "DividendPerShare",
             "BasicContinuousOperations", "DilutedContinuousOperations"}
_PER_SHARE_CONTEXT_RE = re.compile(r"per share|per unit|\beps\b|earnings per|"
                                   r"loss per|income per|dividend", re.I)

# Headline items whose presence signals a well-covered statement (confidence input).
KEY_TOTALS = {
    "income_statement": ["TotalRevenue", "NetIncome"],
    "balance_sheet": ["TotalAssets", "TotalLiabilities",
                      "TotalEquityGrossMinorityInterest", "StockholdersEquity",
                      "CurrentAssets", "CurrentLiabilities"],
    "cash_flow": ["OperatingCashFlow", "InvestingCashFlow", "FinancingCashFlow",
                  "EndCashPosition"],
}


def _resolve_scale(parsed: dict, unit_scale_hint: float | None,
                   mapped: dict) -> tuple[float, float, list[str]]:
    """Resolve the money scale with a confidence. Priority: the statement's own
    units note (`in thousands/millions`), then the WHOLE-DOCUMENT hint captured in
    step 2 (`pdf_extract._detect_unit_scale_hint`, the main fix for statements whose
    note sat on a cropped page header), then assume 1 (flagged, low confidence --
    we do NOT guess a scale from magnitude, which would risk introducing errors)."""
    flags: list[str] = []
    if parsed.get("scale_found"):
        return parsed["scale"], 0.95, flags
    if unit_scale_hint:
        return float(unit_scale_hint), 0.80, ["scale_from_doc_hint"]
    return 1.0, 0.35, ["scale_assumed"]


def _statement_confidence(match_confs: list[float], scale_conf: float,
                          identity_fail: int, by_key_year: dict, statement_type: str,
                          col_years: list[int], sparse: bool) -> float:
    """Composite 0-1 confidence for one statement: mean match confidence, scale
    certainty, key-total coverage, and whether accounting identities reconciled."""
    mean_match = (sum(match_confs) / len(match_confs)) if match_confs else 0.0
    keytot = KEY_TOTALS.get(statement_type, [])
    covered = (sum(1 for k in keytot if any(k in by_key_year[y] for y in col_years))
               / len(keytot)) if keytot else 0.0
    identity_ok = 1.0 if identity_fail == 0 else 0.4
    score = 0.30 * mean_match + 0.25 * scale_conf + 0.25 * covered + 0.20 * identity_ok
    if sparse:
        score *= 0.6
    return round(max(0.0, min(1.0, score)), 3)


def extract_one_statement(text: str, statement_type: str, allowed: set[str], alias_index,
                          *, unit_scale_hint: float | None = None) -> dict:
    """Full rule pipeline for one statement's text: parse -> map (with industry
    gating + plausibility) -> resolve scale (two-pass) -> apply -> validate ->
    confidence. Returns year_meta, line_rows (with per-line confidence), scale,
    currency, dropped, flags, n_mapped, confidence."""
    parsed = parse_statement(text, statement_type)
    flags = list(parsed["flags"])
    col_years = parsed["col_years"]
    period_ends = parsed.get("period_ends", {})
    year_meta = {y: {"period_end": period_ends.get(y), "currency": parsed["currency"]}
                 for y in col_years}
    if not col_years:
        return dict(year_meta={}, line_rows=[], scale=parsed["scale"],
                    currency=parsed["currency"], dropped=[], flags=flags,
                    n_mapped=0, confidence=0.0)

    tokens = _extract_number_tokens(text)
    is_bank = bool(_BANK_RE.search(text))

    # PASS 1 -- map labels to canonical keys, collecting PRE-SCALE printed values.
    # mapped[(year, key)] = (printed, confidence, per_share, label)
    mapped: dict[tuple[int, str], tuple] = {}
    match_confs: list[float] = []
    dropped: list[tuple[int, str]] = []
    n_mapped = 0
    for label, values in parsed["rows"]:
        key, conf = match_label(label, statement_type, alias_index)
        if key is None or key not in allowed:
            continue
        if key in BANK_ONLY_KEYS and not is_bank:      # industry gating
            flags.append("bank_key_on_nonbank")
            continue
        if key in _EPS_KEYS and not _PER_SHARE_CONTEXT_RE.search(label):
            flags.append("eps_without_context")        # bare "Diluted" note number
            continue
        n_mapped += 1
        match_confs.append(conf)
        per_share = key in NO_SCALE_KEYS
        for i, printed in enumerate(values):
            if printed is None:
                continue
            y = col_years[i]
            if not _verify_value(printed, tokens):     # parse sanity (pre-scale)
                dropped.append((y, key))
                continue
            if per_share and abs(printed) > _PER_SHARE_MAX:  # plausibility
                flags.append("implausible_pershare")
                continue
            prev = mapped.get((y, key))
            cand = (printed, conf, per_share, label)
            if prev is None:
                mapped[(y, key)] = cand
            elif conf > prev[1]:
                # keep the higher-confidence mapping; on a tie the FIRST (earlier
                # in the statement) wins -- e.g. the real "Net income" line comes
                # before a later "comprehensive income" total.
                flags.append("duplicate_map")
                mapped[(y, key)] = cand
            else:
                flags.append("duplicate_map")

    # PASS 2 -- resolve scale (note -> whole-doc hint -> assumed) with confidence.
    scale, scale_conf, scale_flags = _resolve_scale(parsed, unit_scale_hint, mapped)
    flags.extend(scale_flags)

    # PASS 3 -- apply scale, build per-year maps + line rows.
    picked: dict[tuple[int, str], float] = {}
    by_key_year: dict[int, dict[str, float]] = {y: {} for y in col_years}
    for (y, key), (printed, conf, per_share, label) in mapped.items():
        val = printed if per_share else printed * scale
        val = _coerce_numeric(val)
        if val is None:
            continue
        picked[(y, key)] = val
        by_key_year[y][key] = val

    # accounting-identity checks per year
    identity_fail = 0
    for y in col_years:
        f = _identity_checks(by_key_year[y], statement_type)
        flags.extend(f)
        identity_fail += len(f)
    sparse = n_mapped < MIN_ROWS_OK
    if sparse:
        flags.append("sparse")

    confidence = _statement_confidence(match_confs, scale_conf, identity_fail,
                                       by_key_year, statement_type, col_years, sparse)
    # statement_lines stays narrow (value only); per-line confidence goes to the
    # parallel statement_line_quality table in run_rule_extraction.
    line_rows = [dict(fiscal_year=y, statement_type=statement_type, line_item=k, value=v)
                 for (y, k), v in picked.items()]
    seen_f: set[str] = set()
    flags = [f for f in flags if not (f in seen_f or seen_f.add(f))]
    return dict(year_meta=year_meta, line_rows=line_rows, scale=scale,
                scale_confidence=scale_conf, currency=parsed["currency"],
                dropped=dropped, flags=flags, n_mapped=n_mapped, confidence=confidence)


def _rule_rows(src_db: str, *, limit: int | None, tickers: set[str] | None) -> list[dict]:
    """Like llm_extract._rows_to_extract but also pulls the step-2 doc_type +
    unit_scale_hint columns (resilient to older DBs lacking them)."""
    conn = sqlite3.connect(src_db)
    try:
        conn.executescript(FILINGS_SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        present = {r[1] for r in conn.execute("PRAGMA table_info(pdf_extractions)")}
        extra = [c for c in ("doc_type", "unit_scale_hint") if c in present]
        sel = "".join(f", p.{c}" for c in extra)
        cur = conn.execute(
            "SELECT p.ticker, p.fiscal_year, p.pdf_url, p.income_statement, "
            "p.balance_sheet, p.cash_flow, p.primary_block, f.company_name, f.exchange"
            + sel +
            " FROM pdf_extractions p "
            "LEFT JOIN filing_pdfs f ON f.ticker=p.ticker AND f.fiscal_year=p.fiscal_year "
            "WHERE p.extract_ok=1 AND p.scanned=0 ORDER BY p.ticker, p.fiscal_year DESC")
        names = [c[0] for c in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    if tickers:
        rows = [r for r in rows if r["ticker"] in tickers]
    if limit:
        rows = rows[:limit]
    return rows


# accounting-identity flags whose presence means a statement FAILED reconciliation.
_IDENTITY_FAIL_FLAGS = {"balance_identity_fail", "asset_split_fail", "gross_profit_fail",
                        "cashflow_recon_fail", "cash_rollforward_fail"}
CONFIDENCE_REVIEW_THRESHOLD = 0.55  # below this -> status 'needs_review'


def run_rule_extraction(cfg, *, force: bool = False, limit: int | None = None,
                        tickers: set[str] | None = None, progress=None) -> dict:
    src_db = cfg.get("storage", {}).get("db_path", "output/filings.db")
    fin_db = (cfg.get("rules", {}).get("db_path")
              or "output/pdf_financials.db")
    vocab = _load_vocab(Path(cfg.get("rules", {}).get("vocab_path", DEFAULT_VOCAB)))
    alias_index = build_alias_index(vocab)

    rows = _rule_rows(src_db, limit=limit, tickers=tickers)
    excluded = _sec_crosslisted_tickers(src_db)

    store = FinancialsStore(fin_db)
    already = set() if force else store.completed_statements()
    prior_tickers = store.existing_tickers()

    identity: dict[str, dict] = {}
    year_meta_acc: dict[tuple[str, int], dict] = {}
    line_rows_acc: list[dict] = []
    quality_acc: list[dict] = []
    raw_rows: list[dict] = []
    status_rows: list[dict] = []
    parsed_ok = skipped = empty = unverified = needs_review = 0
    balance_pass = balance_checked = 0
    key_total_hits = key_total_possible = 0
    confidences: list[float] = []
    t0 = time.monotonic()

    def _status(ticker, fy, stmt, status, reason, n, conf, dtype):
        # keep every status row's keys IDENTICAL (bulk_upsert infers columns
        # from rows[0]).
        status_rows.append(dict(ticker=ticker, fiscal_year=fy, statement_type=stmt,
                                status=status, reason=reason, n_lines=n,
                                confidence=conf, doc_type=dtype))

    if progress:
        progress(f"Rule extraction: {len(rows)} pdf row(s) from {src_db} -> {fin_db}")

    for row in rows:
        ticker, row_fy = row["ticker"], row["fiscal_year"]
        doc_type = row.get("doc_type")
        if ticker in excluded:
            _status(ticker, row_fy, "_all", "skipped_sec_crosslisted",
                    "SEC/EDGAR cross-listed", 0, 0.0, doc_type)
            skipped += 1
            continue
        # doc-type gate: an AIF/MD&A/interim isn't primary statements -> don't
        # parse it as one (None = pre-migration data / classify failed -> allow).
        if doc_type and doc_type not in ("primary_statements", "annual_report_wrapper"):
            _status(ticker, row_fy, "_all", "skipped_non_statements", doc_type, 0, 0.0, doc_type)
            skipped += 1
            continue

        for stmt in STATEMENTS:
            if (ticker, row_fy, stmt) in already:
                skipped += 1
                continue
            text = _section_text(row, stmt)
            if not text:
                _status(ticker, row_fy, stmt, "empty", "no section text", 0, 0.0, doc_type)
                empty += 1
                continue
            allowed = set(vocab.get(stmt, []))
            res = extract_one_statement(text, stmt, allowed, alias_index,
                                        unit_scale_hint=row.get("unit_scale_hint"))
            unverified += len(res["dropped"])
            conf = res["confidence"]
            for y, meta in res["year_meta"].items():
                cur = year_meta_acc.setdefault(
                    (ticker, y), {"period_end": None, "currency": None,
                                  "source_ref": row["pdf_url"]})
                cur["period_end"] = cur["period_end"] or meta.get("period_end")
                cur["currency"] = cur["currency"] or meta.get("currency")
            for ln in res["line_rows"]:
                line_rows_acc.append(dict(ticker=ticker, **ln))
                quality_acc.append(dict(ticker=ticker, fiscal_year=ln["fiscal_year"],
                                        statement_type=ln["statement_type"],
                                        line_item=ln["line_item"], confidence=conf))
            ident = identity.setdefault(
                ticker, {"company_name": row.get("company_name"),
                         "exchange": row.get("exchange"),
                         "latest_fy": None, "currency": res["currency"]})
            for y in res["year_meta"]:
                if ident["latest_fy"] is None or y > ident["latest_fy"]:
                    ident["latest_fy"], ident["currency"] = y, res["currency"]

            # metrics for the report
            if stmt == "balance_sheet" and res["line_rows"]:
                balance_checked += 1
                if "balance_identity_fail" not in res["flags"]:
                    balance_pass += 1
            for kt in KEY_TOTALS.get(stmt, []):
                key_total_possible += 1
                if any(ln["line_item"] == kt for ln in res["line_rows"]):
                    key_total_hits += 1

            n = len(res["line_rows"])
            hard_fail = res["n_mapped"] == 0 and not res["year_meta"]
            if hard_fail:
                status = "empty"
                empty += 1
            elif conf < CONFIDENCE_REVIEW_THRESHOLD:
                status = "needs_review"
                needs_review += 1
                parsed_ok += 1
            else:
                status = "ok_warnings" if res["flags"] else "ok"
                parsed_ok += 1
            if not hard_fail:
                confidences.append(conf)
            _status(ticker, row_fy, stmt, status,
                    (", ".join(res["flags"]) or None), n, conf, doc_type)
            raw_rows.append(dict(ticker=ticker, fiscal_year=row_fy, statement_type=stmt,
                                 model="rules-v1", prompt_version="rules-v1",
                                 unit_scale=res["scale"], currency=res["currency"],
                                 raw_json=json.dumps({"flags": res["flags"],
                                                      "confidence": conf,
                                                      "scale_confidence": res.get("scale_confidence"),
                                                      "n_mapped": res["n_mapped"],
                                                      "n_written": n})))
            if progress:
                warn = f"  [{', '.join(res['flags'])}]" if res["flags"] else ""
                progress(f"  {ticker} {row_fy} {stmt:16} -> {n} lines  conf={conf:.2f}{warn}")

    company_rows = [
        dict(ticker=t, company_name=v["company_name"], exchange=v["exchange"],
             currency=v["currency"], primary_source="cse_pdf_extract")
        for t, v in identity.items() if t not in prior_tickers]
    year_rows = [
        dict(ticker=t, fiscal_year=fy, period_end=m["period_end"], currency=m["currency"],
             source="cse_pdf_extract", source_ref=m["source_ref"])
        for (t, fy), m in year_meta_acc.items()]

    store.bulk_upsert_companies(company_rows)
    store.bulk_upsert_company_years(year_rows)
    store.bulk_upsert_statement_lines(line_rows_acc)
    store.bulk_upsert_line_quality(quality_acc)
    store.bulk_upsert_llm_raw(raw_rows)
    store.bulk_upsert_llm_status(status_rows)
    store.close()

    elapsed = time.monotonic() - t0
    mean_conf = (sum(confidences) / len(confidences)) if confidences else 0.0
    return {"attempted": len(rows) * len(STATEMENTS), "parsed_ok": parsed_ok,
            "empty": empty, "skipped": skipped, "needs_review": needs_review,
            "companies": len(company_rows), "years": len(year_rows),
            "lines": len(line_rows_acc), "unverified_dropped": unverified,
            "excluded_tickers": len(excluded),
            "balance_identity_pass": balance_pass, "balance_identity_checked": balance_checked,
            "key_total_coverage_pct": (key_total_hits / key_total_possible * 100)
                                      if key_total_possible else 0.0,
            "mean_confidence": mean_conf,
            "elapsed": elapsed, "db_path": fin_db}
