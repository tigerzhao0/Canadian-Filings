# Canadian Annual Report PDF Finder — project notes

Pipeline that finds the most recent annual-report PDF for each of ~2,564
Canadian public companies (GuruFocus xlsx input: `C:\Users\tiger\Downloads\Canadian companies.xlsx`),
preferring first-party (company IR site) sources, with labeled exchange
fallbacks for the tail. **Never touches sedarplus.ca.** Free search only
(DuckDuckGo default), designed to run in one session.

## Architecture (resolution order)
1. **Tier 1 (fast, no browser)**: search -> pick IR homepage (**sure match
   only** — real name/acronym/domain match, not a rank-only guess) -> deep
   static crawl -> direct `"<name>" annual report filetype:pdf` search ->
   validate.
2. **Tier 2**: headless-browser (Playwright) render pass, only on companies
   Tier 1 couldn't resolve.
3. **CSE (XCNQ) filings fallback**: pulls latest `ANNUAL_FINANCIAL_STATEMENTS`
   from the CSE's own public API (`thecse.com`) — documents originate from
   SEDAR but are CSE-mirrored, never sedarplus.ca directly.
4. **SEC EDGAR cross-listing check**: if the company also files with the SEC
   (10-K/40-F/20-F) and no first-party PDF was found, flag it (kept out of
   the review queue) rather than leaving it unresolved. Its `pdf_url` field
   is a **plain-text note**, not a link (the real EDGAR link lives in
   `sec_filing_url`) — company already has a first-class filing elsewhere,
   so we don't want it mistaken for a normal PDF find.
5. **TMX (TSX/TSXV) filings fallback**: browser-driven — opens
   `money.tmx.com/en/quote/<SYM>/financials-filings`, pages the month
   carousel back to the latest "Audited annual financial statements". Slow;
   runs last, only on the leftover TSX/TSXV tail.

## Key files
- `run.py` — CLI entrypoint (`--pilot` default / `--full` / `--resume` /
  `--no-render` / `--render-only`)
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
- `main` — has the year-fix + content-verification work
  (`8c71fdc "refined the correct file finder, correct year and removed
  quarterly, made sure pdfs were real"`)
- `add-exchange-fallbacks-and-stats` — has the recency-first ranking +
  multi-candidate-retry + per-stage-stats work
  (`9a78fcc "v3 added more output logging and more accurate year..."`),
  open as PR #1 against `main`
- **These two branches have diverged and need reconciling** — see below.

## Open / interrupted work (pick up here)
The user asked (message interrupted mid-explanation, not yet done):
1. **Bring back the `add-exchange-fallbacks-and-stats` improvements** on top
   of `main`'s current state: `rank_pdfs()` (recency-first ranking, every
   source returns a ranked list not a single guess), multi-candidate retry
   in `_first_validating` (so a dead/blocked top pick doesn't sink the
   company), and the SEC `pdf_url`-is-a-note-not-a-link change. Need to
   merge/rebase these onto `main`'s newer year-fix + `verify_pdf.py` work
   rather than losing either side.
2. **Target fiscal year 2026 specifically** ("make sure the pdfs are for
   fiscal year 2026") — reconcile with the existing `expected_annual_year()`
   dynamic-year logic (current year − 1). Need to clarify: hardcode 2026 for
   now, or is "current year" reasoning still correct and 2026 was just
   today's expected value? (Today's system date was 2026-07-02 in earlier
   sessions, so `expected_annual_year()` = 2025 then — worth double-checking
   against the user's expectation of 2026.)
3. **"If direct from company website isn't a very good match... use the
   brokerage for the file link"** — user's wording, meaning UNCLEAR, cut off
   mid-message. Could mean: (a) a brokerage/data-vendor aggregator as a new
   fallback source, (b) they mean the exchange fallback (CSE/TMX) when they
   say "brokerage", or (c) something else entirely. **Ask the user to
   clarify what "brokerage" refers to before implementing.**

## Known gotchas
- Python environment: `python` resolves to Windows Store Python 3.13 — that's
  where deps (`requirements.txt`, incl. `playwright`, `pypdf`) need to be
  installed; `py` may resolve to a different install.
- Playwright needs `playwright install chromium` once (headless browser
  binary), separate from `pip install playwright`.
- OneDrive + `.git` can be flaky (small-file sync churn) — prefer a fresh
  `git clone` from GitHub on a new machine over trusting OneDrive to sync
  `.git` correctly.
