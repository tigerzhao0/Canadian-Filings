"""Identity-based derivation + zero-fill for the canonical statement slots.

GuruFocus-style displays are DENSE because they don't stop at what a filer
prints: roll-ups are computed from details (and vice versa) via accounting
identities, "Other ..." rows are the PLUG between a total and its named
details, and a template row the company simply doesn't report is 0 on the
statement face. This module reproduces that, with three hard rules:

1. NEVER overwrite a mapped value -- derivation only fills EMPTY slots.
2. Every derived value comes from an identity over values already present
   (mapped or previously derived); iterate to a fixpoint.
3. Zero-fill is LAST, leaf-keys only, and only for a statement that actually
   parsed with its anchor total present -- absence on such a face IS zero.

Pure functions over plain dicts -- unit-testable without a DB.
"""
from __future__ import annotations

IS, BS, CF = "income_statement", "balance_sheet", "cash_flow"

# ---------------------------------------------------------------------------
# Template keys (what the xlsx renders) -- pulled from ROW_SPEC so the export
# and the fill logic can never drift apart.
# ---------------------------------------------------------------------------


def template_keys(include_bank: bool) -> dict[str, list[str]]:
    """statement_type -> ordered template keys, from company_xlsx_export.ROW_SPEC."""
    from company_xlsx_export import ROW_SPEC
    out: dict[str, list[str]] = {IS: [], BS: [], CF: []}
    for row in ROW_SPEC:
        if not isinstance(row, tuple) or row[0] not in ("data", "data_bank"):
            continue
        kind, _label, stmt, key = row[0], row[1], row[2], row[3]
        if not stmt or not key:
            continue
        if kind == "data_bank" and not include_bank:
            continue
        if key not in out[stmt]:
            out[stmt].append(key)
    return out


# ---------------------------------------------------------------------------
# Derivation rules. Each rule: (stmt, key, fn) where fn(get) returns a value
# or None. `get(stmt, key)` returns the CURRENT value (mapped or derived) or
# None. `get0` treats missing as 0.0 -- use it only where a missing detail
# legitimately means "none reported".
# ---------------------------------------------------------------------------


def _sum0(get, stmt, keys):
    vals = [get(stmt, k) for k in keys]
    if all(v is None for v in vals):
        return None
    return sum(v for v in vals if v is not None)


def _plug(total, parts):
    """total - sum(parts, missing=0); None when the total is unknown.
    Small negatives (rounding) clamp to 0; large negatives mean the details
    overlap/conflict -> refuse (None) rather than write a wrong number."""
    if total is None:
        return None
    s = sum(p for p in parts if p is not None)
    r = total - s
    if r < 0:
        return 0.0 if abs(r) <= 0.005 * max(abs(total), 1.0) else None
    return r


def _any2of3(a, b, c):
    """Solve a = b + c for whichever single term is missing. Returns
    (a, b, c) with at most the one missing term filled."""
    known = sum(x is not None for x in (a, b, c))
    if known != 2:
        return a, b, c
    if a is None:
        return b + c, b, c
    if b is None:
        return a, a - c, c
    return a, b, a - b


