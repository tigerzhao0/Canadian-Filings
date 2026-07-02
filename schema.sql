-- Canadian Annual Report PDF Finder — SQLite schema.
-- One row per company (ticker is the primary key so re-runs upsert
-- idempotently and rows already marked 'found' are skipped on resume).

CREATE TABLE IF NOT EXISTS filings (
    ticker            TEXT PRIMARY KEY,
    company_name      TEXT,
    exchange          TEXT,
    ir_homepage_url   TEXT,
    pdf_url           TEXT,
    fiscal_year_guess INTEGER,
    discovery_method  TEXT,
    status            TEXT CHECK(status IN ('found','not_found','needs_review')),
    failure_reason    TEXT,
    sec_filer         INTEGER DEFAULT 0,   -- 1 = files with the SEC (EDGAR)
    sec_filing_url    TEXT,                -- pointer to latest 10-K/40-F/20-F (mirrored into pdf_url too)
    sec_filing_form   TEXT,                -- e.g. '10-K', '40-F', '20-F'
    sec_filing_date   TEXT,                -- filing date of that annual filing, YYYY-MM-DD
    last_checked      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_filings_status ON filings(status);
