# Canadian Annual Report PDF Finder

Finds a direct URL to the most recent **annual report PDF** for each Canadian
public company (TSX / TSXV / CSE), sourced from the company's own investor-
relations site — **not** SEDAR+. Results land in a SQLite database, with a
labelled manual-review queue for anything that can't be resolved automatically.

## Quickstart

```bash
# 1. Install dependencies (Python 3.11+; developed on 3.13)
pip install -r requirements.txt
# For the highest success rate, also grab the headless browser (one-time, ~150MB):
playwright install chromium

# 2. Run a pilot on ~40 sampled companies — NO API KEY NEEDED (uses DuckDuckGo)
python run.py --input "C:/Users/tiger/Downloads/Canadian companies.xlsx"

# 3. When the pilot looks good, run the whole list
python run.py --input "C:/Users/tiger/Downloads/Canadian companies.xlsx" --full

# 4. Mop up the leftovers — re-run resume a few times; found rows stick and the
#    union grows (DuckDuckGo returns different results each pass).
python run.py --input "C:/Users/tiger/Downloads/Canadian companies.xlsx" --resume
```

That's it — no signup, no key. Results are written to `filings.db` as they're
found, and a summary of `found / needs_review / not_found` prints at the end.

## What the run does — a two-tier cascade

For each company, **Tier 1 (fast, no browser)**:
1. **Searches** for the IR site (`"<legal name>" investor relations annual report`).
2. **Identifies** the real corporate IR homepage — filters out Wikipedia, quote
   sites, forums, SEDAR+, news wires (`blocklist.txt`); scores domains by
   name-token / acronym match (so `rbc.com` beats `rbcroyalbank.com`); recognises
   hosted IR platforms (Q4 Inc.).
3. **Crawls** up to 3 hops (following into `ir.`/`investors.` subdomains) toward a
   "Reports & Filings / Annual Report" page, scoring `.pdf` links by report
   keywords + most recent year (meeting minutes, circulars, quarterlies rejected).
4. If the crawl finds nothing, runs one **direct PDF search**
   (`"<name>" annual report filetype:pdf`) and keeps only hits on the company's
   own domain.
5. **Validates** the best candidate (HTTP HEAD → ranged GET → `%PDF` sniff).

Then **Tier 2 (headless Chromium, Playwright)** runs only over the companies
Tier 1 couldn't resolve, re-running the same heuristics on the *rendered* DOM so
JavaScript-built IR portals become reachable. You see Tier-1 results land first,
then Tier-2 grinds the remainder. Skipped automatically (with an install hint) if
Playwright isn't installed — Tier 1 still works standalone.

Everything is **resumable**: progress is written per company, re-running skips
rows already marked `found`, and a crash or rate-limit ban loses no work.

### SEC filers are flagged, not chased
Many Canadian issuers (Shopify, Tilray, …) file their financials with the **SEC**
(10-K / 40-F / 20-F) instead of posting a PDF annual report on their own site.
After the first-party cascade above fails for such a company, a final pass checks
SEC EDGAR (via the free `company_tickers.json` + submissions API — never SEDAR).
If it's an SEC filer, the row is marked `status='not_found'`, `sec_filer=1`,
`failure_reason='sec_filer'`, with `sec_filing_url` pointing at the **latest
10-K/40-F/20-F on EDGAR** — and it's kept **out of the manual-review queue** (the
end-of-run summary counts these separately). A first-party PDF is always
preferred: if the IR site does host one, that wins and the row is a normal
`found`. List them with:
`SELECT ticker, sec_filing_url FROM filings WHERE sec_filer = 1;`

### CSE (XCNQ) companies — exchange filings fallback
CSE-listed micro-caps frequently have no usable IR website. For a CSE/XCNQ
company with no first-party PDF, a fallback pass queries the exchange's own
public API (`thecse.com/api/webapi/company`) and records the latest
`ANNUAL_FINANCIAL_STATEMENTS` PDF, tagged `discovery_method='cse_filings'`. These
documents originate from SEDAR but are **mirrored on the CSE's own servers**
(`sedar-filings-backup.thecse.com`) — `sedarplus.ca` is never accessed. Because
the provenance differs from a company-hosted PDF, these are counted under `found`
but reported separately in the run summary and filterable via
`SELECT ticker, pdf_url FROM filings WHERE discovery_method LIKE 'cse_filings%';`.
First-party IR PDFs are always preferred and used when found.

### Verified vs. unverified finds
Many IR CDNs (Akamai/Cloudflare) return **403 to any non-browser client**, so a
real annual-report PDF can't be HEAD-validated. When a confident candidate on the
company's **own domain** with an annual-report filename is blocked this way, it's
recorded as `found` but tagged `discovery_method = '…+unverified'` (with
`failure_reason = unverified_403`) so you can tell it apart from a confirmed PDF.
The URL is almost certainly correct — it just blocks bots. Filter for these with:
`SELECT ticker, pdf_url FROM filings WHERE discovery_method LIKE '%unverified%';`

