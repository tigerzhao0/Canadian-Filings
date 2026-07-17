-- Structured financials — separate DB from filings.db.
--
-- NORMALIZED 3-level design (companies -> company_years -> statement_lines),
-- matching the intended data model: each TICKER is a row with its own
-- attributes, and its financials live in their own nested tables rather than
-- being flattened into one giant (ticker, year, line, value, source) fact
-- table. The three levels are the "dimensions":
--   1. companies       -- identity, one row per ticker (the outer table)
--   2. company_years   -- one row per (ticker, fiscal_year): the per-year
--                         header (period end, currency, and PROVENANCE, since
--                         a data source provides a whole year's statements at
--                         once -- QuoteMedia today, an LLM-read PDF or SEC XBRL
--                         later, all keyed the same way with no schema change)
--   3. statement_lines -- the actual numbers: one row per (ticker, year,
--                         statement_type, line_item). Pure and narrow -- no
--                         repeated metadata; join up to the parents for it.
-- Cross-ticker / cross-metric queries stay trivial (e.g. every company's
-- TotalAssets for 2025 is one indexed WHERE), a new fiscal year is just new
-- rows (never a schema change), and multiple sources coexist because
-- provenance lives at the year grain. The v_financials view below re-flattens
-- all three back into the old wide row shape for anything that wants it.

-- 1. Identity — one row per ticker (the outer table's rows).
CREATE TABLE IF NOT EXISTS companies (
    ticker          TEXT PRIMARY KEY,
    company_name    TEXT,
    exchange        TEXT,
    currency        TEXT,            -- most-recent reporting currency
    primary_source  TEXT,            -- tmx_quotemedia | cse_quotemedia | neo_quotemedia | ...
    updated_at      TIMESTAMP
);

-- 2. Per-year header — one row per (ticker, fiscal_year). This is the grain at
-- which a SOURCE hands over a full statement set, so source/source_ref live
-- here (not repeated on every line). A future LLM-PDF or SEC-XBRL year for the
-- same company slots in here with its own provenance, no collision.
CREATE TABLE IF NOT EXISTS company_years (
    ticker       TEXT NOT NULL,
    fiscal_year  INTEGER NOT NULL,
    period_end   TEXT,
    currency     TEXT,
    source       TEXT NOT NULL,      -- who provided THIS year's numbers
    source_ref   TEXT,               -- provenance: API symbol / PDF url / filing id
    fetched_at   TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year)
);

-- 3. The numbers — the cells of each company's nested financials grid.
-- One authoritative value per (ticker, year, statement_type, line_item);
-- source is NOT in the key, so a better source later UPDATES the cell rather
-- than duplicating it. `value` NULL = line item reported but empty.
CREATE TABLE IF NOT EXISTS statement_lines (
    ticker          TEXT NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    statement_type  TEXT NOT NULL,   -- 'income_statement' | 'balance_sheet' | 'cash_flow'
    line_item       TEXT NOT NULL,   -- e.g. 'TotalRevenue', 'NetIncome', 'TotalAssets'
    value           REAL,
    PRIMARY KEY (ticker, fiscal_year, statement_type, line_item)
);

CREATE INDEX IF NOT EXISTS idx_lines_ticker_year ON statement_lines(ticker, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_lines_item ON statement_lines(line_item);

-- Convenience: re-flatten the 3 levels into one wide row (the old fact shape),
-- for exports / ad-hoc queries that don't want to write the joins.
CREATE VIEW IF NOT EXISTS v_financials AS
SELECT c.ticker, c.company_name, c.exchange,
       cy.fiscal_year, cy.period_end, cy.currency,
       sl.statement_type, sl.line_item, sl.value,
       cy.source, cy.source_ref
FROM statement_lines sl
JOIN company_years cy ON cy.ticker = sl.ticker AND cy.fiscal_year = sl.fiscal_year
JOIN companies c ON c.ticker = sl.ticker;

-- Secondary/audit layer: the exact raw QuoteMedia response per (ticker, year),
-- kept so a value can be re-derived / spot-checked without re-fetching. NOT
-- the canonical tables for querying -- those are the three above.
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
-- in companies is never ambiguous ("never tried" vs "tried, no data").
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

-- run.py --step 3 (local-LLM extraction of CSE PDF statement text) audit layer.
-- The canonical numbers land in the same companies/company_years/statement_lines
-- tables above (tagged source='cse_pdf_extract'); these two tables are the
-- provenance/outcome record for that pass, parallel to tmx_financials_raw /
-- tmx_financials_status for the QuoteMedia pass.
--
-- pdf_llm_raw: the verbatim model JSON per (ticker, year, statement), so a value
-- can be re-derived / spot-checked (and a wrong unit_scale corrected) without
-- re-running the LLM. NOT canonical -- query statement_lines / v_financials.
CREATE TABLE IF NOT EXISTS pdf_llm_raw (
    ticker          TEXT NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    statement_type  TEXT NOT NULL,   -- 'income_statement' | 'balance_sheet' | 'cash_flow'
    model           TEXT,            -- which Ollama model produced this
    prompt_version  TEXT,            -- llm.prompt_version at extraction time
    unit_scale      REAL,            -- detected 'in thousands/millions' multiplier
    currency        TEXT,
    raw_json        TEXT,            -- verbatim model output (columns + lines)
    extracted_at    TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year, statement_type)
);