def derive_year(vals: dict[tuple[str, str], float], *,
                has_stmt: set[str]) -> dict[tuple[str, str], float]:
    """One (ticker, fiscal_year): fill empty canonical slots from identities.
    `vals` maps (stmt, key) -> mapped value. Returns ONLY the new slots.
    `has_stmt` = statements that actually parsed for this year (an identity
    may only assume 'not reported == zero' within a statement we truly have).
    """
    cur = dict(vals)
    new: dict[tuple[str, str], float] = {}

    def get(stmt, key):
        return cur.get((stmt, key))

    def put(stmt, key, v):
        if v is None or (stmt, key) in cur:
            return
        cur[(stmt, key)] = v
        new[(stmt, key)] = v

    for _ in range(6):  # fixpoint: rules feed each other; 6 passes is plenty
        before = len(cur)

        # cross-statement seed: the CF's own net-income line can anchor a
        # missing/thin income statement (same number, different page)
        if get(IS, "NetIncome") is None \
                and get(CF, "NetIncomeFromContinuingOperations") is not None:
            put(IS, "NetIncome", get(CF, "NetIncomeFromContinuingOperations"))

        # ---------------- income statement ----------------
        if IS in has_stmt:
            rev, cogs, gp = get(IS, "TotalRevenue"), get(IS, "CostOfRevenue"), get(IS, "GrossProfit")
            # service/shell companies: no COGS line -> GrossProfit == Revenue
            if gp is None and rev is not None and cogs is None:
                put(IS, "CostOfRevenue", 0.0)
                put(IS, "GrossProfit", rev)
            elif rev is None and cogs is not None and gp is not None:
                put(IS, "TotalRevenue", cogs + gp)
            elif cogs is None and rev is not None and gp is not None:
                put(IS, "CostOfRevenue", rev - gp)
            elif gp is None and rev is not None and cogs is not None:
                put(IS, "GrossProfit", rev - cogs)

            opex = get(IS, "OperatingExpense")
            if opex is None:
                opex = _sum0(get, IS, ["SellingGeneralAndAdministration",
                                       "ResearchAndDevelopment",
                                       "OtherOperatingExpenses"])
                put(IS, "OperatingExpense", opex)
            if get(IS, "OperatingIncome") is None and get(IS, "GrossProfit") is not None \
                    and get(IS, "OperatingExpense") is not None:
                put(IS, "OperatingIncome", get(IS, "GrossProfit") - get(IS, "OperatingExpense"))

            ni, tax, ptx = get(IS, "NetIncome"), get(IS, "TaxProvision"), get(IS, "PretaxIncome")
            if ptx is None and ni is not None:
                put(IS, "PretaxIncome", ni + (tax or 0.0))
            elif tax is None and ni is not None and ptx is not None:
                put(IS, "TaxProvision", ptx - ni)
            elif ni is None and ptx is not None and tax is not None:
                put(IS, "NetIncome", ptx - tax)
            if get(IS, "NetIncomeContinuousOperations") is None and get(IS, "NetIncome") is not None \
                    and get(IS, "NetIncomeDiscontinuousOperations") is None:
                put(IS, "NetIncomeContinuousOperations", get(IS, "NetIncome"))

            dda = get(IS, "DepreciationAmortizationDepletion")
            if dda is None:
                dda = get(CF, "DepreciationAmortizationDepletion")
                if dda is None:  # banks/industrials that split the CF add-backs
                    dda = _sum0(get, CF, ["Depreciation", "AmortizationOfIntangibles",
                                          "DepreciationAndAmortization"])
                put(IS, "DepreciationAmortizationDepletion", dda)
            ptx = get(IS, "PretaxIncome")
            if get(IS, "EBIT") is None and ptx is not None:
                ie = get(IS, "InterestExpenseNonOperating")
                ii = get(IS, "InterestIncomeNonOperating")
                put(IS, "EBIT", ptx + (ie or 0.0) - (ii or 0.0))
            if get(IS, "EBITDA") is None and get(IS, "EBIT") is not None \
                    and get(IS, "DepreciationAmortizationDepletion") is not None:
                put(IS, "EBITDA", get(IS, "EBIT") + get(IS, "DepreciationAmortizationDepletion"))
            # EPS mirror: shells print one "basic and diluted" figure -- when
            # only one side mapped, the other equals it (no dilutives).
            if get(IS, "DilutedEPS") is None and get(IS, "BasicEPS") is not None:
                put(IS, "DilutedEPS", get(IS, "BasicEPS"))
            if get(IS, "BasicEPS") is None and get(IS, "DilutedEPS") is not None:
                put(IS, "BasicEPS", get(IS, "DilutedEPS"))
            # share count from the EPS the filer printed (GF-style computation)
            if get(IS, "DilutedAverageShares") is None:
                eps, ni = get(IS, "DilutedEPS"), get(IS, "NetIncome")
                if eps is not None and ni is not None and abs(eps) > 0.01:
                    put(IS, "DilutedAverageShares", round(ni / eps))

        # ---------------- balance sheet ----------------
        if BS in has_stmt:
            # cash aggregate
            if get(BS, "CashCashEquivalentsAndShortTermInvestments") is None:
                put(BS, "CashCashEquivalentsAndShortTermInvestments",
                    _sum0(get, BS, ["CashAndCashEquivalents", "ShortTermInvestments"]))
            if get(BS, "Receivables") is None:
                put(BS, "Receivables", _sum0(get, BS, [
                    "AccountsReceivable", "NotesReceivable", "LoansReceivable",
                    "OtherReceivables"]))
            if get(BS, "Inventory") is None:
                put(BS, "Inventory", _sum0(get, BS, [
                    "RawMaterials", "WorkInProcess", "FinishedGoods",
                    "InventoriesAdjustmentsAllowances", "OtherInventories"]))
            if get(BS, "PayablesAndAccruedExpenses") is None:
                put(BS, "PayablesAndAccruedExpenses", _sum0(get, BS, [
                    "AccountsPayable", "CurrentAccruedExpenses"]))
            if get(BS, "CurrentDebtAndCapitalLeaseObligation") is None:
                put(BS, "CurrentDebtAndCapitalLeaseObligation", _sum0(get, BS, [
                    "CurrentDebt", "CurrentCapitalLeaseObligation"]))
            if get(BS, "LongTermDebtAndCapitalLeaseObligation") is None:
                put(BS, "LongTermDebtAndCapitalLeaseObligation", _sum0(get, BS, [
                    "LongTermDebt", "LongTermCapitalLeaseObligation"]))
            if get(BS, "CurrentDeferredLiabilities") is None:
                put(BS, "CurrentDeferredLiabilities", _sum0(get, BS, [
                    "CurrentDeferredRevenue", "CurrentDeferredTaxesLiabilities"]))
            if get(BS, "NonCurrentDeferredLiabilities") is None:
                put(BS, "NonCurrentDeferredLiabilities", _sum0(get, BS, [
                    "NonCurrentDeferredRevenue", "NonCurrentDeferredTaxesLiabilities"]))
            # PPE: gross - accumulated = net (accumulated stored positive)
            g, acc, npp = get(BS, "GrossPPE"), get(BS, "AccumulatedDepreciation"), get(BS, "NetPPE")
            if npp is None and g is not None and acc is not None:
                put(BS, "NetPPE", g - abs(acc))
            elif g is None and npp is not None and acc is not None:
                put(BS, "GrossPPE", npp + abs(acc))

            # assets = current + non-current (either split missing -> solve)
            ta, ca, nca = get(BS, "TotalAssets"), get(BS, "CurrentAssets"), get(BS, "TotalNonCurrentAssets")
            ta2, ca2, nca2 = _any2of3(ta, ca, nca)
            put(BS, "TotalAssets", ta2 if ta is None else None)
            put(BS, "CurrentAssets", ca2 if ca is None else None)
            put(BS, "TotalNonCurrentAssets", nca2 if nca is None else None)

            tl, cl, ncl = get(BS, "TotalLiabilities"), get(BS, "CurrentLiabilities"), get(BS, "TotalNonCurrentLiabilities")
            tl2, cl2, ncl2 = _any2of3(tl, cl, ncl)
            put(BS, "TotalLiabilities", tl2 if tl is None else None)
            put(BS, "CurrentLiabilities", cl2 if cl is None else None)
            put(BS, "TotalNonCurrentLiabilities", ncl2 if ncl is None else None)

            # equity family
            te = get(BS, "TotalEquityGrossMinorityInterest")
            se, mi = get(BS, "StockholdersEquity"), get(BS, "MinorityInterest")
            if te is None and se is not None:
                put(BS, "TotalEquityGrossMinorityInterest", se + (mi or 0.0))
            elif se is None and te is not None:
                put(BS, "StockholdersEquity", te - (mi or 0.0))
            # balance identity: A = L + E (fill the single missing anchor)
            ta, tl = get(BS, "TotalAssets"), get(BS, "TotalLiabilities")
            te = get(BS, "TotalEquityGrossMinorityInterest")
            ta2, tl2, te2 = _any2of3(ta, tl, te)
            put(BS, "TotalAssets", ta2 if ta is None else None)
            put(BS, "TotalLiabilities", tl2 if tl is None else None)
            put(BS, "TotalEquityGrossMinorityInterest", te2 if te is None else None)
            if get(BS, "StockholdersEquity") is None and get(BS, "TotalEquityGrossMinorityInterest") is not None:
                put(BS, "StockholdersEquity",
                    get(BS, "TotalEquityGrossMinorityInterest") - (get(BS, "MinorityInterest") or 0.0))

            # "Other ..." plugs: total minus named details (never below 0)
            if get(BS, "OtherCurrentAssets") is None:
                put(BS, "OtherCurrentAssets", _plug(get(BS, "CurrentAssets"), [
                    get(BS, "CashCashEquivalentsAndShortTermInvestments"),
                    get(BS, "Receivables"), get(BS, "Inventory")]))
            if get(BS, "OtherCurrentLiabilities") is None:
                put(BS, "OtherCurrentLiabilities", _plug(get(BS, "CurrentLiabilities"), [
                    get(BS, "PayablesAndAccruedExpenses"),
                    get(BS, "CurrentDebtAndCapitalLeaseObligation"),
                    get(BS, "CurrentDeferredLiabilities"), get(BS, "TotalTaxPayable")]))
            if get(BS, "OtherNonCurrentAssets") is None:
                put(BS, "OtherNonCurrentAssets", _plug(get(BS, "TotalNonCurrentAssets"), [
                    get(BS, "NetPPE"), get(BS, "Goodwill"), get(BS, "OtherIntangibleAssets"),
                    get(BS, "InvestmentsAndAdvances")]))
            if get(BS, "OtherNonCurrentLiabilities") is None:
                put(BS, "OtherNonCurrentLiabilities", _plug(get(BS, "TotalNonCurrentLiabilities"), [
                    get(BS, "LongTermDebtAndCapitalLeaseObligation"),
                    get(BS, "NonCurrentDeferredLiabilities"),
                    get(BS, "NonCurrentPensionAndOtherPostretirementBenefitPlans")]))

        # ---------------- cash flow ----------------
        if CF in has_stmt:
            if get(CF, "ChangeInWorkingCapital") is None:
                put(CF, "ChangeInWorkingCapital", _sum0(get, CF, [
                    "ChangeInReceivables", "ChangeInInventory", "ChangeInPrepaidAssets",
                    "ChangeInPayablesAndAccruedExpense", "ChangeInOtherWorkingCapital"]))
            if get(CF, "NetIncomeFromContinuingOperations") is None:
                put(CF, "NetIncomeFromContinuingOperations", get(IS, "NetIncome"))
            if get(CF, "CapitalExpenditure") is None and get(CF, "PurchaseOfPPE") is not None:
                put(CF, "CapitalExpenditure", -abs(get(CF, "PurchaseOfPPE")))
            if get(CF, "NetIssuancePaymentsOfDebt") is None:
                put(CF, "NetIssuancePaymentsOfDebt", _sum0(get, CF, [
                    "IssuanceOfDebt", "RepaymentOfDebt"]))
            ocf = get(CF, "OperatingCashFlow")
            if get(CF, "FreeCashFlow") is None and ocf is not None \
                    and get(CF, "CapitalExpenditure") is not None:
                put(CF, "FreeCashFlow", ocf + get(CF, "CapitalExpenditure"))
            elif get(CF, "FreeCashFlow") is None and ocf is not None \
                    and get(CF, "PurchaseOfPPE") is None:
                put(CF, "FreeCashFlow", ocf)  # no capex reported
            # cash rollforward: end = begin + net change (any 2 of 3)
            beg, chg, end = get(CF, "BeginningCashPosition"), get(CF, "ChangesInCash"), get(CF, "EndCashPosition")
            end2, beg2, chg2 = _any2of3(end, beg, chg)  # end = beg + chg
            put(CF, "EndCashPosition", end2 if end is None else None)
            put(CF, "BeginningCashPosition", beg2 if beg is None else None)
            put(CF, "ChangesInCash", chg2 if chg is None else None)
            if get(CF, "ChangesInCash") is None:
                flows = _sum0(get, CF, ["OperatingCashFlow", "InvestingCashFlow",
                                        "FinancingCashFlow", "EffectOfExchangeRateChanges"])
                put(CF, "ChangesInCash", flows)
            # flows identity solve-for-one: chg = OCF + ICF + FCF + FX
            chg = get(CF, "ChangesInCash")
            if chg is not None:
                flow_keys = ["OperatingCashFlow", "InvestingCashFlow", "FinancingCashFlow"]
                missing = [k for k in flow_keys if get(CF, k) is None]
                if len(missing) == 1:
                    known = sum(get(CF, k) for k in flow_keys if get(CF, k) is not None)
                    fx = get(CF, "EffectOfExchangeRateChanges") or 0.0
                    put(CF, missing[0], chg - known - fx)

        if len(cur) == before:
            break
    return new


