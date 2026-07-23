# Line-item audit — methodology & findings

A read-only tool (`src/audit_lineitems.py`) that reconciles the generated
spreadsheet output against the source PDFs, cell by cell, so a human only reviews
a short flagged list instead of eyeballing ~200 template cells against a 90-page
filing. Run: `python src/audit_lineitems.py TICKER[,TICKER...] [--years Y,Y]`.
Per-company detail is written to `docs/audit/<TICKER>_audit.csv`.

## What it checks (three axes)

- **Axis A — mapped-value accuracy.** For every mapped cell, reproduce the
  pipeline's own store-time transform (scale × sign rules) from the printed page
  number and confirm it lands on the stored value; independently re-check every
  sign against the parentheses on the page.
- **Axis B — zero-fill correctness (priority).** For every `0` cell, search the
  statement FACE for the concept's aliases. A face hit with a real number is a
  **BUG_FACE** (the 0 should be a value — a missed mapping). A notes-only hit is
  **NOTE_ONLY** (low severity — the zero-fill contract only claims face absence).
- **Axis C — derived soundness.** Re-run the five accounting identities that
  produced derived cells.

Verdicts: `PASS` / `BUG_FACE` / `MISMATCH` / `SUSPECT` / `NOTE_ONLY`.

## First pass — 5 companies (WSP, RY, K, AAB, ACQ), all years

The tool paid for itself immediately: alongside real pipeline bugs it also
surfaced (and I then corrected) two bugs *in the audit itself* during bring-up
(a false sign-mismatch from checking the wrong source document's face text; a
mis-located candidate line when a key had multiple `line_items_full` rows).

### Confirmed pipeline bug — FIXED: unparenthesized note references

The biggest finding. A trailing **unparenthesized** note reference defeated label
matching: `Property, plant and equipment Note 7 and 8` → normalized to
`property plant and equipment note 7 and` → **no match** → `canonical_key` NULL →
the key (`NetPPE`) got zero-filled. Parenthesized `(note 9)` stripped fine, but
bare `Note N` / `Note N and M` / `– Note N` did not.

- Root cause: `_normalize_label` (`src/rule_extract.py`) only stripped
  parenthesized note refs and a single trailing digit token.
- Fix: added `_NOTE_TAIL_WORD_RE` to strip trailing `note(s) N [and/,/to N…]`
  even without parentheses.
- **Corpus-wide impact: 3,197 currently-unmapped rows — each with a real value —
  now map correctly** (e.g. `Share capital – Note 6` → CapitalStock,
  `Marketable securities – Note 5` → ShortTermInvestments, `Right of use asset –
  Note 8` → NetPPE, `Repayment of debt Note 12` → RepaymentOfDebt). Very common
  in small-cap Canadian filings (em-dash "– Note N" style).
- Low risk: only turns unmapped→mapped or refines a fuzzy match to the same
  concept; never invents a value. 107 tests still pass; verified no over-strip
  (`Notes receivable` still → NotesReceivable).

### Other confirmed issues — deferred (noted, not fixed this pass)

- **Numbers glued into labels** (`Contributed surplus 84.5` — the value fused
  into the label text). A step-2 text-extraction/tokenization issue, not a
  normalization fix; harder and riskier. Seen on some older K/AAB filings.
- **Suffix over-reach** (`Other operating expense` singular → `OperatingExpense`
  the grand total, via suffix salvage). Tightening the suffix rule is risky;
  deferred pending its own verification.
- **Cash-flow / balance identity failures** (Axis C) on a cluster of old filings
  (K 2009–2011 bank-era statements; several AAB micro-cap years; WSP 2021–2022
  cash-flow reconciliation). Mostly old/thin filings; each needs individual
  investigation before any fix.

### Confirmed AUDIT false positives (acceptable review noise, not pipeline bugs)

- **Dash/nil comparative** — WSP 2024 non-controlling interests is genuinely `—`
  (nil) that year; the audit saw the prior-year comparative number on the face.
- **Bank template** — RY `InterestIncomeNonOperating` = 0 is correct; a bank's
  "Interest and dividend income" maps to the bank key `InterestIncome`.
- **Equity-statement concepts** — WSP `AdditionalPaidInCapital` ("Contributed
  surplus") lives in the changes-in-equity statement, not the balance-sheet face.

## Status of the deliverable
- Tool + unit tests committed; full suite green (107 passed).
- Note-reference fix applied; full step-4 remap run to propagate it; audit
  re-run to confirm the flagged cells clear with no new regressions.
- Per-company reconciliation CSVs in this directory.
