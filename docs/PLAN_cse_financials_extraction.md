# Plan: CSE (XCNQ) financial-statement extraction from PDFs

## Context & goal
The end goal of this project is structured financial data (income statement,
balance sheet, cash-flow statement) for **every** company on the GuruFocus
list.

- **TSX / TSXV** — solved. `tmx_financials.py` pulls clean structured JSON
  straight from QuoteMedia's `getFinancialsEnhancedBySymbol.json` API. No PDF
  parsing needed.
- **SEC cross-listed** — future; EDGAR exposes structured XBRL (well-documented
  JSON API), so also no PDF parsing.
- **CSE (XCNQ)** — the remaining gap and the subject of this plan. These
  micro-caps have **no structured data feed anywhere**; the only source is the
  annual-financial-statement **PDF** the pipeline already resolves via
  `cse_filings.py` (stored in `filings.db`, `discovery_method='cse_filings'`,
  `pdf_url` = a `sedar-filings-backup.thecse.com/...` link).

The user's overall approach for CSE:
1. Download the PDF (URL already in `filings.db`).
2. **Scrape/extract the three financial-statement sections as text** ← *this plan*.
3. (Later) feed that text to a **local LLM** that maps it to a JSON schema.

This plan covers **step 2 only** — a module that turns a CSE annual-statement
PDF into clean, per-statement text blocks ready for the LLM. It deliberately
stops short of the LLM mapping.

## What I verified on real CSE PDFs (grounding, not assumption)
Downloaded and analyzed 3 real ones pulled from `filings.db`
(Quizam Media `QQ`, Argo Gold `ARQ`, Mosaic Minerals `MOC`) with `pdfplumber`:

| PDF | pages | ~chars/page | extractable? |
|-----|-------|-------------|--------------|
| Quizam | 35 | 2198 | yes (text-based) |
| Argo | 31 | 2405 | yes (text-based) |
| Mosaic | 28 | 1999 | yes (text-based) |

**Decisive findings:**
1. **`extract_text()` output is excellent and LLM-ready.** A balance-sheet
   page comes out as clean lines: `Cash and cash equivalents 50,056 196,362`
   (label + current-year + prior-year). This is directly consumable by an LLM.
2. **`extract_table()` returns `None`.** These IFRS statements have **no
   ruled/bordered tables** — columns are aligned purely by whitespace/x-position.
   → **Table-extraction libraries (camelot, tabula, pdfplumber `extract_table`)
   are the wrong tool.** The right approach is **text extraction**, and the LLM
   (step 3) does the structuring. This is the single most important design call.
3. **Section-header keywords appear on many pages**, not just the statement:
   the table of contents, the auditor's report, the actual statement, AND the
   notes all say e.g. "statement of financial position". Naive
   "first page matching the regex" is unreliable.
4. **Spacing/word-merge is inconsistent.** Mosaic's cash-flow header wasn't
   matched by a `\s`-based regex because words extract merged
   (`"Cash flowsused inoperatingactivities"`). → Header detection must be
   whitespace-insensitive (strip/collapse spaces before matching).
5. **Minor decode artifacts** (e.g. en-dash → `�`). LLM-tolerant; leave as-is.
6. **Not sampled but must be handled: scanned/image PDFs.** Some CSE micro-caps
   file a scanned signed copy with no text layer. Detectable via the same
   chars-per-page heuristic (`< ~100 chars/page ⇒ scanned`) that
   `verify_pdf.py` already uses conceptually.

## Design

### New module: `cse_extract.py`
Single-responsibility: **PDF bytes → per-statement text blocks.** No network,
no DB, no LLM — those are the caller's job (keeps it unit-testable offline with
the sample PDFs).