# ---------------------------------------------------------------------------
# Zero-fill: leaf template keys only. A key here is safe to call 0 when its
# statement parsed and anchored -- these are DETAIL rows a filer omits exactly
# when the amount is nil. Aggregates/anchors/per-share stay out: those must be
# mapped or derived, never assumed.
# ---------------------------------------------------------------------------

ZEROABLE_KEYS: dict[str, set[str]] = {
    IS: {"CostOfRevenue", "SellingGeneralAndAdministration", "ResearchAndDevelopment",
         "OtherOperatingExpenses", "InterestIncomeNonOperating",
         "InterestExpenseNonOperating", "NetNonOperatingInterestIncomeExpense",
         "OtherIncomeExpense", "TaxProvision", "NetIncomeDiscontinuousOperations",
         "MinorityInterests", "DepreciationAmortizationDepletion"},
    BS: {"CashAndCashEquivalents", "ShortTermInvestments", "AccountsReceivable",
         "NotesReceivable", "LoansReceivable", "OtherReceivables", "Receivables",
         "RawMaterials", "WorkInProcess", "InventoriesAdjustmentsAllowances",
         "FinishedGoods", "OtherInventories", "Inventory", "OtherCurrentAssets",
         "InvestmentsAndAdvances", "LandAndImprovements", "BuildingsAndImprovements",
         "MachineryFurnitureEquipment", "ConstructionInProgress", "GrossPPE",
         "AccumulatedDepreciation", "NetPPE", "OtherIntangibleAssets", "Goodwill",
         "OtherNonCurrentAssets", "AccountsPayable", "TotalTaxPayable", "OtherPayable",
         "CurrentAccruedExpenses", "PayablesAndAccruedExpenses", "CurrentDebt",
         "CurrentCapitalLeaseObligation", "CurrentDebtAndCapitalLeaseObligation",
         "CurrentDeferredRevenue", "CurrentDeferredTaxesLiabilities",
         "CurrentDeferredLiabilities", "OtherCurrentLiabilities", "LongTermDebt",
         "LongTermCapitalLeaseObligation", "LongTermDebtAndCapitalLeaseObligation",
         "NonCurrentPensionAndOtherPostretirementBenefitPlans",
         "NonCurrentDeferredRevenue", "NonCurrentDeferredLiabilities",
         "NonCurrentDeferredTaxesLiabilities", "OtherNonCurrentLiabilities",
         "CommonStock", "PreferredStock", "GainsLossesNotAffectingRetainedEarnings",
         "AdditionalPaidInCapital", "TreasuryStock", "MinorityInterest"},
    CF: {"DepreciationAmortizationDepletion", "ChangeInReceivables", "ChangeInInventory",
         "ChangeInPrepaidAssets", "ChangeInPayablesAndAccruedExpense",
         "ChangeInOtherWorkingCapital", "ChangeInWorkingCapital",
         "StockBasedCompensation", "AssetImpairmentCharge",
         "CashFromDiscontinuedOperatingActivities", "OtherNonCashItems",
         "PurchaseOfPPE", "SaleOfPPE", "PurchaseOfBusiness", "PurchaseOfInvestment",
         "SaleOfInvestment", "NetIntangiblesPurchaseAndSale",
         "CashFromDiscontinuedInvestingActivities", "NetOtherInvestingChanges",
         "IssuanceOfCapitalStock", "RepurchaseOfCapitalStock",
         "NetPreferredStockIssuance", "IssuanceOfDebt", "RepaymentOfDebt",
         "NetIssuancePaymentsOfDebt", "CashDividendsPaid", "NetOtherFinancingCharges",
         "EffectOfExchangeRateChanges", "CapitalExpenditure"},
}

