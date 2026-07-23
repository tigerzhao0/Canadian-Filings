"""run.py --step 4: map the individual line items (step 3, raw_line_items) onto
the canonical GuruFocus/QuoteMedia schema.

Writes to pdf_financials.db:
- statement_lines   -- canonical keys only, overlapping fiscal years merged with
                       NEWEST-SOURCE-PDF-WINS (restated figures are current)
- line_items_full   -- EVERY parsed line, mapped or not, with provenance
                       (nothing from the PDF is lost)
- companies / company_years / statement_line_quality / pdf_llm_raw / pdf_llm_status

Matching order per label (see rule_extract.match_label_ctx):
compound "section > label" (context disambiguates "Loans" under Interest income
vs under Assets) -> bare exact alias -> suffix salvage (prose-glued labels) ->
guarded fuzzy. Bank-only keys are gated on the statement actually looking like
a bank's; EPS keys require per-share context.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

import derive as derive_mod
from financials_pipeline import FinancialsStore, _coerce_numeric
from llm_extract import DEFAULT_VOCAB, NO_SCALE_KEYS, _load_vocab
from rule_extract import (
    BANK_ONLY_KEYS, CONFIDENCE_REVIEW_THRESHOLD, MIN_ROWS_OK, _BANK_RE,
    _EPS_KEYS, _KIND_RANK, _identity_checks, _statement_confidence,
    build_alias_index, build_compound_index, match_label_ctx,
)

_PER_SHARE_LABEL_RE = re.compile(r"per share|per unit|in dollars|per common share", re.I)

# Keys where a filer splits ONE concept across several lines (legal fees +
# consulting + travel + office... are all G&A) -- sibling matches within the
# same (pdf_year, statement) SUM instead of best-one-wins. Totals/EPS keys
# must never aggregate.
AGGREGATE_KEYS = {
    "SellingGeneralAndAdministration", "OtherOperatingExpenses",
    "OtherIncomeExpense", "OtherReceivables", "OtherPayable",
    "OtherCurrentAssets", "OtherCurrentLiabilities", "OtherNonCurrentAssets",
    "OtherNonCurrentLiabilities", "OtherNonCashItems", "NetOtherInvestingChanges",
    "NetOtherFinancingCharges", "StockBasedCompensation",
    "GeneralAndAdministrativeExpense",
    # a CF's working-capital block often splits one bucket over several lines
    "ChangeInReceivables", "ChangeInPayablesAndAccruedExpense",
    "ChangeInPrepaidAssets", "ChangeInOtherWorkingCapital",
    # multiple debt instruments (credit facility + subordinated debt, etc.)
    # each print their own net draw/repayment line -- sum, don't pick one
    # (confirmed on RAY/Stingray: "credit facilities" + "subordinated debt").
    "NetIssuancePaymentsOfDebt", "LongTermDebt", "CurrentDebt",
    # same pattern on the BS face: "broadcast licences" + "other intangibles"
    # are two distinct intangible-asset lines that both belong in the one
    # generic template row (confirmed on RAY/Stingray).
    "OtherIntangibleAssets",
    # "Property and equipment" + "Right-of-use assets on leases" both belong
    # in the one PP&E template row (identity-verified on RAY/Stingray).
    "NetPPE",
    # the finance-expense note's "Interest expense and standby fees" +
    # "Interest expense on lease liabilities" together ARE GuruFocus's
    # Interest Expense figure (exact-match verified on RAY/Stingray).
    "InterestExpenseNonOperating",
    # small-cap equity sections routinely split "contributed capital beyond
    # par" over multiple sibling lines (Contributed Surplus + Reserves +
    # Reserve for Warrants + Share-based Payment Reserve) that must all sum
    # into the one template row -- identity-verified on two real filings:
    # ABA 2023 (Share Capital 5,894,172 + Contributed Surplus 122,500 +
    # Reserves 1,655,483 + Deficit -6,199,841 = Equity Total 1,472,314) and
    # ACAP.P 2023 (317,249 + 14,248 + 48,083 - 167,879 = 211,701).
    "AdditionalPaidInCapital",
    # same pattern for OCI-style equity reserves (translation/hedging/
    # revaluation) -- a company rarely has more than one, but when it does
    # (AD.UN, BKMT, ITR, ORIO: ~2% of cases) they're distinct sibling
    # components of the one OCI template row, not alternates.
    "GainsLossesNotAffectingRetainedEarnings",
}

# Prose guard: a "statement" whose lines read like sentences is a mis-isolated
# MD&A/MRFP region (MID.UN), not a statement -- keep the lines in
# line_items_full but never let them into canonical statement_lines.
_PROSE_MEDIAN_WORDS = 9


def _reroute_statement(stmt: str, by_line: dict[int, list[dict]],
                       alias_index) -> str:
    """Step 2 sometimes files a block under the wrong statement (TNT.UN's
    'cash_flow' text is actually its balance sheet). Vote each candidate type
    by exact-alias hits over the block's labels; reroute only on a decisive
    win (>=5 hits and >=2x the current type's score)."""
    from rule_extract import _normalize_label
    labels = [_normalize_label(cells[0]["label"] or "") for cells in by_line.values()]
    scores = {cand: sum(1 for lb in labels if lb in alias_index.get(cand, {}))
              for cand in ("income_statement", "balance_sheet", "cash_flow")}
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    if best != stmt and scores[best] >= 5 and scores[best] >= 2 * max(scores[stmt], 1):
        return best
    return stmt


def _looks_like_prose(by_line: dict[int, list[dict]]) -> bool:
    if not by_line:
        return False
    wc = sorted(len((cells[0]["label"] or "").split()) for cells in by_line.values())
    median_words = wc[len(wc) // 2]
    if median_words >= _PROSE_MEDIAN_WORDS:
        return True
    valued = sum(1 for cells in by_line.values()
                 if any(c["value_printed"] is not None for c in cells))
    return len(by_line) >= 8 and valued < 0.3 * len(by_line)

# Contra-asset keys the PDFs print as parenthesized deductions (negative) but
# QuoteMedia/GuruFocus store as POSITIVE magnitudes. Also expense-magnitude
# income-statement keys: some filers print their expense subtotal as a
# negative/parenthesized figure (deducted from revenue on the page) rather
# than a bare positive cost -- confirmed on AGMR (2025 "Total operating
# expenses" printed as -4,247,593). derive.py's identities (GrossProfit =
# Revenue - CostOfRevenue, OperatingIncome = GrossProfit - OperatingExpense)
# assume these are positive magnitudes like every other expense-type row
# (see the f6b4cee TaxProvision/InterestExpense commit for the established
# convention); a source-negative value flips OperatingIncome's sign instead
# of just reducing it. abs() is a no-op for the common positive-printed case.
_ABS_VALUE_KEYS = {"AllowanceForLoansAndLeaseLosses", "AccumulatedDepreciation",
                   "CostOfRevenue", "OperatingExpense",
                   "SellingGeneralAndAdministration", "ResearchAndDevelopment",
                   "OtherOperatingExpenses", "DepreciationAmortizationDepletion"}
# Attribution keys stored NEGATED by QM/GuruFocus (income attributable to
# non-controlling interests is a deduction from group income: screenshot shows
# "Other Income (Minority Interest)" as -5, -7).
_NEGATE_KEYS = {"MinorityInterests"}

# Bottom-line result keys where some filers convey the sign PURELY through
# the caption word ("Net loss for the year   7,121", no minus sign or
# parens -- the page relies on the reader knowing "loss" means negative)
# rather than through the printed number itself. Root-caused via a
# ground-truth diff against QuoteMedia (ASCU 2022: our NetIncome=+7,121 vs
# their -7,121 -- exact magnitude match); confirmed the whole statement's
# own arithmetic sums to +7,121 (a magnitude), and the line is captioned
# "Loss and comprehensive loss for the year" with no negative sign anywhere
# on the page. A label that mixes in "income"/"earnings"/"gain" (e.g. "Net
# income (loss) for the year") is the OPPOSITE case -- an alternating
# caption where the source's own signed number must be trusted as printed
# -- so this only fires on an unambiguous, pure "loss" caption.
_NET_INCOME_KEYS = {"NetIncome", "NetIncomeFromContinuingOperations",
                    "NetIncomeContinuousOperations", "NetIncomeCommonStockholders",
                    "NetIncomeFromContinuingAndDiscontinuedOperation",
                    "NetIncomeDiscontinuousOperations"}
_PURE_LOSS_LABEL_RE = re.compile(r"\bloss\b", re.I)
_INCOME_OR_GAIN_RE = re.compile(r"\b(?:income|earnings|gain)\b", re.I)


def _is_pure_loss_label(label: str) -> bool:
    return bool(_PURE_LOSS_LABEL_RE.search(label)) and not _INCOME_OR_GAIN_RE.search(label)

# QuoteMedia stores the SAME value under several synonym keys (verified: their
# values are identical for these). After mapping, replicate to the sibling keys
# that are still absent so the xlsx template rows referencing either fill in.
# STRICT equality groups only -- Basic/Diluted average shares etc. are related
# but NOT equal and must never be fanned out.
EQUIVALENT_FANOUT: dict[str, list[str]] = {
    "OperatingCashFlow": ["CashFlowFromContinuingOperatingActivities"],
    "InvestingCashFlow": ["CashFlowFromContinuingInvestingActivities"],
    "FinancingCashFlow": ["CashFlowFromContinuingFinancingActivities"],
    # NOTE: NetIncome is NOT fanned to NetIncomeCommonStockholders -- NICS is
    # net income minus PREFERRED dividends (RY 2018: 12,115 vs NI 12,400), so
    # they differ whenever preferred shares exist.
    "NetIncome": ["NetIncomeFromContinuingAndDiscontinuedOperation"],
    "BasicEPS": ["BasicContinuousOperations"],
    "DilutedEPS": ["DilutedContinuousOperations"],
    # NOTE: CashAndDueFromBanks is NOT fanned to CashAndCashEquivalents -- for
    # banks QM's CashAndCashEquivalents = cash + interest-bearing deposits
    # (56,723 + 66,020 = 122,743 for RY 2024), a different aggregate.
    "TotalEquityGrossMinorityInterest": ["TotalEquityGrossMinority"],
    "DepreciationAndAmortization": ["DepreciationAmortizationDepletion"],
    "DepreciationAmortizationDepletion": ["DepreciationAndAmortization"],
    # "Share capital" (one combined line, common companies outside financial
    # services) maps to CapitalStock, which isn't its own Sheet1 row --
    # CommonStock is the rendered template row. Fanned, not merged: a
    # CommonStock/PreferredStock SPLIT (when the raw statement shows one)
    # still wins on its own via direct mapping, since fanout only fills an
    # EMPTY slot (confirmed on RAY/Stingray: "Share capital" 322.366 -> both).
    "CapitalStock": ["CommonStock"],
}

# A raw label that explicitly says "basic and diluted" (one combined figure,
# common when a company has no dilutive securities so the two are equal by
# definition, e.g. "Weighted average number of common shares outstanding
# (basic and diluted)") maps to only ONE of the Basic/Diluted pair via the
# normal matcher, leaving its sibling empty even though the source states
# they're identical -- confirmed at 12,692 ticker-years corpus-wide. Safe to
# duplicate the value across, unlike a bare "Basic" or "Diluted" label alone
# (which must NOT be assumed equal to its sibling).
_BASIC_AND_DILUTED_RE = re.compile(r"\bbasic\b\s*(?:and|&|/)\s*diluted\b", re.I)
_BASIC_DILUTED_SIBLING = {
    "BasicEPS": "DilutedEPS", "DilutedEPS": "BasicEPS",
    "BasicAverageShares": "DilutedAverageShares",
    "DilutedAverageShares": "BasicAverageShares",
}


def _load_raw_line_items(src_db: str, tickers: set[str] | None,
                         limit: int | None) -> tuple[dict, dict, dict]:
    """Returns (grouped, identity, hints):
      grouped[ticker][pdf_year][stmt] = [row dicts, ordered by line_no]
      identity[ticker] = {company_name, exchange}
      hints[(ticker, pdf_year)] = {unit_scale_hint, pdf_url}
    """
    conn = sqlite3.connect(src_db)
    try:
        try:
            cur = conn.execute(
                "SELECT ticker, pdf_year, statement_type, line_no, section, zone, "
                "label, synthetic, col_year, period_end, value_printed, unit_scale, "
                "currency FROM raw_line_items "
                "ORDER BY ticker, pdf_year DESC, statement_type, line_no, col_year")
        except sqlite3.OperationalError:
            raise SystemExit(
                "raw_line_items is empty/missing -- run the line-item parsing "
                "step first (run.py --step 3).")
        names = [c[0] for c in cur.description]
        grouped: dict = {}
        for rec in cur.fetchall():
            row = dict(zip(names, rec))
            if tickers and row["ticker"] not in tickers:
                continue
            grouped.setdefault(row["ticker"], {}) \
                   .setdefault(row["pdf_year"], {}) \
                   .setdefault(row["statement_type"], []).append(row)
        identity: dict = {}
        for tk, name, exch in conn.execute(
                "SELECT ticker, company_name, exchange FROM filings"):
            identity[tk] = {"company_name": name, "exchange": exch}
        hints: dict = {}
        try:
            for tk, fy, hint in conn.execute(
                    "SELECT ticker, fiscal_year, unit_scale_hint FROM pdf_extractions"):
                hints[(tk, fy)] = {"unit_scale_hint": hint}
        except sqlite3.OperationalError:
            pass
        for tk, fy, url in conn.execute(
                "SELECT ticker, fiscal_year, pdf_url FROM filing_pdfs"):
            hints.setdefault((tk, fy), {})["pdf_url"] = url
    finally:
        conn.close()
    if limit:
        grouped = dict(list(grouped.items())[:limit])
    return grouped, identity, hints


# ---- per-ticker worker (runs in a subprocess) --------------------------
# Every structure below is keyed with the ticker INCLUDED in the tuple
# (ticker, col_year, stmt, key) exactly as in the original single-threaded
# loop, even though a worker only ever handles one ticker -- this keeps the
# candidate-picking / fanout / derive / zero-fill / fill-rate logic below
# byte-identical to the pre-parallelization version (nothing here reads or
# writes another ticker's data; the merge-newest-pdf-wins tie-break, the
# bank/revenue flags, and the derived-metric fixpoint are ALL scoped to a
# single ticker's own (col_year) slots), so splitting the outer "for ticker
# in grouped" loop into independent process-pool tasks changes nothing about
# what gets computed -- only that it now happens in parallel.
_WORKER_ALIAS_INDEX = None
_WORKER_COMPOUND_INDEX = None


def _init_worker(vocab_path: str) -> None:
    """ProcessPoolExecutor initializer: build the (fairly expensive) alias/
    compound indexes ONCE per worker process, not once per ticker task."""
    global _WORKER_ALIAS_INDEX, _WORKER_COMPOUND_INDEX
    vocab = _load_vocab(Path(vocab_path))
    _WORKER_ALIAS_INDEX = build_alias_index(vocab)
    _WORKER_COMPOUND_INDEX = build_compound_index(vocab)
    # The label matcher is a PURE function of (label, section, zone,
    # statement_type) once the alias/compound indexes are fixed -- and those
    # indexes are built exactly once here per worker and never mutated. Reset
    # the per-process memo cache on (re-)init so it can never carry a stale
    # result from a different vocab; in practice a worker inits once and lives
    # for the whole run, accumulating hits across every ticker it handles.
    _match_cached.cache_clear()


@lru_cache(maxsize=None)
def _match_cached(label, section, zone, statement_type):
    """Per-worker memoized wrapper around match_label_ctx. Keyed on the
    4-tuple only (the alias/compound indexes are dicts -- unhashable, and
    constant per worker -- so they're read from the process globals rather
    than passed as cache args). Financial-statement labels are massively
    repetitive across issuers/years (~4:1 total-to-distinct), and the
    expensive difflib fuzzy fallback fires on the most-repeated long-tail
    labels, so this eliminates the bulk of redundant matching work. Output
    is provably identical to calling match_label_ctx directly."""
    return match_label_ctx(label, section, zone, statement_type,
                           _WORKER_ALIAS_INDEX, _WORKER_COMPOUND_INDEX)


def _process_ticker(ticker: str, by_pdf: dict, hints: dict, already: set,
                    identity_entry: dict, is_new_company: bool) -> dict:
    # compound_index is read directly by _match_cached from the process
    # globals; only alias_index is still needed locally (for _reroute_statement).
    alias_index = _WORKER_ALIAS_INDEX

    company_rows: list[dict] = []
    year_meta: dict[tuple, dict] = {}     # (ticker, col_year) -> meta
    picked: dict[tuple, tuple] = {}       # (ticker, col_year, stmt, key) -> (value, rank, conf)
    slot_src: dict[tuple, int] = {}       # which pdf_year set each slot
    full_rows: list[dict] = []
    quality_rows: list[dict] = []
    status_rows: list[dict] = []
    raw_rows: list[dict] = []
    confidences: list[float] = []
    parsed_ok = skipped = needs_review = 0
    balance_pass = balance_checked = 0
    mapped_labels = total_labels = 0
    stmt_cover: dict[tuple, set] = {}     # (ticker, col_year) -> statements parsed
    bank_tickers: set[str] = set()
    rev_labelled: set[tuple] = set()      # (ticker, col_year) whose IS mentions revenue/sales
    prov: dict[tuple, str] = {}           # slot -> 'derived' | 'absent_on_face'
    combined_basic_diluted: set[tuple] = set()  # slots whose raw label said "basic and diluted"
    log_lines: list[str] = []

    if True:
        latest_currency = None
        for pdf_year in sorted(by_pdf, reverse=True):     # newest PDF first
            hint = hints.get((ticker, pdf_year), {})
            for stmt, rows in by_pdf[pdf_year].items():
                if (ticker, pdf_year, stmt) in already:
                    skipped += 1
                    continue
                # ---- statement-instance context ----
                blob = " ".join(f"{r['section'] or ''} {r['label']}" for r in rows)
                is_bank = bool(_BANK_RE.search(blob))
                scale = next((r["unit_scale"] for r in rows if r["unit_scale"]), None)
                flags: list[str] = []
                scale_conf = 0.95
                if scale is None:
                    scale = hint.get("unit_scale_hint")
                    if scale is not None:
                        scale_conf = 0.80
                        flags.append("scale_from_doc_hint")
                if scale is None:
                    scale, scale_conf = 1.0, 0.35
                    flags.append("scale_assumed")
                currency = next((r["currency"] for r in rows if r["currency"]), None)
                if currency and latest_currency is None:
                    latest_currency = currency

                # ---- map each distinct line once, then apply to its year cells ----
                by_line: dict[int, list[dict]] = {}
                for r in rows:
                    by_line.setdefault(r["line_no"], []).append(r)
                by_year: dict[int, dict[str, float]] = {}
                match_confs: list[float] = []
                n_mapped = 0
                prose = _looks_like_prose(by_line)
                if prose:
                    flags.append("prose_rows")
                else:
                    routed = _reroute_statement(stmt, by_line, alias_index)
                    if routed != stmt:
                        flags.append(f"rerouted_from_{stmt}")
                        stmt = routed
                    if is_bank:
                        bank_tickers.add(ticker)
                    for r in rows:
                        stmt_cover.setdefault((ticker, r["col_year"]), set()).add(stmt)
                    if stmt == "income_statement" and any(
                            re.search(r"revenue|sales", cells[0]["label"] or "", re.I)
                            for cells in by_line.values()):
                        for r in rows:
                            rev_labelled.add((ticker, r["col_year"]))
                for line_no, cells in sorted(by_line.items()):
                    r0 = cells[0]
                    label, section, zone = r0["label"], r0["section"], r0["zone"]
                    if prose:
                        key, conf, kind = None, 0.0, None
                    else:
                        key, conf, kind = _match_cached(label, section, zone, stmt)
                    if key and key in BANK_ONLY_KEYS and not is_bank:
                        key, conf, kind = None, 0.0, None
                        flags.append("bank_key_on_nonbank")
                    if (key in _EPS_KEYS and kind != "compound"
                            and not _PER_SHARE_LABEL_RE.search(label)
                            and not (section and _PER_SHARE_LABEL_RE.search(section))):
                        key, conf, kind = None, 0.0, None
                        flags.append("eps_without_context")
                    per_share = (key in NO_SCALE_KEYS) or bool(
                        _PER_SHARE_LABEL_RE.search(label))
                    total_labels += 1
                    if key:
                        n_mapped += 1
                        mapped_labels += 1
                        match_confs.append(conf)
                    for cell in cells:
                        y = cell["col_year"]
                        printed = cell["value_printed"]
                        val = None
                        if printed is not None:
                            val = printed if per_share else printed * scale
                            if key in _ABS_VALUE_KEYS and val is not None:
                                val = abs(val)
                            if key in _NEGATE_KEYS and val is not None:
                                val = -val
                            if (key in _NET_INCOME_KEYS and val is not None
                                    and val > 0 and _is_pure_loss_label(label)):
                                val = -val
                            val = _coerce_numeric(val)
                        full_rows.append(dict(
                            ticker=ticker, fiscal_year=y, statement_type=stmt,
                            source_pdf_year=pdf_year, line_no=line_no,
                            section=section, raw_label=label, canonical_key=key,
                            value=val, match_kind=kind))
                        if key and val is not None:
                            slot = (ticker, y, stmt, key)
                            cand = (val, _KIND_RANK.get(kind, 0), conf)
                            if (key in _BASIC_DILUTED_SIBLING
                                    and _BASIC_AND_DILUTED_RE.search(label)):
                                combined_basic_diluted.add(slot)
                            prev = picked.get(slot)
                            if prev is None:
                                picked[slot] = cand
                                slot_src[slot] = pdf_year
                            elif slot_src[slot] == pdf_year and key in AGGREGATE_KEYS:
                                # sibling lines of one concept (legal + consulting
                                # + travel -> G&A): SUM within the same source PDF
                                picked[slot] = (prev[0] + val,
                                                max(prev[1], cand[1]),
                                                min(prev[2], cand[2]))
                            elif (slot_src[slot] == pdf_year
                                  and (prev[1], prev[2]) < (cand[1], cand[2])):
                                # better-quality match within the SAME (newest)
                                # source PDF; an older PDF never overrides.
                                picked[slot] = cand
                            yk = by_year.setdefault(y, {})
                            if key in AGGREGATE_KEYS and key in yk:
                                yk[key] += val
                            else:
                                yk.setdefault(key, val)
                        meta = year_meta.setdefault((ticker, y), {
                            "period_end": None, "currency": None,
                            "source_ref": hint.get("pdf_url")})
                        meta["period_end"] = meta["period_end"] or cell["period_end"]
                        meta["currency"] = meta["currency"] or currency

                # ---- identities + confidence for this statement instance ----
                identity_fail = 0
                for y, bk in by_year.items():
                    f = _identity_checks(bk, stmt)
                    flags.extend(f)
                    identity_fail += len(f)
                if stmt == "balance_sheet" and by_year:
                    balance_checked += 1
                    if "balance_identity_fail" not in flags:
                        balance_pass += 1
                sparse = n_mapped < MIN_ROWS_OK
                if sparse:
                    flags.append("sparse")
                conf_stmt = _statement_confidence(
                    match_confs, scale_conf, identity_fail, by_year, stmt,
                    sorted(by_year), sparse)
                confidences.append(conf_stmt)
                parsed_ok += 1
                if conf_stmt < CONFIDENCE_REVIEW_THRESHOLD:
                    needs_review += 1
                    status = "needs_review"
                else:
                    status = "ok_warnings" if flags else "ok"
                seen_f: set = set()
                flags = [f for f in flags if not (f in seen_f or seen_f.add(f))]
                status_rows.append(dict(ticker=ticker, fiscal_year=pdf_year,
                                        statement_type=stmt, status=status,
                                        reason=(", ".join(flags) or None),
                                        n_lines=n_mapped, confidence=conf_stmt,
                                        doc_type=None))
                raw_rows.append(dict(ticker=ticker, fiscal_year=pdf_year,
                                     statement_type=stmt, model="rules-v2",
                                     prompt_version="rules-v2", unit_scale=scale,
                                     currency=currency,
                                     raw_json=json.dumps({
                                         "flags": flags, "confidence": conf_stmt,
                                         "labels": len(by_line), "mapped": n_mapped})))
                for y, bk in by_year.items():
                    for k in bk:
                        quality_rows.append(dict(ticker=ticker, fiscal_year=y,
                                                 statement_type=stmt, line_item=k,
                                                 confidence=conf_stmt,
                                                 match_kind="mapped"))
                log_lines.append(f"  {ticker} pdf{pdf_year} {stmt:16} "
                                 f"{n_mapped}/{len(by_line)} labels mapped  "
                                 f"conf={conf_stmt:.2f}")
        if is_new_company:
            ident = identity_entry
            company_rows.append(dict(ticker=ticker,
                                     company_name=ident.get("company_name"),
                                     exchange=ident.get("exchange"),
                                     currency=latest_currency,
                                     primary_source="cse_pdf_extract"))

    # Synonym fanout FIRST (known-equivalent data), before derive/zero-fill:
    # fanning CapitalStock -> CommonStock etc. must win the slot before
    # zero-fill's "we truly found nothing" fallback claims it -- zero-fill
    # only fills a slot when it's still EMPTY, so if it runs first on a
    # ZEROABLE_KEYS member (CommonStock is one), the fanout below would find
    # the slot already occupied by a 0 and never apply (confirmed on
    # RAY/Stingray: CommonStock stayed 0 despite CapitalStock=322.366 mapped).
    for (t, y, s, k), v in list(picked.items()):
        for sibling in EQUIVALENT_FANOUT.get(k, []):
            slot = (t, y, s, sibling)
            if slot not in picked:
                picked[slot] = v

    # Same idea, but conditional on the raw label itself saying "basic and
    # diluted" (tracked per-slot above) rather than an unconditional key ->
    # key mapping -- a bare "Basic" label must NOT fan to Diluted.
    for slot in combined_basic_diluted:
        t, y, s, k = slot
        v = picked.get(slot)
        if v is None:
            continue
        sib_slot = (t, y, s, _BASIC_DILUTED_SIBLING[k])
        if sib_slot not in picked:
            picked[sib_slot] = v

    # ---- derivation + zero-fill (identity fixpoint; NEVER overwrites mapped) ----
    # "bank" here = actually MAPPED a bank display key. The looser _BANK_RE
    # text flag over-fires on funds/CPCs and would suppress their zero-fill.
    bank_display = {t for (t, _y, _s, k) in picked
                    if k in ("GrossLoan", "TotalDeposits", "NetInterestIncome",
                             "CashAndDueFromBanks")}
    by_ty: dict[tuple, dict] = {}
    for (t, y, s, k), v in picked.items():
        by_ty.setdefault((t, y), {})[(s, k)] = v[0]
    derived_n = zero_n = 0
    for (t, y), vals in by_ty.items():
        hs = stmt_cover.get((t, y), set())
        if not hs:
            continue
        # Shell rule: an anchored income statement with NO revenue-like label
        # anywhere on its face (CPCs, explorers) really has zero revenue --
        # seed 0 so GrossProfit/OperatingIncome derive instead of staying blank.
        if ("income_statement" in hs
                and ("income_statement", "TotalRevenue") not in vals
                and ("income_statement", "NetIncome") in vals
                and (t, y) not in rev_labelled
                and t not in bank_display):
            slot = (t, y, "income_statement", "TotalRevenue")
            vals[("income_statement", "TotalRevenue")] = 0.0
            picked[slot] = (0.0, 0, 0.30)
            prov[slot] = "absent_on_face"
            zero_n += 1
        newd = derive_mod.derive_year(vals, has_stmt=hs)
        for (s, k), v in newd.items():
            slot = (t, y, s, k)
            if slot not in picked:
                picked[slot] = (_coerce_numeric(v), 0, 0.60)
                prov[slot] = "derived"
                derived_n += 1
        zf = derive_mod.zero_fill({**vals, **newd}, has_stmt=hs,
                                  is_bank=t in bank_display)
        for (s, k), v in zf.items():
            slot = (t, y, s, k)
            if slot not in picked:
                picked[slot] = (v, 0, 0.30)
                prov[slot] = "absent_on_face"
                zero_n += 1
        # SECOND derive pass: the zeros are inputs too (EBITDA = EBIT + 0 DDA,
        # OperatingExpense = SG&A + 0 + 0, cash aggregate = cash + 0 STI ...)
        newd2 = derive_mod.derive_year({**vals, **newd, **zf}, has_stmt=hs)
        for (s, k), v in newd2.items():
            slot = (t, y, s, k)
            if slot not in picked:
                picked[slot] = (_coerce_numeric(v), 0, 0.55)
                prov[slot] = "derived"
                derived_n += 1
    for slot, kind in prov.items():
        t, y, s, k = slot
        quality_rows.append(dict(ticker=t, fiscal_year=y, statement_type=s,
                                 line_item=k, confidence=picked[slot][2],
                                 match_kind=kind))

    # ---- template fill rate (the number the xlsx user actually sees) ----
    fill_by_ticker: dict[str, float] = {}
    for t in sorted({tt for (tt, _y) in by_ty}):
        keys = derive_mod.fill_metric_keys(is_bank=(t in bank_display))
        years = sorted({y for (tt, y) in by_ty if tt == t})
        if not keys or not years:
            continue
        filled = sum(1 for y in years for (s, k) in keys if (t, y, s, k) in picked)
        fill_by_ticker[t] = 100.0 * filled / (len(keys) * len(years))
    line_rows = [dict(ticker=t, fiscal_year=y, statement_type=s, line_item=k,
                      value=v[0]) for (t, y, s, k), v in picked.items()]
    year_rows = [dict(ticker=t, fiscal_year=y, period_end=m["period_end"],
                      currency=m["currency"], source="cse_pdf_extract",
                      source_ref=m["source_ref"])
                 for (t, y), m in year_meta.items()]

    return dict(
        company_rows=company_rows, year_rows=year_rows, line_rows=line_rows,
        full_rows=full_rows, quality_rows=quality_rows, status_rows=status_rows,
        raw_rows=raw_rows, confidences=confidences,
        parsed_ok=parsed_ok, skipped=skipped, needs_review=needs_review,
        balance_pass=balance_pass, balance_checked=balance_checked,
        mapped_labels=mapped_labels, total_labels=total_labels,
        derived_n=derived_n, zero_n=zero_n, fill_by_ticker=fill_by_ticker,
        log_lines=log_lines,
    )


def run_schema_mapping(cfg, *, force: bool = False, limit: int | None = None,
                       tickers: set[str] | None = None, progress=None) -> dict:
    src_db = cfg.get("storage", {}).get("db_path", "output/filings.db")
    fin_db = cfg.get("rules", {}).get("db_path") or "output/pdf_financials.db"
    vocab_path = str(Path(cfg.get("rules", {}).get("vocab_path", DEFAULT_VOCAB)))
    process_workers = int(cfg.get("rules", {}).get(
        "process_workers", min(20, os.cpu_count() or 4)))

    grouped, identity, hints = _load_raw_line_items(src_db, tickers, limit)
    store = FinancialsStore(fin_db)
    already = set() if force else store.completed_statements()
    prior_tickers = store.existing_tickers()
    t0 = time.monotonic()

    # Slice the global already/hints sets down to each ticker's own subset
    # BEFORE dispatch -- keeps each task's pickled payload small (a company's
    # own filings, not the whole backlog's).
    already_by_ticker: dict[str, set] = {}
    for entry in already:
        already_by_ticker.setdefault(entry[0], set()).add(entry)
    hints_by_ticker: dict[str, dict] = {}
    for key in hints:
        hints_by_ticker.setdefault(key[0], {})[key] = hints[key]

    company_rows: list[dict] = []
    year_rows: list[dict] = []
    line_rows: list[dict] = []
    full_rows: list[dict] = []
    quality_rows: list[dict] = []
    status_rows: list[dict] = []
    raw_rows: list[dict] = []
    confidences: list[float] = []
    parsed_ok = skipped = needs_review = 0
    balance_pass = balance_checked = 0
    mapped_labels = total_labels = 0
    derived_n = zero_n = 0
    fill_by_ticker: dict[str, float] = {}

    if progress:
        progress(f"Schema mapping: {len(grouped)} ticker(s) from {src_db} -> {fin_db}  "
                 f"(process_workers={process_workers})")

    with ProcessPoolExecutor(max_workers=process_workers,
                             initializer=_init_worker,
                             initargs=(vocab_path,)) as pool:
        futures = [
            pool.submit(_process_ticker, ticker, by_pdf,
                       hints_by_ticker.get(ticker, {}),
                       already_by_ticker.get(ticker, set()),
                       identity.get(ticker, {}), ticker not in prior_tickers)
            for ticker, by_pdf in grouped.items()
        ]
        for fut in as_completed(futures):
            res = fut.result()
            if progress:
                for line in res["log_lines"]:
                    progress(line)
            company_rows.extend(res["company_rows"])
            year_rows.extend(res["year_rows"])
            line_rows.extend(res["line_rows"])
            full_rows.extend(res["full_rows"])
            quality_rows.extend(res["quality_rows"])
            status_rows.extend(res["status_rows"])
            raw_rows.extend(res["raw_rows"])
            confidences.extend(res["confidences"])
            parsed_ok += res["parsed_ok"]
            skipped += res["skipped"]
            needs_review += res["needs_review"]
            balance_pass += res["balance_pass"]
            balance_checked += res["balance_checked"]
            mapped_labels += res["mapped_labels"]
            total_labels += res["total_labels"]
            derived_n += res["derived_n"]
            zero_n += res["zero_n"]
            fill_by_ticker.update(res["fill_by_ticker"])

    fills = sorted(fill_by_ticker.values())
    fill_median = fills[len(fills) // 2] if fills else 0.0
    if progress and fill_by_ticker:
        worst = sorted(fill_by_ticker.items(), key=lambda kv: kv[1])[:5]
        progress(f"  fill rate: median {fill_median:.0f}%  "
                 f"(worst: {', '.join(f'{t} {p:.0f}%' for t, p in worst)})")

    # A ticker's lines are always rebuilt whole from raw_line_items, so purge
    # first -- otherwise keys mapped/derived/zero-filled on a previous run but
    # not this one survive as stale ghosts (upsert never deletes). BUG FIXED:
    # this used to purge only {r["ticker"] for r in company_rows} -- but
    # company_rows only contains BRAND-NEW tickers (`if ticker not in
    # prior_tickers`), so any ticker already known from an earlier run was
    # NEVER purged on a subsequent --force rerun. Confirmed: RY's
    # CashAndCashEquivalents/ShortTermInvestments stayed frozen at a stale
    # 'absent_on_face' 0 from before the bank-BS zero-fill guard was added,
    # surviving multiple later --force reruns untouched. Purge every ticker
    # actually being rebuilt THIS run (grouped == every ticker with raw
    # line items processed), not just newly-discovered ones.
    store.purge_ticker_lines(set(grouped.keys()))
    store.bulk_upsert_companies(company_rows)
    store.bulk_upsert_company_years(year_rows)
    store.bulk_upsert_statement_lines(line_rows)
    store.bulk_upsert_line_items_full(full_rows)
    store.bulk_upsert_line_quality(quality_rows)
    store.bulk_upsert_llm_raw(raw_rows)
    store.bulk_upsert_llm_status(status_rows)
    store.close()

    return {"tickers": len(grouped), "parsed_ok": parsed_ok,
            "needs_review": needs_review, "skipped": skipped,
            "companies": len(company_rows), "years": len(year_rows),
            "lines": len(line_rows), "full_lines": len(full_rows),
            "derived_lines": derived_n, "zero_filled_lines": zero_n,
            "fill_median_pct": fill_median,
            "fill_by_ticker": fill_by_ticker,
            "label_map_rate_pct": (mapped_labels / total_labels * 100)
                                  if total_labels else 0.0,
            "balance_identity_pass": balance_pass,
            "balance_identity_checked": balance_checked,
            "mean_confidence": (sum(confidences) / len(confidences))
                               if confidences else 0.0,
            "elapsed": time.monotonic() - t0, "db_path": fin_db}