## Pilot vs. full run

- **Pilot (default).** Running with no mode flag samples ~40 companies spread
  across the market-cap range (large-cap TSX → micro-cap TSXV) so you can tune
  heuristics before committing to the full list. Override the size with
  `--sample-size N`. The default-is-pilot design means you can't kick off a
  ~2,500-row run by accident.
- **Full run.** Add `--full` to process the entire input file. Expect it to run
  for a few hours — the polite, serialised DuckDuckGo search stage is the
  pace-setter (this is deliberate; hammering DuckDuckGo gets you IP-blocked).
- **Resume — run it more than once.** `--resume` (or re-running `--full`)
  continues against the existing `filings.db`, retrying only rows that aren't
  `found`. Because DuckDuckGo returns a slightly different result set each time,
  **each resume pass resolves a different subset** — found rows stick, so the
  union climbs toward near-total on large/mid-caps. Two or three passes is normal.
- **Tier-2 only.** `--render-only` runs just the headless-browser pass over the
  current unresolved rows (handy after tuning, or to add rendering to a DB you
  first built with `--no-render`). `--no-render` skips Tier 2 entirely.

## Inspecting results

```bash
sqlite3 filings.db "SELECT status, COUNT(*) FROM filings GROUP BY status;"
sqlite3 filings.db "SELECT ticker, pdf_url, fiscal_year_guess FROM filings WHERE status='found' LIMIT 20;"
sqlite3 filings.db "SELECT ticker, failure_reason FROM filings WHERE status='needs_review';"
```

## Configuration

Defaults live in `config.example.yaml` and work out of the box. To customise,
copy it to `config.yaml` (git-ignored) and pass `--config config.yaml`, or edit
in place. Notable knobs:

- `search.provider` — `duckduckgo` (default, keyless, finishes in one session)
  or `google_cse`.
- `search.min_delay_seconds`, `search.backoff_*` — search politeness/backoff.
  If DuckDuckGo throttles too often during your pilot, raise `min_delay_seconds`.
- `crawl.max_concurrency`, `crawl.per_domain_delay_seconds`, `crawl.max_hops`.
- `blocklist_path` — points at `blocklist.txt` (edit to add non-corporate hosts).

### Optional: using Google Custom Search instead

Google's official Custom Search JSON API is more reliable but its **free tier
caps at 100 queries/day**, so it can't finish the ~2,500 list in one session.
To use it anyway (e.g. for a small batch):

1. Create an API key and a Custom Search Engine (set to "search the entire web")
   at <https://programmablesearchengine.google.com/>.
2. In `config.yaml` set `search.provider: google_cse` and fill in
   `search.google_cse.api_key` / `.cx` — or export `GOOGLE_CSE_API_KEY` and
   `GOOGLE_CSE_CX`. The pipeline fails fast with a clear message if they're missing.

## Files

| File | Role |
|------|------|
| `run.py` | CLI entrypoint (the only command you need). |
| `search_provider.py` | Pluggable search backends (DuckDuckGo, Google CSE) + backoff. |
| `discover_ir.py` | IR-homepage identification + platform detection. |
| `crawl_pdf.py` | Tier-1 deep crawl + PDF candidate scoring (shared helpers). |
| `pdf_search.py` | Tier-1 direct `filetype:pdf` fallback (own-domain only). |
| `render.py` | Tier-2 headless-browser (Playwright) render pass. |
| `sec_edgar.py` | SEC-filer detection + latest-annual-filing lookup (EDGAR). |
| `cse_filings.py` | CSE/XCNQ exchange-filings fallback (thecse.com API). |
| `validate.py` | PDF reachability check + verified/blocked/fail classification. |
| `pipeline.py` | Two-tier orchestration, concurrency, SQLite checkpointing. |
| `ingest.py` | Loads the GuruFocus `.xlsx` or a CSV. |
| `schema.sql` | SQLite schema. |
| `config.example.yaml` | Settings template (no real keys). |
| `blocklist.txt` | Editable non-corporate-domain blocklist. |

## Scope / non-goals

Does **not** access SEDAR+, bypass logins/paywalls/CAPTCHAs, or aim for 100%
coverage. The target is a strong majority resolved plus a clean, actionable
`needs_review` list for the rest. Large/mid-cap coverage should be high; thin
or defunct TSXV micro-cap sites will land in review — that's expected.

SEC EDGAR **is** used (it's the official first-party source for US/cross-listed
filers, and only as a fallback pointer once a first-party PDF can't be found) —
SEDAR+ is never touched.

Playwright powers the Tier-2 render pass for JS-heavy IR sites. It only runs over
companies Tier 1 couldn't resolve, so its cost scales with the leftovers, not the
whole list. Disable it with `--no-render` if you want the light/fast path only.
