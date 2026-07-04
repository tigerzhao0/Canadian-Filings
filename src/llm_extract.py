"""run.py --step 3: local-LLM extraction of CSE annual-report statement TEXT
into the structured financials DB.

Reads the raw statement text step 2 stored in filings.db `pdf_extractions`
(income_statement / balance_sheet / cash_flow, or the primary_block fallback),
sends each statement to a local Ollama model with a JSON-schema-enforced
response, and upserts the parsed numbers into financials.db's canonical 3-level
tables (companies / company_years / statement_lines) tagged
`source='cse_pdf_extract'` -- the same shape QuoteMedia fills, so
company_export.py emits these PDF-only companies with no schema change.

Design notes:
- Line items are mapped onto the CANONICAL QuoteMedia vocabulary
  (data/canonical_lineitems.json) so PDF companies stay query-compatible with
  QuoteMedia ones. A line that matches no canonical key is dropped -- never
  invented, never force-mapped.
- The model transcribes; PYTHON owns the arithmetic. The model reports each
  value exactly as printed plus a detected `scale` (thousands->1000,
  millions->1000000); we compute value = printed * scale here, deterministically.
  Per-share / share-count keys are exempt from scaling (NO_SCALE_KEYS).
- The stage is GPU-serial (Ollama serializes on one card), so this is a plain
  synchronous loop -- no asyncio, unlike steps 1/2.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from financials_pipeline import FinancialsStore, _coerce_numeric

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VOCAB = ROOT / "data" / "canonical_lineitems.json"
FILINGS_SCHEMA = ROOT / "sql" / "schema.sql"

STATEMENTS = ("income_statement", "balance_sheet", "cash_flow")

# Canonical keys whose values are per-share amounts or share COUNTS, already in
# absolute units -- a "(in thousands)" note applies to the money columns, not
# to these, so they must NOT be scale-multiplied.
NO_SCALE_KEYS = {
    "BasicEPS", "DilutedEPS",
    "BasicAverageShares", "DilutedAverageShares",
    "SharesOutstanding", "ShareClassLevelSharesOutstanding",
    "BasicContinuousOperations", "DilutedContinuousOperations",
    "TotalEmployees",
}

# Hallucination guard: every value the model reports must literally appear
# (as a printed number, pre-scale) somewhere in the source text it was given --
# otherwise it's dropped rather than trusted. Cheap, no extra LLM calls.
_NUM_RE = re.compile(r"\(?\$?-?\d[\d,]*\.?\d*\)?")


def _extract_number_tokens(text: str) -> set[float]:
    tokens: set[float] = set()
    for m in _NUM_RE.finditer(text or ""):
        s = m.group().strip().strip("()").replace("$", "").replace(",", "").strip()
        if not s or s in ("-", "."):
            continue
        try:
            v = float(s)
        except ValueError:
            continue
        tokens.add(round(abs(v), 2))
    return tokens


def _verify_value(num: float, tokens: set[float]) -> bool:
    if num is None:
        return False
    if round(abs(num), 2) in tokens:
        return True
    return float(round(abs(num))) in tokens  # tolerate int-vs-decimal formatting


# Loose synonym clusters within QuoteMedia's own vocabulary (NOT sent to the
# model / not used to restrict allowed keys -- purely a post-hoc diagnostic:
# flag a ticker where different years used different synonyms for what's
# probably the same real-world concept, e.g. share count or net income).
SYNONYM_GROUPS: list[frozenset[str]] = [
    frozenset({"NetIncome", "NetIncomeCommonStockholders", "NetIncomeContinuousOperations",
              "NetIncomeFromContinuingAndDiscontinuedOperation", "NormalizedIncome"}),
    # Weighted-average shares (the EPS denominator) vs point-in-time shares
    # outstanding are DIFFERENT accounting concepts -- kept as separate clusters
    # even though they often coincide numerically for static-cap-table issuers.
    frozenset({"BasicAverageShares", "DilutedAverageShares"}),
    frozenset({"SharesOutstanding", "ShareClassLevelSharesOutstanding"}),
    frozenset({"OperatingCashFlow", "CashFlowFromContinuingOperatingActivities"}),
    frozenset({"FinancingCashFlow", "CashFlowFromContinuingFinancingActivities"}),
    frozenset({"InvestingCashFlow", "CashFlowFromContinuingInvestingActivities"}),
    frozenset({"DepreciationAmortizationDepletion", "DepreciationAndAmortization"}),
    frozenset({"TotalEquityGrossMinorityInterest", "TotalEquityGrossMinority",
              "StockholdersEquity", "CommonStockEquity"}),
    frozenset({"CashAndCashEquivalents", "Cash", "CashCashEquivalentsAndShortTermInvestments"}),
    frozenset({"AccountsPayable", "Payables", "PayablesAndAccruedExpenses"}),
    frozenset({"CapitalStock", "CommonStock"}),
    frozenset({"NetPPEPurchaseAndSale", "PurchaseOfPPE", "CapitalExpenditureReported",
              "NetCapitalExpenditureDisposals"}),
    frozenset({"NetCommonStockIssuance", "CommonStockIssuance", "IssuanceOfCapitalStock"}),
    frozenset({"NetIssuancePaymentsOfDebt", "IssuanceOfDebt", "NetLongTermDebtIssuance"}),
    frozenset({"ChangeInPayablesAndAccruedExpense", "ChangeInPayable", "ChangeInAccountPayable"}),
    frozenset({"ChangeInReceivables", "ChangesInAccountReceivables"}),
]
_GROUP_ID = {key: "|".join(sorted(grp)) for grp in SYNONYM_GROUPS for key in grp}


def _group_id(key: str) -> str | None:
    return _GROUP_ID.get(key)

# Response schema handed to Ollama as `format=` (so output is always valid JSON
# of this exact shape) AND used to jsonschema-validate defensively afterward.
RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["scale", "columns", "lines"],
    "properties": {
        "scale": {"type": "number"},
        "currency": {"type": ["string", "null"]},
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["fiscalYear"],
                "properties": {
                    "fiscalYear": {"type": "integer"},
                    "periodEnd": {"type": ["string", "null"]},
                },
            },
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key", "values"],
                "properties": {
                    "key": {"type": "string"},
                    "values": {"type": "array",
                               "items": {"type": ["number", "string", "null"]}},
                },
            },
        },
    },
}

_SYSTEM = (
    "You extract line items from raw PDF-extracted financial statement text and "
    "return ONLY the requested JSON.\n"
    "Rules:\n"
    "- Each row has one number per comparative period column, left to right.\n"
    "- `columns` maps each numeric column, in that same left-to-right order, to "
    "its fiscalYear (the 4-digit year of the period END) and periodEnd "
    "(ISO yyyy-mm-dd if determinable, else null).\n"
    "- Map each line to ONE of the ALLOWED KEYS. If a line does not clearly "
    "match an allowed key, OMIT it. Never invent, infer, or compute values.\n"
    "- Report every value EXACTLY as printed (do NOT multiply by any scale). "
    "Parentheses mean negative: \"(1,234)\" -> -1234. Strip thousands "
    "separators. A dash '-' or blank means null for that column.\n"
    "- Set `scale` from the statement's units note: \"in thousands\" -> 1000, "
    "\"in millions\" -> 1000000, otherwise 1.\n"
    "- `values` length MUST equal `columns` length."
)


def _load_vocab(path: Path) -> dict[str, list[str]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rows_to_extract(src_db: str, *, limit: int | None = None,
                     tickers: set[str] | None = None) -> list[dict]:
    conn = sqlite3.connect(src_db)
    try:
        conn.executescript(FILINGS_SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        cur = conn.execute(
            "SELECT p.ticker, p.fiscal_year, p.pdf_url, "
            "       p.income_statement, p.balance_sheet, p.cash_flow, p.primary_block, "
            "       f.company_name, f.exchange "
            "FROM pdf_extractions p "
            "LEFT JOIN filing_pdfs f ON f.ticker = p.ticker AND f.fiscal_year = p.fiscal_year "
            "WHERE p.extract_ok = 1 AND p.scanned = 0 "
            "ORDER BY p.ticker, p.fiscal_year DESC")
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    if tickers:
        rows = [r for r in rows if r["ticker"] in tickers]
    if limit:
        rows = rows[:limit]
    return rows


def _section_text(row: dict, statement_type: str) -> str:
    """The isolated section if step 2 captured one, else the whole primary_block
    (which contains all three statements) as the fallback."""
    txt = (row.get(statement_type) or "").strip()
    if txt:
        return txt
    return (row.get("primary_block") or "").strip()


def _build_messages(ticker: str, statement_type: str,
                    allowed_keys: list[str], text: str) -> list[dict]:
    numbered = "\n".join(f"{i+1:>3}| {ln}"
                         for i, ln in enumerate(text.splitlines()) if ln.strip())
    user = (
        f"STATEMENT: {statement_type}\n"
        f"COMPANY: {ticker}\n"
        f"ALLOWED KEYS: {', '.join(allowed_keys)}\n"
        "--- RAW TEXT (one source line per row) ---\n"
        f"{numbered}"
    )
    return [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user}]


def _to_number(v):
    """Robustly coerce a model value to float. The response schema already
    forces number|null, but models occasionally emit "(1,234)" strings -- handle
    parentheses-negatives / separators defensively."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("$", "").replace(" ", "")
        if s in ("", "-", "—", "–"):
            return None
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()")
        try:
            n = float(s)
        except ValueError:
            return None
        return -n if neg else n
    return None