CREATE INDEX IF NOT EXISTS idx_pdf_llm_raw_ticker ON pdf_llm_raw(ticker);

-- Cross-year consistency diagnostic: flags a ticker where different fiscal
-- years used different canonical keys (per SYNONYM_GROUPS in llm_extract.py)
-- for what's probably the same real-world concept (e.g. net income mapped to
-- NetIncome in one year, NetIncomeContinuousOperations in another). Purely
-- informational -- doesn't change what's in statement_lines, just flags it
-- for a human look / a future synonym-collapse pass.
CREATE TABLE IF NOT EXISTS pdf_llm_consistency (
    ticker         TEXT NOT NULL,
    concept_group  TEXT NOT NULL,   -- the synonym cluster, as a sorted '|'-joined key list
    pattern        TEXT,            -- 'redundant_every_year' (same keys always co-occur --
                                     -- one concept double-mapped) | 'switches_by_year'
                                     -- (different years genuinely used different keys)
    keys_used      TEXT,            -- JSON: {canonical_key: [fiscal_years using it]}
    checked_at     TIMESTAMP,
    PRIMARY KEY (ticker, concept_group)
);

-- One row per (ticker, fiscal_year, statement_type) attempted, so "tried but
-- the model failed" is never confused with "not tried" (the resume key).
-- status: 'ok' | 'invalid_json' | 'empty' | 'no_columns' | 'llm_error'
CREATE TABLE IF NOT EXISTS pdf_llm_status (
    ticker          TEXT NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    statement_type  TEXT NOT NULL,
    status          TEXT,
    reason          TEXT,
    n_lines         INTEGER,         -- canonical line items written for this statement
    confidence      REAL,            -- 0-1 extraction confidence (rule path)
    doc_type        TEXT,            -- primary_statements | aif | mda | interim | other
    checked_at      TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year, statement_type)
);

-- EVERY parsed line item (mapped AND unmapped), with provenance -- the
-- "nothing from the PDF is lost" table. canonical_key is NULL for lines that
-- didn't map onto the GuruFocus/QuoteMedia vocabulary; value is unit-scaled.
CREATE TABLE IF NOT EXISTS line_items_full (
    ticker           TEXT NOT NULL,
    fiscal_year      INTEGER NOT NULL,   -- the comparative-period year (col_year)
    statement_type   TEXT NOT NULL,
    source_pdf_year  INTEGER NOT NULL,   -- which PDF this row came from
    line_no          INTEGER NOT NULL,
    section          TEXT,
    raw_label        TEXT NOT NULL,
    canonical_key    TEXT,               -- NULL = unmapped (still kept)
    value            REAL,
    match_kind       TEXT,               -- compound | exact | suffix | fuzzy | NULL
    updated_at       TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year, statement_type, source_pdf_year, line_no)
);
CREATE INDEX IF NOT EXISTS idx_line_items_full_ticker
    ON line_items_full(ticker, fiscal_year);

-- Per-line-item quality (rule path): a confidence per written value, for the
-- dashboard heatmap. Kept parallel so statement_lines stays a narrow fact table.
CREATE TABLE IF NOT EXISTS statement_line_quality (
    ticker          TEXT NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    statement_type  TEXT NOT NULL,
    line_item       TEXT NOT NULL,
    confidence      REAL,
    match_kind      TEXT,   -- mapped / derived / absent_on_face
    checked_at      TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year, statement_type, line_item)
);

-- One row per run_tmx_financials() call, so real elapsed time / "last
-- refreshed" is available (bulk-upsert stamps every row with the same
-- write-time timestamp, so there's no per-row timing to derive this from --
-- it has to be captured explicitly at the point the run actually finishes).
CREATE TABLE IF NOT EXISTS run_log (
    run_at        TIMESTAMP PRIMARY KEY,
    total         INTEGER,
    resolved      INTEGER,
    failed        INTEGER,
    excluded      INTEGER,
    elapsed_sec   REAL
);
