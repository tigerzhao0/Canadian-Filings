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
import re
import sqlite3
import time
from pathlib import Path

from financials_pipeline import FinancialsStore, _coerce_numeric
from llm_extract import DEFAULT_VOCAB, NO_SCALE_KEYS, _load_vocab
from rule_extract import (
    BANK_ONLY_KEYS, CONFIDENCE_REVIEW_THRESHOLD, MIN_ROWS_OK, _BANK_RE,
    _EPS_KEYS, _KIND_RANK, _identity_checks, _statement_confidence,
    build_alias_index, build_compound_index, match_label_ctx,
)

_PER_SHARE_LABEL_RE = re.compile(r"per share|per unit|in dollars|per common share", re.I)

# Contra-asset keys the PDFs print as parenthesized deductions (negative) but
# QuoteMedia/GuruFocus store as POSITIVE magnitudes.
_ABS_VALUE_KEYS = {"AllowanceForLoansAndLeaseLosses", "AccumulatedDepreciation"}
# Attribution keys stored NEGATED by QM/GuruFocus (income attributable to
# non-controlling interests is a deduction from group income: screenshot shows
# "Other Income (Minority Interest)" as -5, -7).
_NEGATE_KEYS = {"MinorityInterests"}

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


def run_schema_mapping(cfg, *, force: bool = False, limit: int | None = None,
                       tickers: set[str] | None = None, progress=None) -> dict:
    src_db = cfg.get("storage", {}).get("db_path", "output/filings.db")
    fin_db = cfg.get("rules", {}).get("db_path") or "output/pdf_financials.db"
    vocab = _load_vocab(Path(cfg.get("rules", {}).get("vocab_path", DEFAULT_VOCAB)))
    alias_index = build_alias_index(vocab)
    compound_index = build_compound_index(vocab)

    grouped, identity, hints = _load_raw_line_items(src_db, tickers, limit)
    store = FinancialsStore(fin_db)
    already = set() if force else store.completed_statements()
    prior_tickers = store.existing_tickers()

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
    t0 = time.monotonic()

    if progress:
        progress(f"Schema mapping: {len(grouped)} ticker(s) from {src_db} -> {fin_db}")

    for ticker, by_pdf in grouped.items():
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
                for line_no, cells in sorted(by_line.items()):
                    r0 = cells[0]
                    label, section, zone = r0["label"], r0["section"], r0["zone"]
                    key, conf, kind = match_label_ctx(label, section, zone, stmt,
                                                      alias_index, compound_index)
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
                            val = _coerce_numeric(val)
                        full_rows.append(dict(
                            ticker=ticker, fiscal_year=y, statement_type=stmt,
                            source_pdf_year=pdf_year, line_no=line_no,
                            section=section, raw_label=label, canonical_key=key,
                            value=val, match_kind=kind))
                        if key and val is not None:
                            slot = (ticker, y, stmt, key)
                            cand = (val, _KIND_RANK.get(kind, 0), conf)
                            prev = picked.get(slot)
                            if prev is None:
                                picked[slot] = cand
                                slot_src[slot] = pdf_year
                            elif (slot_src[slot] == pdf_year
                                  and (prev[1], prev[2]) < (cand[1], cand[2])):
                                # better-quality match within the SAME (newest)
                                # source PDF; an older PDF never overrides.
                                picked[slot] = cand
                            by_year.setdefault(y, {}).setdefault(key, val)
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
                                                 confidence=conf_stmt))
                if progress:
                    progress(f"  {ticker} pdf{pdf_year} {stmt:16} "
                             f"{n_mapped}/{len(by_line)} labels mapped  "
                             f"conf={conf_stmt:.2f}")
        if ticker not in prior_tickers:
            ident = identity.get(ticker, {})
            company_rows.append(dict(ticker=ticker,
                                     company_name=ident.get("company_name"),
                                     exchange=ident.get("exchange"),
                                     currency=latest_currency,
                                     primary_source="cse_pdf_extract"))

    # Synonym fanout: fill QuoteMedia's duplicate keys where absent.
    for (t, y, s, k), v in list(picked.items()):
        for sibling in EQUIVALENT_FANOUT.get(k, []):
            slot = (t, y, s, sibling)
            if slot not in picked:
                picked[slot] = v

    line_rows = [dict(ticker=t, fiscal_year=y, statement_type=s, line_item=k,
                      value=v[0]) for (t, y, s, k), v in picked.items()]
    year_rows = [dict(ticker=t, fiscal_year=y, period_end=m["period_end"],
                      currency=m["currency"], source="cse_pdf_extract",
                      source_ref=m["source_ref"])
                 for (t, y), m in year_meta.items()]

    # A ticker's lines are always rebuilt whole from raw_line_items, so purge
    # first -- otherwise keys mapped on a previous run but not this one would
    # survive as stale ghosts (upsert never deletes).
    store.purge_ticker_lines({r["ticker"] for r in company_rows})
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
            "label_map_rate_pct": (mapped_labels / total_labels * 100)
                                  if total_labels else 0.0,
            "balance_identity_pass": balance_pass,
            "balance_identity_checked": balance_checked,
            "mean_confidence": (sum(confidences) / len(confidences))
                               if confidences else 0.0,
            "elapsed": time.monotonic() - t0, "db_path": fin_db}