def _normalize(parsed: dict, allowed: set[str], statement_type: str, source_text: str):
    """Turn a validated model response into (year_meta, line_rows, scale,
    currency, dropped).

    year_meta:  { fiscal_year: {"period_end": str|None, "currency": str|None} }
    line_rows:  [ {fiscal_year, statement_type, line_item, value}, ... ]
    dropped:    [ (fiscal_year, line_item), ... ] -- values that failed the
                hallucination guard (their printed magnitude doesn't appear
                anywhere in source_text) and were therefore NOT written.
    Applies scale (except NO_SCALE_KEYS) and drops non-canonical keys / nulls.
    """
    try:
        scale = float(parsed.get("scale") or 1) or 1.0
    except (TypeError, ValueError):
        scale = 1.0
    if scale <= 0:
        scale = 1.0
    currency = parsed.get("currency")
    tokens = _extract_number_tokens(source_text)

    columns = parsed.get("columns") or []
    # Column i -> plausible fiscal year (else None so we skip that column).
    col_years: list[int | None] = []
    year_meta: dict[int, dict] = {}
    for col in columns:
        fy = col.get("fiscalYear")
        fy = int(fy) if isinstance(fy, (int, float)) and 1900 <= int(fy) <= 2100 else None
        col_years.append(fy)
        if fy is not None and fy not in year_meta:
            year_meta[fy] = {"period_end": col.get("periodEnd"), "currency": currency}

    line_rows: list[dict] = []
    dropped: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for line in parsed.get("lines") or []:
        key = line.get("key")
        if key not in allowed:
            continue
        values = line.get("values") or []
        if len(values) != len(col_years):
            continue  # misaligned row -- can't trust the column mapping
        for i, raw in enumerate(values):
            fy = col_years[i]
            if fy is None:
                continue
            num = _to_number(raw)  # pre-scale, exactly as printed
            if num is None:
                continue
            if not _verify_value(num, tokens):
                # Hallucination guard: the printed magnitude isn't anywhere in
                # the source text -- don't trust it, never invent a value.
                dropped.append((fy, key))
                continue
            if key not in NO_SCALE_KEYS:
                num *= scale
            num = _coerce_numeric(num)
            if num is None:
                continue
            dedup = (fy, key)
            if dedup in seen:  # first occurrence wins (statements sometimes repeat a label)
                continue
            seen.add(dedup)
            line_rows.append(dict(fiscal_year=fy, statement_type=statement_type,
                                  line_item=key, value=num))
    return year_meta, line_rows, scale, currency, dropped


