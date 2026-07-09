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
    ap.add_argument("--step", type=int, choices=(1, 2, 3), default=1,
                    help="1 (default) = the pipeline: structured financials + find the "
                         "annual-report PDFs for the tail QuoteMedia misses. 2 = process "
                         "those found PDFs: download -> extract income/balance/cash-flow "
                         "statement text -> delete the file -> store in pdf_extractions. "
                         "3 = local-LLM extraction: read pdf_extractions, map each "
                         "statement's text to the canonical financials schema via a local "
                         "Ollama model, and write to financials.db (source='cse_pdf_extract'). "
                         "Steps 2 and 3 read from the DB and need no --input.")
    ap.add_argument("--input", required=False,
                    help="Company list (.xlsx GuruFocus export or ticker,name,exchange CSV). "
                         "Required for --step 1; ignored for --step 2.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--pilot", action="store_true",
                      help="Run a small sampled subset (default mode).")
    mode.add_argument("--full", action="store_true", help="Run the entire input file.")
    mode.add_argument("--resume", action="store_true",
                      help="Continue against the existing DB (same as full; found rows skipped).")
    ap.add_argument("--sample-size", type=int, default=None,
                    help="Pilot sample size (default from config, ~40).")
    ap.add_argument("--force", action="store_true",
                    help="(--step 3) Re-run statements already marked 'ok' in "
                         "pdf_llm_status instead of skipping them.")
    ap.add_argument("--limit", type=int, default=None,
                    help="(--step 3) Process at most N pdf rows (smoke testing).")
    ap.add_argument("--tickers", default=None,
                    help="(--step 3) Comma-separated ticker allow-list (smoke testing).")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="Config YAML (defaults to config.example.yaml).")
    ap.add_argument("--no-render", action="store_true",
                    help="Skip the Tier-2 Playwright render pass (Tier 1 only).")
    ap.add_argument("--render-only", action="store_true",
                    help="Run ONLY the Tier-2 render pass over rows the fast tier "
                         "left unresolved in the existing DB.")
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
        print(f"  elapsed        : {result['elapsed']:.1f}s")
        print(f"  stored in      : {result['db_path']} (table 'pdf_extractions')")
        print("=" * 44)
        return

    # Step 3: local-LLM extraction. Reads pdf_extractions (filings.db), writes the
    # canonical financials tables (financials.db). GPU-serial, so synchronous --
    # NOT asyncio.run like steps 1/2. Needs no --input.
    if args.step == 3:
        from llm_extract import run_llm_extraction
        src_db = cfg.get("storage", {}).get("db_path", "output/filings.db")
        fin_db = cfg.get("tmx_financials", {}).get("db_path", "output/financials.db")
        tickers = ({t.strip() for t in args.tickers.split(",") if t.strip()}
                   if args.tickers else None)
        print(f"Step 3: LLM extraction  |  read {src_db} -> write {fin_db}")
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

    # Mode selection: pilot is the default unless --full/--resume given.
    full_mode = args.full or args.resume
    if full_mode:
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
    else:
        # Default flow: structured financials FIRST (cheap, reliable, covers
        # ~99% of TSX/TSXV/CSE/XCNQ/NEOE), then the PDF finder only for
        # whatever's left -- which already runs CSE filings -> SEC cross-list
        # check -> TMX filings -> Tier 1 -> Tier 2 internally (see
        # pipeline.py), so this naturally chains into the requested
        # financials -> SEC -> full PDF pipeline order without needing to
        # reorder pipeline.py itself.
        from financials_pipeline import run_tmx_financials
        financials_targets = [c for c in selected if is_tmx_exchange(c.exchange)]
        print(f"\nStage 1/2: structured financials (TSX/TSXV/CSE/XCNQ/NEOE via "
             f"QuoteMedia) on {len(financials_targets)} compan(ies) -> "
             f"{cfg.get('tmx_financials', {}).get('db_path', 'output/financials.db')}")
        fin_result = asyncio.run(run_tmx_financials(financials_targets, cfg, progress=print))
        _print_financials_summary(fin_result)

        resolved_tickers = fin_result.get("resolved_tickers", set())
        remaining = [c for c in selected if c.ticker not in resolved_tickers]
        print(f"\nStage 2/2: PDF finder (CSE filings -> SEC cross-list check -> "
             f"TMX filings -> Tier 1 -> Tier 2) on {len(remaining)} compan(ies) "
             "not covered by structured financials...")

    _validate_provider_ready(cfg)
    from search_provider import build_provider
    from pipeline import run_pipeline

    provider_name = (cfg.get("search", {}).get("provider") or "duckduckgo")
    print(f"search provider: {provider_name}  |  "
          f"DB: {cfg.get('storage', {}).get('db_path', 'filings.db')}")

    provider = build_provider(cfg)
    render_default = bool(cfg.get("render", {}).get("enabled", True))
    use_render = render_default and not args.no_render
    summary = asyncio.run(run_pipeline(
        remaining, provider, cfg,
        use_render=use_render, render_only=args.render_only, progress=print))

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
