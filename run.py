#!/usr/bin/env python3
"""Canadian Annual Report PDF Finder — single CLI entrypoint.

Runs discovery -> crawl -> validate -> store end to end.

    python run.py --input "Canadian companies.xlsx"            # pilot (default)
    python run.py --input "Canadian companies.xlsx" --full     # whole list
    python run.py --input "Canadian companies.xlsx" --resume   # continue a run

--pilot is the default so a full ~2,500-row run can't start by accident.
--resume is implicit whenever the SQLite DB already exists: 'found' rows are
always skipped regardless of flag.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# The project modules live in src/ (kept flat so their inter-imports stay bare,
# e.g. `from crawl_pdf import ...`). Put src/ on the path before importing them.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

DEFAULT_CONFIG = Path(__file__).with_name("config.example.yaml")


def _load_config(path: Path) -> dict:
    import yaml

    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _pilot_sample(companies: list, size: int) -> list:
    """Evenly-spaced sample across the (market-cap-ordered) list so the pilot
    spans large-cap TSX at the top through micro-cap TSXV at the bottom."""
    n = len(companies)
    if size >= n:
        return companies
    step = n / size
    idxs = sorted({min(n - 1, int(i * step)) for i in range(size)})
    return [companies[i] for i in idxs]


def _next_batch(companies: list, n: int, db_path: str) -> tuple[list, int]:
    """The next `n` companies (in list order) NOT yet processed (no row in the
    filings table). Lets you chip through the full list a batch at a time -- each
    run advances past whatever's already been attempted. Returns (batch, total_pending)."""
    import sqlite3
    processed: set[str] = set()
    if Path(db_path).exists():
        conn = sqlite3.connect(db_path)
        try:
            processed = {r[0] for r in conn.execute("SELECT ticker FROM filings")}
        except sqlite3.OperationalError:
            pass  # table not created yet -> nothing processed
        finally:
            conn.close()
    pending = [c for c in companies if c.ticker not in processed]
    return pending[:n], len(pending)


def _validate_provider_ready(cfg: dict) -> None:
    """Fail fast (before any work) if the chosen provider lacks credentials."""
    import os

    provider = (cfg.get("search", {}).get("provider") or "duckduckgo").lower()
    if provider in ("google_cse", "google"):
        gcfg = cfg.get("search", {}).get("google_cse", {}) or {}
        key = gcfg.get("api_key") or os.environ.get("GOOGLE_CSE_API_KEY", "")
        cx = gcfg.get("cx") or os.environ.get("GOOGLE_CSE_CX", "")
        if not key or not cx:
            _die(
                "Search provider 'google_cse' is selected but its credentials "
                "are missing.\nSet search.google_cse.api_key and .cx in your "
                "config.yaml, or export GOOGLE_CSE_API_KEY and GOOGLE_CSE_CX.\n"
                "(Tip: the default 'duckduckgo' provider needs no key at all.)"
            )


