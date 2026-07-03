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

-- Multi-year annual-filing index: one row per (ticker, fiscal_year), so Stage 2
-- can record up to ~5 years of annual report / financial-statement PDFs per
-- company to match the 5-year depth the structured-financials stage pulls (see
-- financials.db). The single-company `filings` table above stays the per-company
-- STATUS/outcome record (and still holds the most-recent pdf_url as the primary
-- pointer); this table is the per-year DETAIL. Strictly ANNUAL documents only --
-- quarterly/interim filings are excluded at the source (see cse_filings.py,
-- tmx_filings.py, sec_edgar.py ANNUAL_FORMS, crawl_pdf.py NEGATIVE_HINTS).
CREATE TABLE IF NOT EXISTS filing_pdfs (
    ticker            TEXT NOT NULL,
    company_name      TEXT,
    exchange          TEXT,
    fiscal_year       INTEGER NOT NULL,
    pdf_url           TEXT,               -- the annual filing document URL for that year
    discovery_method  TEXT,               -- cse_filings | tmx_filings | sec_edgar | crawl | render | pdf_search
    verified          INTEGER,            -- 1 = content-confirmed financial statement, 0 = reachable-but-unverified
    failure_reason    TEXT,
    last_checked      TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year)
);

CREATE INDEX IF NOT EXISTS idx_filing_pdfs_ticker ON filing_pdfs(ticker);

-- Step 2 (run.py --step 2) output: for each filing_pdfs row, the PDF is
-- downloaded, its income/balance/cash-flow statement sections are extracted as
-- TEXT (see pdf_extract.py), the file is then deleted, and the text lands here.
-- This is the raw material for a later LLM pass that maps it into the same
-- statement shape financials.db already holds for QuoteMedia companies.
-- primary_block is the whole primary-statements span, kept as a fallback for
-- when a section couldn't be isolated cleanly. scanned=1 means no text layer
-- (needs OCR, a future branch) -- extract_ok=0 with reason explains any miss.
CREATE TABLE IF NOT EXISTS pdf_extractions (
    ticker            TEXT NOT NULL,
    fiscal_year       INTEGER NOT NULL,
    pdf_url           TEXT,
    scanned           INTEGER,
    income_statement  TEXT,
    balance_sheet     TEXT,
    cash_flow         TEXT,
    primary_block     TEXT,
    extract_ok        INTEGER,
    reason            TEXT,
    extracted_at      TIMESTAMP,
    PRIMARY KEY (ticker, fiscal_year)
);

CREATE INDEX IF NOT EXISTS idx_pdf_extractions_ticker ON pdf_extractions(ticker);
