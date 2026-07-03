-- Structured financials — separate DB from filings.db.
--
-- financial_facts is the CANONICAL, cross-source table: one row per single
-- line item (ticker, fiscal_year, statement_type, line_item, source). This
-- long/normalized shape is deliberate -- it's what lets QuoteMedia (TSX/TSXV
-- direct, CSE/XCNQ via a :CNX symbol suffix, ~97% CSE coverage -- see
-- financials_pipeline.py) and the future CSE-LLM-PDF-extraction fallback
-- (for the ~3% QuoteMedia misses; an LLM reading raw PDF text from hundreds
-- of inconsistent micro-caps, wildly varying field coverage/naming) coexist
-- in the same table without a schema change or field-name collisions.
-- `source`/`source_ref` carries provenance on every single value, which
-- matters once an LLM is in the extraction path. Query this table for
-- anything cross-source; pivot to wide format at read time if you need it
-- for an export.
CREATE TABLE IF NOT EXISTS financial_facts (
    ticker          TEXT NOT NULL,
    exchange        TEXT,
    fiscal_year     INTEGER NOT NULL,
    period_end      TEXT,
    currency        TEXT,
    statement_type  TEXT NOT NULL,   -- 'income_statement' | 'balance_sheet' | 'cash_flow'
    line_item       TEXT NOT NULL,   -- e.g. 'TotalRevenue', 'NetIncome', 'TotalAssets'
    value           REAL,
    source          TEXT NOT NULL,   -- 'tmx_quotemedia' | 'cse_quotemedia' |
                                     -- 'cse_llm_extract' | 'sec_xbrl' (later)
    source_ref      TEXT,            -- provenance: API call / PDF URL / filing id
    extracted_at    TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year, statement_type, line_item, source)
);

CREATE INDEX IF NOT EXISTS idx_facts_ticker_year ON financial_facts(ticker, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_facts_source ON financial_facts(source);

-- Secondary/audit layer for the TMX source specifically: the exact raw
-- QuoteMedia response per (ticker, year), kept so a fact can be re-derived
-- or spot-checked without re-fetching. NOT the canonical table for
-- querying/joining across sources -- that's financial_facts above.
CREATE TABLE IF NOT EXISTS tmx_financials_raw (
    ticker            TEXT NOT NULL,
    company_name      TEXT,
    exchange          TEXT,
    year              INTEGER NOT NULL,
    period_end        TEXT,
    currency          TEXT,
    income_statement  TEXT,   -- JSON blob, QuoteMedia field names, verbatim
    balance_sheet     TEXT,   -- JSON blob, QuoteMedia field names, verbatim
    cash_flow         TEXT,   -- JSON blob, QuoteMedia field names, verbatim
    fetched_at        TIMESTAMP,
    PRIMARY KEY (ticker, year)
);

CREATE INDEX IF NOT EXISTS idx_tmx_financials_raw_ticker ON tmx_financials_raw(ticker);

-- One row per company attempted, regardless of outcome, so a missing ticker
-- in financial_facts is never ambiguous ("never tried" vs "tried, no data").
-- status: 'ok' | 'no_data' | 'capital_pool_company' | 'trust_or_fund'
--   the latter two = ticker ends in .P / .UN and came back with no data --
--   still API-called like everything else, just excluded from success-rate
--   math since these often have nothing to report by design, not by failure.
CREATE TABLE IF NOT EXISTS tmx_financials_status (
    ticker       TEXT PRIMARY KEY,
    company_name TEXT,
    exchange     TEXT,
    status       TEXT,
    reason       TEXT,
    checked_at   TIMESTAMP
);