def _die(msg: str) -> None:
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Find Canadian companies' annual-report PDFs.")
    ap.add_argument("--step", type=int, choices=(1, 2, 3, 4), default=1,
                    help="The 4-step workflow: 1 (default) = find annual-report PDF "
                         "LINKS (10-yr history; no QuoteMedia unless --with-financials). "
                         "2 = download PDFs -> extract raw statement TEXT -> delete file. "
                         "3 = separate the text into individual LINE ITEMS "
                         "(raw_line_items -- every line kept). "
                         "4 = map line items onto the canonical SCHEMA "
                         "(pdf_financials.db + per-company xlsx). "
                         "Steps 2-4 read from the DB and need no --input.")
    ap.add_argument("--input", required=False,
                    help="Company list (.xlsx GuruFocus export or ticker,name,exchange CSV). "
                         "Required for --step 1; ignored for --step 2.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--pilot", action="store_true",
                      help="Run a small sampled subset (default mode).")
    mode.add_argument("--full", action="store_true", help="Run the entire input file.")
    mode.add_argument("--resume", action="store_true",
                      help="Continue against the existing DB (same as full; found rows skipped).")
    mode.add_argument("--next", type=int, default=None, metavar="N",
                      help="Process the next N companies not yet in the DB (chip "
                           "through the full list a batch at a time; each run advances).")
    ap.add_argument("--sample-size", type=int, default=None,
                    help="Pilot sample size (default from config, ~40).")
    ap.add_argument("--force", action="store_true",
                    help="(--step 3/4) Re-process rows already done.")
    ap.add_argument("--limit", type=int, default=None,
                    help="(--step 3/4) Process at most N rows (smoke testing).")
    ap.add_argument("--tickers", default=None,
                    help="(--step 3/4) Comma-separated ticker allow-list.")
    ap.add_argument("--method", choices=("rules", "llm"), default="rules",
                    help="(--step 4) Mapping method. 'rules' (default) = "
                         "deterministic matcher, no model needed. 'llm' = legacy "
                         "local-Ollama path (reads statement text directly).")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="Config YAML (defaults to config.example.yaml).")
    ap.add_argument("--years", type=int, default=None,
                    help="(--step 1) How many years of annual filings to collect "
                         "per company (default 10, from pipeline.FILING_YEARS).")
    ap.add_argument("--no-render", action="store_true",
                    help="Skip the Tier-2 Playwright render pass (Tier 1 only).")
    ap.add_argument("--render-only", action="store_true",
                    help="Run ONLY the Tier-2 render pass over rows the fast tier "
                         "left unresolved in the existing DB.")
    ap.add_argument("--skip-financials", action="store_true",
                    help="(deprecated, now the DEFAULT) Step 1 skips the QuoteMedia "
                         "API and finds PDF links directly. Kept as a no-op.")
    ap.add_argument("--with-financials", action="store_true",
                    help="(--step 1) Legacy combined flow: QuoteMedia structured "
                         "financials first, then the PDF finder only on the tail "
                         "QuoteMedia missed.")
    ap.add_argument("--overwrite", action="store_true",
                    help="(--step 1) Re-process companies already marked 'found' "
                         "(default skips them). Resets their status + clears old PDF "
                         "links so the finder re-runs and overwrites the results.")
    ap.add_argument("--financials", action="store_true",
                    help="Run ONLY the structured-financials stage (TSX/TSXV/CSE/XCNQ/"
                         "NEOE via QuoteMedia -- CSE/XCNQ via a :CNX symbol suffix, "
                         "NEOE via an ATS suffix like :OMG, ~99%% coverage) and stop -- "
                         "skips the PDF finder entirely. Without this flag, the default "
                         "run does financials first anyway, then falls through to the "
                         "PDF finder only for whatever those exchanges didn't cover. "
                         "Writes to its own DB (see tmx_financials.db_path in config), "
                         "separate from filings.db.")
    args = ap.parse_args()

    # --- fail-fast validation ------------------------------------------------
    if not args.config.exists():
        _die(f"Config file not found: {args.config}")
    cfg = _load_config(args.config)

    # Step 2: PDF processing (download -> extract -> delete). Reads filing_pdfs
    # from the DB, needs no --input, and stops without touching the finder.
    if args.step == 2:
        from pdf_pipeline import run_pdf_processing
        db_path = cfg.get("storage", {}).get("db_path", "output/filings.db")
        print(f"Step 2: PDF processing  |  DB: {db_path}")
        result = asyncio.run(run_pdf_processing(cfg, progress=print))
        print("\n" + "=" * 44)
        print("  PDF processing complete")
        print("=" * 44)
        print(f"  PDFs attempted : {result['attempted']}")
        print(f"  extracted ok   : {result['extracted']}")
        print(f"  scanned (OCR)  : {result['scanned']}")
        print(f"  failed         : {result['failed']}")
        if result.get("sec_skipped"):
            print(f"  SEC/EDGAR excluded (not a PDF, no data needed) : "
                  f"{result['sec_skipped']}  (not a failure -- already on EDGAR)")
        denom = result['extracted'] + result['failed']
        if denom:
            print(f"  success rate   : {result['extracted']/denom*100:.1f}%  "
                 f"({result['extracted']}/{denom}, excluding SEC/EDGAR above)")
        print(f"  elapsed        : {result['elapsed']:.1f}s")
        print(f"  stored in      : {result['db_path']} (table 'pdf_extractions')")
        print("=" * 44)
        return

    # Step 3: separate raw statement text into individual LINE ITEMS.
    if args.step == 3:
        from line_items import run_line_item_parsing
        tickers = ({t.strip() for t in args.tickers.split(",") if t.strip()}
                   if args.tickers else None)
        db_path = cfg.get("storage", {}).get("db_path", "output/filings.db")
        print(f"Step 3: line-item parsing  |  DB: {db_path}")
        result = run_line_item_parsing(cfg, force=args.force, limit=args.limit,
                                       tickers=tickers, progress=print)
        print("\n" + "=" * 44)
        print("  Line-item parsing complete")
        print("=" * 44)
        print(f"  pdf rows parsed   : {result['pdf_rows']}")
        print(f"  statements parsed : {result['statements']}")
        print(f"  line items stored : {result['lines']}  (table 'raw_line_items' -- EVERY line kept)")
        print(f"  empty statements  : {result['empty']}")
        print(f"  elapsed           : {result['elapsed']:.1f}s")
        print("=" * 44)
        print("  Next: python run.py --step 4   (map line items onto the schema)")
        return

    # Step 4: map line items onto the canonical schema (rules default).
    if args.step == 4 and args.method == "rules":
        from schema_map import run_schema_mapping
        src_db = cfg.get("storage", {}).get("db_path", "output/filings.db")
        fin_db = cfg.get("rules", {}).get("db_path") or "output/pdf_financials.db"
        tickers = ({t.strip() for t in args.tickers.split(",") if t.strip()}
                   if args.tickers else None)
        print(f"Step 4: schema mapping  |  read {src_db} -> write {fin_db}")
        result = run_schema_mapping(cfg, force=args.force, limit=args.limit,
                                    tickers=tickers, progress=print)
        print("\n" + "=" * 44)
        print("  Schema mapping complete")
        print("=" * 44)
        print(f"  tickers                   : {result['tickers']}")
        print(f"  statements mapped         : {result['parsed_ok']}")
        print(f"  needs review (low conf)   : {result['needs_review']}")
        print(f"  skipped (already done)    : {result['skipped']}")
        print(f"  new companies             : {result['companies']}")
        print(f"  company-years written     : {result['years']}")
        print(f"  canonical lines written   : {result['lines']}")
        print(f"  ALL lines kept (full)     : {result['full_lines']}  (table 'line_items_full')")
        print("  " + "-" * 42)
        print("  QUALITY")
        print(f"  label map rate            : {result['label_map_rate_pct']:.1f}%")
        bp, bc = result['balance_identity_pass'], result['balance_identity_checked']
        print(f"  balance-sheet identity    : {bp}/{bc} pass"
              f"{'  (' + f'{bp/bc*100:.0f}%)' if bc else ''}")
        print(f"  mean statement confidence : {result['mean_confidence']:.2f}")
        print(f"  elapsed                   : {result['elapsed']:.2f}s")
        print(f"  stored in                 : {result['db_path']}")
        print("=" * 44)
        # Auto-export per-company workbooks (template + All Line Items sheet).
        import subprocess
        out_dir = "output/pdf_financials_xlsx"
        r = subprocess.run([sys.executable, str(Path(__file__).parent / "src" / "company_xlsx_export.py"),
                            "--all", "--db", fin_db, "--out-dir", out_dir],
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        return

    if args.step == 4:
        from llm_extract import run_llm_extraction
        src_db = cfg.get("storage", {}).get("db_path", "output/filings.db")
        fin_db = cfg.get("tmx_financials", {}).get("db_path", "output/financials.db")
        tickers = ({t.strip() for t in args.tickers.split(",") if t.strip()}
                   if args.tickers else None)
        print(f"Step 4 (legacy LLM): read {src_db} -> write {fin_db}")
        result = run_llm_extraction(cfg, force=args.force, limit=args.limit,
                                    tickers=tickers, progress=print)
        print("\n" + "=" * 44)
        print("  LLM extraction complete")
        print("=" * 44)
        print(f"  statement-calls attempted : {result['attempted']}")
        print(f"  parsed ok                 : {result['parsed_ok']}")
        print(f"  invalid / errored         : {result['invalid']}")
        print(f"  skipped (done/empty)      : {result['skipped']}")
        print(f"  new companies             : {result['companies']}")
        print(f"  company-years written     : {result['years']}")
        print(f"  statement lines written   : {result['lines']}")
        print(f"  values dropped (unverified vs source text) : "
              f"{result['unverified_dropped']}")
        print(f"  cross-year consistency warnings : {result['consistency_warnings']} "
              f"(see table 'pdf_llm_consistency')")
        print(f"  parse success rate        : {result['parse_success_rate_pct']:.1f}%  "
              "(parsed ok / (parsed ok + invalid), excludes skipped)")
        print(f"  value verification rate   : {result['value_verification_rate_pct']:.1f}%  "
              "(values written / (written + dropped))")
        print(f"  actual LLM round-trips    : {result['llm_calls']}  "
              f"({result['avg_seconds_per_call']:.1f}s/call avg)")
        print(f"  elapsed                   : {result['elapsed']:.1f}s")
        print(f"  stored in                 : {result['db_path']}")
        print("=" * 44)
        print("  Next: python src/company_export.py --all   "
              "(regenerate every output/companies/<TICKER>.json)")
        return

    if not args.input:
        _die("--step 1 needs --input (the company list). "
             "(--step 2 reads filing_pdfs from the DB and needs no --input.)")
    input_path = Path(args.input)
    if not input_path.exists():
        _die(f"Input file not found: {input_path}\n"
             "Pass the GuruFocus .xlsx export or a ticker,legal_company_name,exchange CSV.")

    # Imports deferred until after arg validation so `--help` works w/o deps.
    from ingest import load_companies

    try:
        companies = load_companies(input_path)
    except Exception as exc:  # noqa: BLE001
        _die(f"Could not read input: {exc}")

    from financials_pipeline import is_tmx_exchange

    # --financials scopes the whole run (including pilot sampling) to just
    # the exchanges it can fetch, since that's ALL this mode does.
    if args.financials:
        before = len(companies)
        companies = [c for c in companies if is_tmx_exchange(c.exchange)]
        print(f"{before} companies loaded; {len(companies)} are TSX/TSXV/CSE/"
             "XCNQ/NEOE (the only exchanges --financials handles).")

    # Mode selection: pilot is the default unless --full/--resume/--next given.
    full_mode = args.full or args.resume
    if args.next:
        db_path = cfg.get("storage", {}).get("db_path", "output/filings.db")
        selected, total_pending = _next_batch(companies, args.next, db_path)
        mode_label = f"NEXT {len(selected)} (of {total_pending} not-yet-processed)"
        if not selected:
            print("Nothing left to process -- every company in the input is "
                  "already in the DB. (Use --overwrite to redo them.)")
            return
    elif full_mode:
        selected = companies
        mode_label = "FULL" + (" (resume)" if args.resume else "")
    else:
        size = args.sample_size or int(cfg.get("pilot", {}).get("default_sample_size", 40))
        selected = _pilot_sample(companies, size)
        mode_label = f"PILOT (sample of {len(selected)})"

    def _print_financials_summary(result: dict) -> None:
        excluded = result.get("excluded_non_reporting", 0)
        print("\n" + "=" * 44)
        print("  Financials fetch complete")
        print("=" * 44)
        print(f"  companies (all API-called) : {result['total']}")
        print(f"  resolved                   : {result['resolved']}")
        print(f"  failed                     : {result['failed']}")
        if excluded:
            print(f"  excluded (.P/.UN, no data) : {excluded}  "
                 "(not a failure -- CPC/trust/fund with nothing to report)")
        denom = result['resolved'] + result['failed']
        print(f"  success rate                : {result['success_rate_pct']:.1f}%  "
             f"({result['resolved']}/{denom}, excluding the non-reporting .P/.UN above)")
        print(f"  elapsed                     : {result['elapsed']:.1f}s")
        print(f"  stored in                   : {result['db_path']}")
        print("=" * 44)

    if args.financials:
        # Financials-only mode: run the structured fetch and stop -- no PDF finder.
        from financials_pipeline import run_tmx_financials
        print(f"Mode: {mode_label}  |  financials DB: "
             f"{cfg.get('tmx_financials', {}).get('db_path', 'output/financials.db')}")
        result = asyncio.run(run_tmx_financials(selected, cfg, progress=print))
        _print_financials_summary(result)
        return

    print(f"Mode: {mode_label}  |  {len(selected)} companies selected")

    if args.render_only:
        # Narrow resume of an existing PDF-finder DB (re-render specific
        # unresolved rows) -- not a fresh run, so skip the financials stage.
        remaining = selected
    elif args.with_financials:
        # Legacy flow: structured financials FIRST (QuoteMedia), then the PDF
        # finder only for whatever's left.
        from financials_pipeline import run_tmx_financials
        financials_targets = [c for c in selected if is_tmx_exchange(c.exchange)]
        print(f"\nStage 1/2: structured financials (TSX/TSXV/CSE/XCNQ/NEOE via "
             f"QuoteMedia) on {len(financials_targets)} compan(ies) -> "
             f"{cfg.get('tmx_financials', {}).get('db_path', 'output/financials.db')}")
        fin_result = asyncio.run(run_tmx_financials(financials_targets, cfg, progress=print))
        _print_financials_summary(fin_result)

        resolved_tickers = fin_result.get("resolved_tickers", set())
        remaining = [c for c in selected if c.ticker not in resolved_tickers]
        print(f"\nStage 2/2: PDF finder on {len(remaining)} compan(ies) "
             "not covered by structured financials...")
    else:
        # DEFAULT: PDF links for the selected companies -- step 1 of the
        # 4-step workflow (links -> raw text -> line items -> schema). The
        # QuoteMedia API is NOT called here (use --financials for that, or
        # --with-financials for the legacy combined flow). Passes run
        # CSE -> TMX -> Tier 1 -> Tier 2 -> SEC-flag -> year-pattern probe,
        # so a company's own IR PDFs always win over an SEC note, and
        # year-patterned URLs (ar_2025_e.pdf) are extrapolated back
        # ~10 years.
        remaining = selected
        print(f"\nStep 1 ({mode_label}): PDF links for {len(remaining)} "
              "selected compan(ies) (no QuoteMedia API).")

    _validate_provider_ready(cfg)
    from search_provider import build_provider
    import pipeline
    from pipeline import run_pipeline

    if args.years:
        print(f"Overriding filing-years depth: {pipeline.FILING_YEARS} -> {args.years}")
        pipeline.FILING_YEARS = args.years

    provider_name = (cfg.get("search", {}).get("provider") or "duckduckgo")
    print(f"search provider: {provider_name}  |  "
          f"DB: {cfg.get('storage', {}).get('db_path', 'filings.db')}")

    provider = build_provider(cfg)
    render_default = bool(cfg.get("render", {}).get("enabled", True))
    use_render = render_default and not args.no_render
    summary = asyncio.run(run_pipeline(
        remaining, provider, cfg,
        use_render=use_render, render_only=args.render_only,
        overwrite=args.overwrite, progress=print))

    sec = summary.get("sec_filer", 0)
    cse = summary.get("cse_filings", 0)
    tmx = summary.get("tmx_filings", 0)
    stage_stats = summary.get("_stage_stats", [])
    elapsed_total = summary.get("_elapsed_total", 0.0)
    total = summary["found"] + summary["needs_review"] + summary["not_found"]

    def _fmt(s: float) -> str:
        if s < 60:
            return f"{s:.1f}s"
        m, sec_ = divmod(int(s), 60)
        return f"{m}m{sec_:02d}s"

    def _pct(n: int, d: int) -> float:
        return (n / d * 100.0) if d else 0.0

    if stage_stats:
        print("\n" + "-" * 60)
        print("  Stage breakdown (found / attempted this run)")
        print("-" * 60)
        for label, attempted, resolved, elapsed in stage_stats:
            print(f"  {label:<32} {resolved:>4}/{attempted:<5} "
                 f"({_pct(resolved, attempted):5.1f}%)  {_fmt(elapsed):>7}")
        print("-" * 60)

    print("\n" + "=" * 44)
    print("  Run complete — results by status")
    print("=" * 44)
    print(f"  found          : {summary['found']}")
    if cse:
        print(f"    of which via CSE mirrored-SEDAR filings : {cse}")
    if tmx:
        print(f"    of which via TMX mirrored-SEDAR filings : {tmx}")
    print(f"  needs_review   : {summary['needs_review']}")
    print(f"  not_found      : {summary['not_found']}")
    if sec:
        print(f"    of which SEC cross-listed (flagged, not review) : {sec}")
    print(f"  -----------------------------")
    print(f"  total in DB    : {total}")
    print("=" * 44)
    with_pdf = summary.get("with_pdf_url", 0)
    non_sec_total = total - sec  # SEC cross-listed have no first-party PDF by
                                  # definition, so they're excluded from both
                                  # sides of this ratio, not just the numerator.
    print(f"  resolved (has a real PDF url, excl. SEC cross-listed) : "
         f"{with_pdf}/{non_sec_total} ({_pct(with_pdf, non_sec_total):.1f}%)")
    pdf_years = summary.get("filing_pdf_years", 0)
    pdf_cos = summary.get("filing_pdf_companies", 0)
    if pdf_years:
        print(f"  multi-year annual filings collected : {pdf_years} across "
             f"{pdf_cos} compan(ies) (table 'filing_pdfs', up to 5yr each)")
    print("=" * 44)
    print(f"  total run time : {_fmt(elapsed_total)}")
    print("=" * 44)
    print(f"\nDetails in {cfg.get('storage', {}).get('db_path', 'filings.db')} "
          "(table 'filings'). Re-run with --resume to retry non-found rows.\n"
          "Tip: rows tagged '+stale' in discovery_method have an annual report "
          "older than expected -- worth a spot check.")


if __name__ == "__main__":
    main()