# A statement may only be zero-filled when an ANCHOR came through -- proof the
# face was really parsed, not an empty/prose section. Any mapped STRUCTURAL
# total qualifies (a fund that prints "Net assets" but no "Total assets" line
# is still a really-parsed balance sheet).
_ANCHORS: dict[str, tuple[str, ...]] = {
    IS: ("NetIncome", "TotalRevenue", "PretaxIncome", "OperatingIncome",
         "OperatingExpense"),
    BS: ("TotalAssets", "TotalLiabilities", "StockholdersEquity",
         "TotalEquityGrossMinorityInterest", "CurrentAssets"),
    CF: ("OperatingCashFlow", "ChangesInCash", "FinancingCashFlow",
         "InvestingCashFlow", "EndCashPosition"),
}


# Rows that do not APPLY to a bank's statements (banks report a liquidity-
# ordered balance sheet with no current/non-current split, and no
# COGS/operating-income presentation) -- excluded from a bank's fill
# denominator instead of faked.
BANK_NA_KEYS: set[tuple[str, str]] = {
    (IS, "OperatingExpense"), (IS, "OperatingIncome"),
    (BS, "CurrentAssets"), (BS, "TotalNonCurrentAssets"),
    (BS, "CurrentLiabilities"), (BS, "TotalNonCurrentLiabilities"),
    (BS, "CashCashEquivalentsAndShortTermInvestments"),
}


