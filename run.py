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
import sys
from pathlib import Path

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
    ap.add_argument("--input", required=True,
                    help="Company list (.xlsx GuruFocus export or ticker,name,exchange CSV).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--pilot", action="store_true",
                      help="Run a small sampled subset (default mode).")
    mode.add_argument("--full", action="store_true", help="Run the entire input file.")
    mode.add_argument("--resume", action="store_true",
                      help="Continue against the existing DB (same as full; found rows skipped).")
    ap.add_argument("--sample-size", type=int, default=None,
                    help="Pilot sample size (default from config, ~40).")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="Config YAML (defaults to config.example.yaml).")
    ap.add_argument("--no-render", action="store_true",
                    help="Skip the Tier-2 Playwright render pass (Tier 1 only).")
    ap.add_argument("--render-only", action="store_true",
                    help="Run ONLY the Tier-2 render pass over rows the fast tier "
                         "left unresolved in the existing DB.")
    args = ap.parse_args()

    # --- fail-fast validation ------------------------------------------------
    if not args.config.exists():
        _die(f"Config file not found: {args.config}")
    cfg = _load_config(args.config)

    input_path = Path(args.input)
    if not input_path.exists():
        _die(f"Input file not found: {input_path}\n"
             "Pass the GuruFocus .xlsx export or a ticker,legal_company_name,exchange CSV.")

    _validate_provider_ready(cfg)

    # Imports deferred until after arg validation so `--help` works w/o deps.
    from ingest import load_companies
    from search_provider import build_provider
    from pipeline import run_pipeline

    try:
        companies = load_companies(input_path)
    except Exception as exc:  # noqa: BLE001
        _die(f"Could not read input: {exc}")

    # Mode selection: pilot is the default unless --full/--resume given.
    full_mode = args.full or args.resume
    if full_mode:
        selected = companies
        mode_label = "FULL" + (" (resume)" if args.resume else "")
    else:
        size = args.sample_size or int(cfg.get("pilot", {}).get("default_sample_size", 40))
        selected = _pilot_sample(companies, size)
        mode_label = f"PILOT (sample of {len(selected)})"

    provider_name = (cfg.get("search", {}).get("provider") or "duckduckgo")
    print(f"Mode: {mode_label}  |  search provider: {provider_name}  |  "
          f"DB: {cfg.get('storage', {}).get('db_path', 'filings.db')}")

    provider = build_provider(cfg)
    render_default = bool(cfg.get("render", {}).get("enabled", True))
    use_render = render_default and not args.no_render
    summary = asyncio.run(run_pipeline(
        selected, provider, cfg,
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
    print(f"  total run time : {_fmt(elapsed_total)}")
    print("=" * 44)
    print(f"\nDetails in {cfg.get('storage', {}).get('db_path', 'filings.db')} "
          "(table 'filings'). Re-run with --resume to retry non-found rows.\n"
          "Tip: rows tagged '+stale' in discovery_method have an annual report "
          "older than expected -- worth a spot check.")


if __name__ == "__main__":
    main()