def _call_llm(client, cfg: dict, messages: list[dict], *, model: str) -> dict:
    """One Ollama chat call with schema-enforced JSON output. Raises on failure."""
    resp = client.chat(
        model=model,
        messages=messages,
        format=RESPONSE_SCHEMA,
        options={"temperature": float(cfg.get("temperature", 0)),
                 "num_ctx": int(cfg.get("num_ctx", 16384))},
        keep_alive=cfg.get("keep_alive", "30m"),
    )
    content = resp["message"]["content"]
    return json.loads(content)


def run_llm_extraction(cfg, *, force: bool = False, limit: int | None = None,
                       tickers: set[str] | None = None, progress=None) -> dict:
    llm = cfg.get("llm", {}) or {}
    src_db = cfg.get("storage", {}).get("db_path", "output/filings.db")
    fin_db = cfg.get("tmx_financials", {}).get("db_path", "output/financials.db")
    model = llm.get("model", "qwen2.5:7b-instruct-q4_K_M")
    fallback_model = llm.get("fallback_model")
    use_fallback = bool(llm.get("use_fallback_on_invalid"))
    prompt_version = str(llm.get("prompt_version", "v1"))
    vocab = _load_vocab(Path(llm.get("vocab_path", DEFAULT_VOCAB)))

    try:
        from ollama import Client
    except ImportError as exc:  # noqa: TRY003
        raise SystemExit(
            "The 'ollama' python package is required for --step 3.\n"
            "  pip install ollama\n"
            "and install the Ollama server (https://ollama.com), then:\n"
            f"  ollama pull {model}") from exc
    client = Client(host=llm.get("base_url", "http://localhost:11434"),
                    timeout=float(llm.get("request_timeout_s", 120)))

    rows = _rows_to_extract(src_db, limit=limit, tickers=tickers)
    if progress:
        progress(f"LLM extraction: {len(rows)} pdf row(s) in {src_db} "
                 f"-> {fin_db}  (model={model})")
    if not rows:
        if progress:
            progress("  nothing to extract -- pdf_extractions has no usable rows. "
                     "Run step 2 first.")
        return {"attempted": 0, "parsed_ok": 0, "invalid": 0, "skipped": 0,
                "companies": 0, "years": 0, "lines": 0,
                "unverified_dropped": 0, "consistency_warnings": 0,
                "elapsed": 0.0, "db_path": fin_db}

    store = FinancialsStore(fin_db)
    already = set() if force else store.completed_statements()
    prior_tickers = store.existing_tickers()

    # Accumulators (write once at the end -- see FinancialsStore docstring).
    identity: dict[str, dict] = {}            # ticker -> {name, exchange, latest_fy, currency}
    year_meta_acc: dict[tuple[str, int], dict] = {}   # (ticker, fy) -> {period_end, currency, source_ref}
    line_rows_acc: list[dict] = []
    raw_rows: list[dict] = []
    status_rows: list[dict] = []

    parsed_ok = invalid = skipped = unverified_dropped = 0
    t0 = time.monotonic()
    total_calls = len(rows) * len(STATEMENTS)
    done = 0

    for row in rows:
        ticker = row["ticker"]
        row_fy = row["fiscal_year"]
        for stmt in STATEMENTS:
            done += 1
            if (ticker, row_fy, stmt) in already:
                skipped += 1
                continue
            if progress:
                progress(f"  [{done}/{total_calls}] {ticker} {row_fy} {stmt} ...")
            text = _section_text(row, stmt)
            if not text:
                status_rows.append(dict(ticker=ticker, fiscal_year=row_fy,
                                        statement_type=stmt, status="empty",
                                        reason="no section text or primary_block",
                                        n_lines=0))
                skipped += 1
                if progress:
                    progress("      -> skipped (no text)")
                continue

            messages = _build_messages(ticker, stmt, vocab.get(stmt, []), text)
            parsed = None
            used_model = model
            for attempt_model in ([model, fallback_model] if (use_fallback and fallback_model)
                                  else [model]):
                try:
                    parsed = _call_llm(client, llm, messages, model=attempt_model)
                    used_model = attempt_model
                    break
                except Exception as exc:  # noqa: BLE001 - one bad call must not kill the batch
                    last_err = exc
                    parsed = None
            if parsed is None:
                invalid += 1
                status_rows.append(dict(ticker=ticker, fiscal_year=row_fy,
                                        statement_type=stmt, status="llm_error",
                                        reason=str(last_err)[:200], n_lines=0))
                if progress:
                    progress(f"      -> llm_error: {str(last_err)[:120]}")
                continue

            allowed = set(vocab.get(stmt, []))
            year_meta, lines, scale, currency, dropped = _normalize(
                parsed, allowed, stmt, text)
            unverified_dropped += len(dropped)
            raw_rows.append(dict(ticker=ticker, fiscal_year=row_fy, statement_type=stmt,
                                 model=used_model, prompt_version=prompt_version,
                                 unit_scale=scale, currency=currency,
                                 raw_json=json.dumps(parsed)))
            if not year_meta:
                status_rows.append(dict(ticker=ticker, fiscal_year=row_fy,
                                        statement_type=stmt, status="no_columns",
                                        reason="no plausible fiscal-year columns",
                                        n_lines=0))
                invalid += 1
                if progress:
                    progress("      -> invalid: no plausible fiscal-year columns")
                continue

            for fy, meta in year_meta.items():
                key = (ticker, fy)
                cur = year_meta_acc.setdefault(
                    key, {"period_end": None, "currency": None, "source_ref": row["pdf_url"]})
                cur["period_end"] = cur["period_end"] or meta.get("period_end")
                cur["currency"] = cur["currency"] or meta.get("currency")
            for ln in lines:
                line_rows_acc.append(dict(ticker=ticker, **ln))

            # Identity: remember the most-recent year's currency for this ticker.
            ident = identity.setdefault(
                ticker, {"company_name": row.get("company_name"),
                         "exchange": row.get("exchange"),
                         "latest_fy": None, "currency": None})
            for fy, meta in year_meta.items():
                if ident["latest_fy"] is None or fy > ident["latest_fy"]:
                    ident["latest_fy"], ident["currency"] = fy, meta.get("currency")

            reason = None
            if dropped:
                sample = ", ".join(f"{k}({fy})" for fy, k in dropped[:5])
                reason = f"{len(dropped)} value(s) failed text-verification: {sample}"
            status_rows.append(dict(ticker=ticker, fiscal_year=row_fy,
                                    statement_type=stmt, status="ok", reason=reason,
                                    n_lines=len(lines)))
            parsed_ok += 1
            if progress:
                msg = (f"      -> ok ({len(lines)} line items, "
                      f"{len(year_meta)} year column(s))")
                if dropped:
                    msg += f"  [{len(dropped)} dropped: unverified vs source text]"
                progress(msg)

    # --- assemble canonical rows -------------------------------------------
    company_rows = [
        dict(ticker=t, company_name=v["company_name"], exchange=v["exchange"],
             currency=v["currency"], primary_source="cse_pdf_extract")
        for t, v in identity.items() if t not in prior_tickers  # don't clobber QuoteMedia identity
    ]
    year_rows = [
        dict(ticker=t, fiscal_year=fy, period_end=m["period_end"],
             currency=m["currency"], source="cse_pdf_extract",
             source_ref=m["source_ref"])
        for (t, fy), m in year_meta_acc.items()
    ]

    # Cross-year consistency check: for each ticker, did different years use
    # different synonyms (per SYNONYM_GROUPS) for what's probably the same
    # concept (e.g. net income, share count)? Purely diagnostic -- doesn't
    # change what got written, just flags it for a human look.
    by_ticker_group: dict[tuple[str, str], dict[str, set[int]]] = {}
    for ln in line_rows_acc:
        gid = _group_id(ln["line_item"])
        if gid is None:
            continue
        d = by_ticker_group.setdefault((ln["ticker"], gid), {})
        d.setdefault(ln["line_item"], set()).add(ln["fiscal_year"])
    consistency_rows = []
    for (tk, gid), keymap in by_ticker_group.items():
        if len(keymap) <= 1:
            continue
        year_sets = list(keymap.values())
        # Same keys present in the SAME years every time -> the model is
        # redundantly double-mapping one concept into >1 canonical key, not
        # switching which key it uses year to year (a different, milder issue).
        pattern = ("redundant_every_year" if all(s == year_sets[0] for s in year_sets)
                  else "switches_by_year")
        consistency_rows.append(dict(
            ticker=tk, concept_group=gid, pattern=pattern,
            keys_used=json.dumps({k: sorted(years) for k, years in keymap.items()})))

    store.bulk_upsert_companies(company_rows)
    store.bulk_upsert_company_years(year_rows)
    store.bulk_upsert_statement_lines(line_rows_acc)
    store.bulk_upsert_llm_raw(raw_rows)
    store.bulk_upsert_llm_status(status_rows)
    store.bulk_upsert_consistency_flags(consistency_rows)
    store.close()

    elapsed = time.monotonic() - t0
    return {"attempted": total_calls, "parsed_ok": parsed_ok, "invalid": invalid,
            "skipped": skipped, "companies": len(company_rows),
            "years": len(year_rows), "lines": len(line_rows_acc),
            "unverified_dropped": unverified_dropped,
            "consistency_warnings": len(consistency_rows),
            "elapsed": elapsed, "db_path": fin_db}