Primary entry point:
```python
def extract_statements(pdf_bytes: bytes) -> ExtractResult
```
Returns a dataclass:
```python
@dataclass
class ExtractResult:
    ok: bool                     # False only when nothing usable came out
    scanned: bool                # True if no text layer (OCR needed / used)
    npages: int
    sections: dict[str, str]     # {"income_statement": "...text...",
                                 #  "balance_sheet": "...", "cash_flow": "..."}
    primary_block: str           # full text of the primary-statements span,
                                 # used as fallback when a section can't be
                                 # isolated — the LLM can still find it here
    reason: str | None           # why ok=False / notes (e.g. "scanned_no_ocr")
```
Rationale for returning **both** isolated `sections` and the whole
`primary_block`: section isolation is best-effort (finding #3/#4 above make it
imperfect). Handing the LLM the isolated section when we're confident, and the
whole primary-statements span when we're not, means a fuzzy boundary never
loses data — the LLM tolerates extra surrounding text far better than missing
lines.

### Extraction algorithm (text-based)
1. **Load with `pdfplumber`**, extract per-page text into a `list[str]`.
   (Prefer `pdfplumber` over the existing `pypdf` path in `verify_pdf.py` — its
   layout fidelity on these statements is visibly better; keep `pypdf` only as a
   secondary if `pdfplumber` yields nothing.)
2. **Scanned detection:** if `total_chars < npages * 100`, mark `scanned=True`
   and route to the OCR branch (below).
3. **Find anchor markers** (whitespace-insensitive: lowercase + collapse runs of
   whitespace before matching):
   - `auditor_page` = first page matching `independent auditor` /
     `auditor'?s report` / `report of independent` → **start** of the primary
     statements (statements always follow the audit opinion).
   - `notes_page` = first page after `auditor_page` matching
     `notes to (the )?(consolidated )?financial statements` → **end** of the
     primary statements block.
   - `primary_block` = concatenated text of pages `(auditor_page, notes_page)`.
     In all 3 samples this is the ~4 consecutive statement pages.
4. **Isolate each statement within the primary block**, best-effort, using
   header regexes matched against *collapsed-whitespace* page text:
   - `balance_sheet`: `statements? of financial position` | `balance sheet`
   - `income_statement`: `statements? of (loss|income|operations|comprehensive
     (income|loss)|profit)` (CSE issuers vary wildly here — cover all)
   - `cash_flow`: `statements? of cash ?flows?`
   Assign each in-range page to the statement whose header appears at/near its
   **top** (headers sit in the first ~3 lines of the actual statement page;
   this disambiguates from the many in-text mentions inside notes). A statement
   may span 2 pages → include the following page if it has no new statement
   header.
5. **Fallback:** any statement not confidently isolated → leave its
   `sections[...]` empty; `primary_block` still carries it for the LLM.
6. `ok = True` if `primary_block` is non-empty (or OCR produced text);
   else `ok=False, reason="no_primary_statements_found"`.

### OCR branch (scanned PDFs)
- Gate behind a config flag + graceful skip if deps missing (mirror how
  `render.py`/Playwright is optional): if OCR libs absent, return
  `ok=False, scanned=True, reason="scanned_no_ocr"` so the company is simply
  flagged "needs OCR", never crashes the batch.
- Implementation: `pdf2image` (render pages to images) → `pytesseract`
  (Tesseract OCR) → same text-based algorithm on the OCR'd text. Requires the
  external **Tesseract** binary + **Poppler** (document as one-time installs,
  like `playwright install chromium`).
- Keep OCR opt-in and last-resort; the majority sampled were text-based.

### Integration (thin caller — separate file, NOT inside `cse_extract.py`)
A small runner (e.g. extend `run.py` with a `--extract-financials` mode, or a
new `run_financials.py`) that:
1. Reads `filings.db` for rows where
   `discovery_method LIKE 'cse_filings%' AND pdf_url IS NOT NULL`.
2. Downloads each PDF with the **existing** `httpx` client + browser-UA pattern
   already used in `verify_pdf.py` / `validate.py` (reuse, don't reinvent;
   these CSE hosts already work with that UA).
