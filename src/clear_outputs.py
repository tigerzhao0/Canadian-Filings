#!/usr/bin/env python3
"""Clear generated output/ files -- tiered so you can never accidentally wipe
the expensive-to-rebuild stuff with a single flag.

TIERS (each includes everything in the tier(s) above it):
  default   xlsx exports + dashboard + per-company JSON
            -- trivial to regenerate: `python run.py --step 4 --force`
            -- python src/build_dashboard.py
  --derived + pdf_financials.db (canonical/derived schema-mapped data)
            -- cheap to regenerate (~3 min for all companies):
               `python run.py --step 4 --force`
  --filings + filings.db (discovered PDF links, extracted statement text,
            parsed line items -- steps 1-3). KEEPS financials.db (QuoteMedia
            ground truth). Use this to re-run the PDF pipeline.
  --all     + financials.db too (QuoteMedia ground truth)
            -- EXPENSIVE to regenerate: filings.db needs the full step 1
               (search/crawl/CSE/TMX/SEC/annualreports.com -- can take an
               hour+ over ~2500 companies) and step 2 (downloads every PDF);
               financials.db needs `run.py --financials` (QuoteMedia API pass).
            -- --filings/--all require --yes too, so neither runs unattended.

PER-COMPANY (orthogonal to the tiers above -- surgical, not whole-file):
  --ticker RY   delete every row for ONE company across filings.db AND
                pdf_financials.db (every ticker-keyed table), plus its
                exported xlsx/json files. Keeps financials.db (QuoteMedia
                ground truth for that ticker) unless --with-quotemedia is
                also given. For a full, isolated single-company re-test.

Usage:
  python src/clear_outputs.py                  # safe default
  python src/clear_outputs.py --dry-run         # show what WOULD be deleted
  python src/clear_outputs.py --derived
  python src/clear_outputs.py --filings --yes
  python src/clear_outputs.py --all --yes
  python src/clear_outputs.py --ticker RY --dry-run
  python src/clear_outputs.py --ticker RY --yes
  python src/clear_outputs.py --ticker RY --with-quotemedia --yes
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"

# Every ticker-keyed table, per DB. financials.db and pdf_financials.db share
# the same schema_financials.sql shape; filings.db is schema.sql +
# raw_line_items (added by line_items.py). run_log is run-level, not
# ticker-keyed, and is deliberately excluded.
_FILINGS_TICKER_TABLES = ("filings", "filing_pdfs", "pdf_extractions", "raw_line_items")
_FINANCIALS_TICKER_TABLES = (
    "companies", "company_years", "statement_lines", "statement_line_quality",
    "line_items_full", "tmx_financials_raw", "tmx_financials_status",
    "pdf_llm_raw", "pdf_llm_consistency", "pdf_llm_status",
)

# (path relative to output/, human label, regeneration hint)
_SAFE = [
    ("pdf_financials_xlsx", "per-company xlsx exports (rules pipeline)",
     "python run.py --step 4 --force"),
    ("financials_xlsx", "per-company xlsx exports (QuoteMedia pipeline)",
     "python src/company_xlsx_export.py --all --db output/financials.db --out-dir output/financials_xlsx"),
    ("companies", "per-company JSON exports",
     "python src/company_export.py --all"),
    ("pdf_companies", "per-company JSON exports (rules pipeline)", None),
    ("financials_dashboard.html", "dashboard HTML",
     "python src/build_dashboard.py"),
]
_DERIVED = [
    ("pdf_financials.db", "canonical schema-mapped data (statement_lines, "
     "line_items_full, quality)", "python run.py --step 4 --force"),
]
# The PDF-pipeline's own expensive artifact (rebuilt by re-running steps 1-3
# with a full re-download). Separate from financials.db so a PDF re-run can
# wipe this WITHOUT touching the QuoteMedia ground truth.
_FILINGS = [
    ("filings.db", "discovered PDF links + extracted statement text + "
     "parsed line items (steps 1-3)",
     "python run.py --step 1 --input <company list> --full   (then --step 2, --step 3)"),
]
# The QuoteMedia ground truth -- unrelated to the PDF pipeline, only the
# baseline verify_company compares against. Rarely needs clearing.
_QUOTEMEDIA = [
    ("financials.db", "QuoteMedia ground-truth financials (verify_company baseline)",
     "python run.py --financials --input <company list> --full"),
]


def _targets(derived: bool, filings: bool, all_: bool
             ) -> list[tuple[Path, str, str | None]]:
    picked = list(_SAFE)
    if derived or filings or all_:
        picked += _DERIVED
    if filings or all_:
        picked += _FILINGS
    if all_:
        picked += _QUOTEMEDIA
    return [(OUTPUT / rel, label, hint) for rel, label, hint in picked]


def _delete_ticker_rows(db_path: Path, ticker: str, tables: tuple, *,
                        dry_run: bool) -> dict[str, int]:
    """DELETE every row for `ticker` across `tables` in one DB. Returns
    {table: row_count} for tables that actually had rows (0-row tables are
    omitted so the report stays readable). Tables absent from an older DB are
    skipped silently -- same tolerance as purge_ticker_lines."""
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    counts: dict[str, int] = {}
    try:
        for table in tables:
            try:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE ticker = ?",  # noqa: S608
                    (ticker,)).fetchone()[0]
            except sqlite3.OperationalError:
                continue  # table doesn't exist in this DB
            if n == 0:
                continue
            counts[table] = n
            if not dry_run:
                conn.execute(f"DELETE FROM {table} WHERE ticker = ?", (ticker,))  # noqa: S608
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return counts


def clear_ticker(ticker: str, *, with_quotemedia: bool, dry_run: bool) -> None:
    """Surgical per-company delete: every row for `ticker` across
    filings.db + pdf_financials.db (and financials.db too when
    with_quotemedia), plus its exported xlsx/json files. Everything else
    (all other companies) is untouched."""
    ticker = ticker.strip().upper()
    verb = "Would delete" if dry_run else "Deleting"
    print(f"{'[DRY RUN] ' if dry_run else ''}{verb} ALL data for ticker={ticker}:")

    dbs = [
        (OUTPUT / "filings.db", _FILINGS_TICKER_TABLES, "filings.db"),
        (OUTPUT / "pdf_financials.db", _FINANCIALS_TICKER_TABLES, "pdf_financials.db"),
    ]
    if with_quotemedia:
        dbs.append((OUTPUT / "financials.db", _FINANCIALS_TICKER_TABLES, "financials.db"))
    else:
        print("  (keeping financials.db -- QuoteMedia ground truth for this "
             "ticker stays intact; add --with-quotemedia to also clear it)")

    total_rows = 0
    for db_path, tables, label in dbs:
        counts = _delete_ticker_rows(db_path, ticker, tables, dry_run=dry_run)
        if not counts:
            print(f"  {label}: no rows found")
            continue
        for table, n in counts.items():
            print(f"  {label}::{table}  {n} row(s)")
            total_rows += n

    files = [
        OUTPUT / "pdf_financials_xlsx" / f"{ticker}.xlsx",
        OUTPUT / "companies" / f"{ticker}.json",
    ]
    if with_quotemedia:
        files.append(OUTPUT / "financials_xlsx" / f"{ticker}.xlsx")
    freed_files = 0
    for f in files:
        if not f.exists():
            continue
        print(f"  {'would delete' if dry_run else 'deleting':12} {f.relative_to(ROOT)}")
        if not dry_run:
            try:
                f.unlink()
                freed_files += 1
            except PermissionError:
                print(f"    ! still locked by another process, NOT deleted: "
                     f"{f.relative_to(ROOT)}")

    print(f"\n{'Would remove' if dry_run else 'Removed'}: {total_rows} row(s) "
         f"across {len(dbs)} DB(s)"
         + ("" if dry_run else f", {freed_files} file(s)"))
    if not dry_run:
        print(f"\nRe-run just this company with, e.g.:")
        print(f"  # --step 1 has NO --tickers filter -- it reads the whole --input")
        print(f"  # list. Isolate one company with a tiny ticker,name,exchange CSV")
        print(f"  # (see ingest.py) as --input, or a filtered xlsx, then --full:")
        print(f"  python run.py --step 1 --input <tiny CSV/xlsx with just {ticker}> --full")
        print(f"  python run.py --step 2")
        print(f"  python run.py --step 3 --tickers {ticker} --force")
        print(f"  python run.py --step 4 --tickers {ticker} --force")


def _size(p: Path) -> int:
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--derived", action="store_true",
                    help="Also clear pdf_financials.db (cheap to rebuild).")
    ap.add_argument("--filings", action="store_true",
                    help="Also clear filings.db + pdf_financials.db (the PDF "
                         "pipeline -- re-download & re-extract). KEEPS "
                         "financials.db (QuoteMedia ground truth). Use this to "
                         "re-run the PDF pipeline. Requires --yes too.")
    ap.add_argument("--all", action="store_true",
                    help="Also clear filings.db + financials.db (EVERYTHING, "
                         "incl. the QuoteMedia ground truth). Requires --yes.")
    ap.add_argument("--yes", action="store_true",
                    help="Required alongside --filings/--all to actually delete "
                         "the expensive tier. Ignored otherwise.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be deleted, delete nothing.")
    ap.add_argument("--ticker", metavar="TICKER",
                    help="Surgical per-company delete: wipe every row for this "
                         "ticker across filings.db + pdf_financials.db, plus its "
                         "exported files. Ignores the tier flags above. Requires "
                         "--yes (or --dry-run to preview).")
    ap.add_argument("--with-quotemedia", action="store_true",
                    help="With --ticker, also clear this ticker's rows from "
                         "financials.db (QuoteMedia ground truth). Default keeps it.")
    args = ap.parse_args()

    if args.ticker:
        if not args.yes and not args.dry_run:
            print(f"--ticker {args.ticker} wants to delete all data for this "
                 "company -- re-run with --yes to confirm, or add --dry-run to preview.")
            raise SystemExit(1)
        clear_ticker(args.ticker, with_quotemedia=args.with_quotemedia,
                    dry_run=args.dry_run)
        return

    if (args.all or args.filings) and not args.yes and not args.dry_run:
        which = "filings.db + financials.db" if args.all else "filings.db"
        print(f"--{'all' if args.all else 'filings'} wants to delete {which} -- "
              "that took real time to build (network crawl"
              f"{' / QuoteMedia API' if args.all else ''}).")
        print(f"Re-run with --{'all' if args.all else 'filings'} --yes to "
              "confirm, or add --dry-run to preview.")
        raise SystemExit(1)

    targets = _targets(args.derived, args.filings, args.all)
    tier = ("ALL incl. QuoteMedia" if args.all
            else "PDF pipeline (keeps financials.db)" if args.filings
            else "derived" if args.derived else "safe")
    total = freed = 0
    locked: list[str] = []
    print(f"{'[DRY RUN] ' if args.dry_run else ''}Clearing output/ ({tier} tier):")
    for path, label, hint in targets:
        n = _size(path)
        if n == 0 and not path.exists():
            continue
        total += n
        print(f"  {'would delete' if args.dry_run else 'deleting':12} "
             f"{path.relative_to(ROOT)}  ({_fmt_size(n)})  -- {label}")
        if args.dry_run:
            continue
        # NOTE: no ignore_errors -- a file open elsewhere (e.g. an xlsx open
        # in Excel) must be reported, not silently pretended-cleared.
        blocked = []
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    try:
                        child.unlink()
                    except PermissionError:
                        blocked.append(child)
            for child in sorted(path.rglob("*"), key=lambda p: -len(p.parts)):
                if child.is_dir():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            path.mkdir(parents=True, exist_ok=True)
        else:
            try:
                path.unlink(missing_ok=True)
            except PermissionError:
                blocked.append(path)
        if blocked:
            for b in blocked:
                locked.append(str(b.relative_to(ROOT)))
            print(f"    ! still locked by another process, NOT deleted: "
                 f"{', '.join(str(b.relative_to(ROOT)) for b in blocked)}")
        else:
            freed += n

    verb = "Would free" if args.dry_run else "Freed"
    print(f"\n{verb}: {_fmt_size(total if args.dry_run else freed)}"
         + (f"  ({_fmt_size(total - freed)} skipped -- locked)" if locked else ""))
    if locked:
        print("\nClose the file(s)/app holding these open, then re-run to finish:")
        for f in locked:
            print(f"  {f}")
    if not args.dry_run:
        print("\nRegenerate with:")
        seen = set()
        for _p, _label, hint in targets:
            if hint and hint not in seen:
                seen.add(hint)
                print(f"  {hint}")


if __name__ == "__main__":
    main()