def fill_metric_keys(is_bank: bool) -> list[tuple[str, str]]:
    """The (stmt, key) cells that COUNT toward a company's template fill rate.
    Banks also drop the industrial balance-sheet leaves: a bank's face never
    disaggregates them (they're notes-only), so they can't be filled honestly."""
    tmpl = template_keys(include_bank=is_bank)
    keys = [(s, k) for s, ks in tmpl.items() for k in ks]
    if is_bank:
        keys = [(s, k) for (s, k) in keys if (s, k) not in BANK_NA_KEYS
                and not (s == BS and k in ZEROABLE_KEYS[BS])]
    return keys


def zero_fill(vals: dict[tuple[str, str], float], *, has_stmt: set[str],
              is_bank: bool) -> dict[tuple[str, str], float]:
    """Return {(stmt, key): 0.0} for every still-empty leaf template key of an
    anchored statement. Bank-only keys are never zero-filled for non-banks
    (they aren't rendered); for banks only MoneyMarketInvestments may zero
    (a genuinely optional disclosure) -- a bank missing GrossLoan is a mapping
    failure, not a zero."""
    tmpl = template_keys(include_bank=False)   # bank rows never bulk zero-fill
    out: dict[tuple[str, str], float] = {}
    for stmt in (IS, BS, CF):
        if stmt not in has_stmt:
            continue
        if is_bank and stmt == BS:
            # a bank's face doesn't disaggregate the industrial rows, but the
            # amounts are NOT zero (payables etc. live in Other liabilities'
            # notes -- QuoteMedia proves them nonzero). Leave them blank.
            continue
        if not any(vals.get((stmt, a)) is not None for a in _ANCHORS[stmt]):
            continue
        for key in tmpl[stmt]:
            if key in ZEROABLE_KEYS[stmt] and (stmt, key) not in vals:
                out[(stmt, key)] = 0.0
    if is_bank and BS in has_stmt and vals.get((BS, "TotalAssets")) is not None \
            and (BS, "MoneyMarketInvestments") not in vals:
        out[(BS, "MoneyMarketInvestments")] = 0.0
    return out
