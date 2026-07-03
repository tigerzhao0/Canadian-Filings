# Canadian Annual Report PDF Finder — project notes

Pipeline that finds the most recent annual-report PDF for each of ~2,564
Canadian public companies (GuruFocus xlsx input: `C:\Users\tiger\Downloads\Canadian companies.xlsx`),
preferring official exchange/regulator filing sources first and only
scraping (company IR sites) for the tail those can't resolve. **Never
touches sedarplus.ca.** Free search only (DuckDuckGo default), designed to
run in one session.

## Architecture (resolution order)
Official sources are tried first, cheapest/most-authoritative before
slowest; scraping is the last resort ("only scrape when necessary"):
1. **CSE (XCNQ) filings fallback**: pulls latest `ANNUAL_FINANCIAL_STATEMENTS`
   from the CSE's own public API (`thecse.com`) — documents originate from
   SEDAR but are CSE-mirrored, never sedarplus.ca directly. Pure HTTP, no
   browser — runs first since it's cheap.
2. **SEC EDGAR cross-listing check**: if the company also files with the SEC
   (10-K/40-F/20-F) and hasn't been resolved by CSE, flag it (kept out of
   the review queue) rather than leaving it unresolved. Its `pdf_url` field
   is a **plain-text note**, not a link (the real EDGAR link lives in
   `sec_filing_url`) — company already has a first-class filing elsewhere,
   so we don't want it mistaken for a normal PDF find. Runs before TMX so
   SEC-flagged companies are excluded from the TMX pass.
3. **TMX (TSX/TSXV) filings fallback**: browser-driven — opens
   `money.tmx.com/en/quote/<SYM>/financials-filings`, pages the month
   carousel back to the latest "Audited annual financial statements". Slow,
   but still runs before scraping since it's an official source.
4. **Tier 1 (fast, no browser)**, only on whatever's still unresolved:
   search -> pick IR homepage (**sure match only** — real name/acronym/domain
   match, not a rank-only guess) -> deep static crawl -> direct
   `"<name>" annual report filetype:pdf` search -> validate.
5. **Tier 2**: headless-browser (Playwright) render pass, only on companies
   Tier 1 couldn't resolve. Slowest stage; runs last.

## Project layout (reorganized)
Files are grouped into folders; `run.py` + `config.example.yaml` stay at root.
- `run.py` (root) — entry point; puts `src/` on `sys.path` so the flat modules'
  bare inter-imports (`from crawl_pdf import ...`) resolve unchanged.
- `src/` — all module `.py` (kept FLAT, imports unchanged).
- `sql/` — `schema.sql`, `schema_financials.sql`, `company_schema.json`.
  Modules reference these via `Path(__file__).resolve().parent.parent / "sql" / ...`.
- `data/` — `blocklist.txt`, `Canadian Companies.xlsx` (config points here).
- `output/` — generated DBs (`filings.db`, `financials.db`), dashboard HTML,
  Excel exports, and `output/companies/<TICKER>.json`. Config `db_path`s point
  here; `.claude/launch.json` serves this dir.
- `docs/` — design/plan markdown.

## Key files
- `run.py` — CLI entrypoint (`--step 1` default = pipeline / `--step 2` = PDF
  processing; plus `--pilot`/`--full`/`--resume`/`--financials`/`--no-render`/
  `--render-only`)
- `pipeline.py` — orchestration, SQLite (`filings.db`), per-stage console
  stats, fiscal-year tagging (`+stale`/`+unverified`)
- `crawl_pdf.py` — scoring/ranking shared by all first-party sources
  (`score_pdf`, `is_confident`); year extraction is **filename-first**
  (`_pdf_year`) since a WordPress upload path like
  `/wp-content/uploads/2026/01/MON_FS_2022.pdf` encodes an upload date, not
  the fiscal year — filename wins
- `discover_ir.py` — IR homepage "sure match" gating
- `pdf_search.py`, `render.py`, `cse_filings.py`, `tmx_filings.py`,
  `sec_edgar.py` — the fallback sources
- `verify_pdf.py` — downloads a validated PDF and confirms via `pypdf` text
  extraction that it actually reads like a financial statement (rejects
  cover letters / "filing coming soon" notices / terms pages; spares
  scanned/unparseable PDFs)
- `validate.py`, `ingest.py`, `schema.sql`, `config.example.yaml`

## Repo / branches
- `main` — **everything is consolidated here now** (merge commit `c954e4a`).
  Has both the year-fix/content-verification work AND the recency-first
  ranking/multi-candidate-retry/per-stage-stats/SEC-note-not-link work,
  reconciled by hand where the two sides conflicted (`crawl_pdf.py`'s
  `score_pdf`, `pipeline.py`'s `_first_validating` + Tier1/Tier2 callers).
- `add-exchange-fallbacks-and-stats` — merged into `main`; its PR (#1) is
  closed. The branch itself may still exist on the remote/locally but has
  nothing `main` doesn't already have — safe to delete whenever.

## Resolved (previously open) work
- **Fiscal year targeting**: `expected_annual_year()` in `pipeline.py` now
  returns `today.year` (was `today.year - 1`), so as of 2026 it targets
  fiscal year 2026, still dynamic (advances every January).
- **"Brokerage" ask**: resolved as meaning the exchange fallbacks (CSE/TMX).
  The pipeline now tries CSE -> SEC -> TMX (official sources) *before*
  Tier 1/Tier 2 scraping, reversing the old scrape-first order — scraping
  only runs on whatever those official sources couldn't resolve. `run_pipeline`
  seeds a `filings` row (status='needs_review') per company up front so
  CSE/SEC/TMX's row-selection queries have something to act on before any
  Tier 1 upsert would otherwise have created that row.

## Known gotchas
- Python environment: `python` resolves to Windows Store Python 3.13 — that's
  where deps (`requirements.txt`, incl. `playwright`, `pypdf`) need to be
  installed; `py` may resolve to a different install.
- Playwright needs `playwright install chromium` once (headless browser
  binary), separate from `pip install playwright`.
- OneDrive + `.git` can be flaky (small-file sync churn) — prefer a fresh
  `git clone` from GitHub on a new machine over trusting OneDrive to sync
  `.git` correctly.