3. Calls `cse_extract.extract_statements(pdf_bytes)`.
4. Persists the result. **New table** `cse_financials_raw` (the single-PDF
   `filings` schema doesn't fit multi-section text):
   ```sql
   CREATE TABLE IF NOT EXISTS cse_financials_raw (
       ticker TEXT PRIMARY KEY,
       pdf_url TEXT,
       scanned INTEGER DEFAULT 0,
       income_statement TEXT,
       balance_sheet TEXT,
       cash_flow TEXT,
       primary_block TEXT,
       extract_ok INTEGER,
       reason TEXT,
       last_extracted TIMESTAMP
   );
   ```
   This becomes the **input to the later LLM step**, which reads these text
   columns and writes structured JSON to a further table.
5. Concurrency: plain HTTP + CPU-bound `pdfplumber` parse. Reuse the semaphore
   pattern; a modest bound (e.g. 8) since parsing is CPU work, not just I/O.

## Dependencies
- **Add now:** `pdfplumber` (already present in the working `python` 3.10 env,
  but add to `requirements.txt` so it's declared).
- **OCR (optional, document as extra):** `pytesseract`, `pdf2image`, plus the
  external Tesseract + Poppler binaries. Keep out of the base install path;
  guard imports so the module works without them (text-based PDFs need neither).
- Note the env caveat: this repo's `python` → `C:\Python310` has `pdfplumber`
  + `pypdf` but **not** `httpx`; the download-caller must run where `httpx` is
  installed. Worth reconciling the env story (CLAUDE.md still claims Store
  Python 3.13) as part of this work.

## Edge cases & risks
- **Statement wording varies a lot** across CSE issuers (loss vs operations vs
  comprehensive income; "financial position" vs "balance sheet"). Mitigated by
  broad regexes + the `primary_block` fallback so nothing is lost even on a miss.
- **Two-page statements** (long balance sheets / cash flows) → continuation-page
  rule (step 4) handles it.
- **Multi-statement pages** (a tiny issuer crams position+loss on one page) →
  that page's text lands in `primary_block`; the LLM separates them. Don't
  over-engineer splitting.
- **Comparative-only / restated columns** → irrelevant to extraction; passed
  through verbatim for the LLM to interpret.
- **Wrong PDF resolved** (e.g. an MD&A, not the statements) → `cse_filings.py`
  already prefers `ANNUAL_FINANCIAL_STATEMENTS`; additionally, `ok=False` when
  no auditor/notes anchors are found gives a natural quality signal to review.
- **Scanned PDFs** → detected + flagged; OCR optional. Quantify their share by
  running the char/page heuristic across all CSE rows before committing to OCR.

## Verification
1. **Offline unit test** on the 3 sample PDFs already in the scratchpad (add
   them, or a few, as fixtures): assert `ok=True`, `scanned=False`, and that
   each `sections[...]` contains an expected anchor line (e.g. balance_sheet
   contains `total assets`, cash_flow contains `operating activities`).
2. **Mosaic is the deliberate hard case** (its cash-flow header defeated a naive
   regex) — assert the whitespace-insensitive matcher now catches it, or that
   `primary_block` contains the cash-flow lines regardless.
3. **Batch dry-run** over all `cse_filings` rows in `filings.db`: report
   `ok` / `scanned` / `not-found` counts and a per-section fill rate, to size
   how much (if any) OCR work is actually needed before building it.
4. Eyeball 3–5 `primary_block` outputs to confirm they're clean enough to hand
   an LLM.

## Phasing
1. `cse_extract.py` + text-based algorithm + offline unit tests on the samples. ← build first
2. The download+persist caller (`--extract-financials` / new table).
3. Batch dry-run to measure success + scanned share.
4. OCR branch **only if** the dry-run shows a meaningful scanned tail.
5. (Separate, later) the local-LLM schema-mapping step that consumes
   `cse_financials_raw`.
